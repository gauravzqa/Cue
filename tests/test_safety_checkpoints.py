"""Checkpoints as a third row kind, and the lock that lets two threads share
the journal.

On determinism: neither thread test here can fail spuriously. The lock test
asserts that a second caller CANNOT proceed while the lock is held, so the only
way it fails is if the lock is gone. The stress test asserts that every entry
written is readable afterwards, which is true under the lock and only sometimes
true without it. Both can therefore flake toward a false pass and never toward
a false failure -- which is the only direction a concurrency test is allowed to
be wrong in, because a test that goes red at random teaches people to re-run.
"""

from __future__ import annotations

import threading

from daa.contracts import UndoAction
from daa.safety.undo import UndoJournal


def _undo(n: int) -> UndoAction:
    return UndoAction(
        description=f"put back {n}",
        tool="move_files",
        args={"pairs": [[f"/tmp/daa-test/{n}.txt", f"/tmp/daa-test/old/{n}.txt"]]},
    )


def _journal(tmp_path) -> UndoJournal:
    return UndoJournal(tmp_path / "undo.jsonl")


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def test_a_checkpoint_is_an_index_over_rows_not_a_snapshot(tmp_path):
    journal = _journal(tmp_path)
    checkpoint = journal.open_checkpoint(job_id="job1")
    inside = [journal.record(_undo(i), produced_by="move_files",
                             checkpoint_id=checkpoint.id) for i in range(3)]
    journal.record(_undo(9), produced_by="move_files")  # outside the window

    found = journal.checkpoint(checkpoint.id)
    assert found is not None
    assert found.job_id == "job1"
    assert set(found.entry_ids) == {e.id for e in inside}
    assert len(found.entry_ids) == 3


def test_the_window_comes_back_newest_first(tmp_path):
    """Reverse order is not a detail: replaying a window forwards re-applies
    the mutations in order, with each inverse fighting the one after it."""
    journal = _journal(tmp_path)
    checkpoint = journal.open_checkpoint()
    made = [journal.record(_undo(i), produced_by="move_files",
                           checkpoint_id=checkpoint.id) for i in range(3)]
    window = journal.window(checkpoint.id)
    assert [e.id for e in window] == [e.id for e in reversed(made)]


def test_a_consumed_entry_leaves_the_window(tmp_path):
    journal = _journal(tmp_path)
    checkpoint = journal.open_checkpoint()
    first = journal.record(_undo(1), produced_by="move_files", checkpoint_id=checkpoint.id)
    journal.record(_undo(2), produced_by="move_files", checkpoint_id=checkpoint.id)
    journal.commit(first)
    assert [e.id for e in journal.window(checkpoint.id)] != [first.id]
    assert len(journal.window(checkpoint.id)) == 1


def test_sealing_does_not_disable_rollback_it_changes_the_sentence(tmp_path):
    journal = _journal(tmp_path)
    checkpoint = journal.open_checkpoint()
    journal.record(_undo(1), produced_by="move_files", checkpoint_id=checkpoint.id)
    sealed = journal.seal_checkpoint(checkpoint.id, "the email has already gone")
    assert sealed is not None and sealed.sealed is True
    assert journal.seal_reason(checkpoint.id) == "the email has already gone"
    # Still rollable. Checkpoints do not make irreversible things reversible;
    # they make the boundary speakable.
    assert len(journal.window(checkpoint.id)) == 1


def test_sealing_an_unknown_checkpoint_is_a_no_op_not_a_new_one(tmp_path):
    journal = _journal(tmp_path)
    assert journal.seal_checkpoint("nope", "x") is None
    assert journal.checkpoints() == []


def test_checkpoints_survive_a_restart_on_the_existing_machinery(tmp_path):
    path = tmp_path / "undo.jsonl"
    first = UndoJournal(path)
    checkpoint = first.open_checkpoint(job_id="job1")
    first.record(_undo(1), produced_by="move_files", checkpoint_id=checkpoint.id)
    first.seal_checkpoint(checkpoint.id, "the email has already gone")

    second = UndoJournal(path)
    reloaded = second.checkpoint(checkpoint.id)
    assert reloaded is not None
    assert reloaded.sealed is True
    assert second.seal_reason(checkpoint.id) == "the email has already gone"
    assert len(reloaded.entry_ids) == 1
    assert oct(path.stat().st_mode)[-3:] == "600", "no new file, no new hardening code"


def test_a_checkpoint_row_does_not_disturb_the_plain_undo_path(tmp_path):
    journal = _journal(tmp_path)
    checkpoint = journal.open_checkpoint()
    entry = journal.record(_undo(1), produced_by="move_files", checkpoint_id=checkpoint.id)
    assert journal.peek().id == entry.id
    assert len(journal) == 1


def test_a_forged_checkpoint_row_cannot_manufacture_an_entry(tmp_path):
    """The journal is untrusted input, and a third row kind is a third thing a
    hostile row can claim to be."""
    path = tmp_path / "undo.jsonl"
    path.write_text(
        '{"kind":"checkpoint","id":{"not":"a string"},"sealed":true}\n'
        '{"kind":"checkpoint"}\n'
    )
    journal = UndoJournal(path)
    assert journal.checkpoints() == []
    assert journal.peek() is None


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_a_second_thread_cannot_read_the_journal_mid_reload(tmp_path):
    """`reload()` EMPTIES `_entries` before refilling it.

    A concurrent `peek()` landing in that window sees an empty journal and
    reports there is nothing to undo, which is the most expensive lie this file
    can tell. The lock closes the window, and this test holds it open on
    purpose to prove the lock is what closes it.
    """
    journal = _journal(tmp_path)
    journal.record(_undo(1), produced_by="move_files")

    inside = threading.Event()
    release = threading.Event()
    real_append = journal._append

    def blocking_append(row):
        inside.set()
        assert release.wait(5.0), "the writer was never released"
        return real_append(row)

    journal._append = blocking_append  # type: ignore[method-assign]
    peeked: list[object] = []
    writer = threading.Thread(
        target=lambda: journal.record(_undo(2), produced_by="move_files"), daemon=True
    )
    reader = threading.Thread(target=lambda: peeked.append(journal.peek()), daemon=True)

    writer.start()
    assert inside.wait(5.0), "the writer never reached the locked section"
    reader.start()
    # The reader must be BLOCKED. If it completes here, the lock is gone.
    reader.join(timeout=0.25)
    assert reader.is_alive(), "peek() ran while record() held the journal"
    assert peeked == []

    release.set()
    writer.join(timeout=5.0)
    reader.join(timeout=5.0)
    assert not reader.is_alive()
    assert peeked and peeked[0] is not None


def test_many_threads_recording_lose_nothing(tmp_path):
    journal = _journal(tmp_path)
    threads = 8
    per_thread = 12
    start = threading.Barrier(threads)
    errors: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            start.wait(timeout=5.0)
            for i in range(per_thread):
                journal.record(_undo(worker * 100 + i), produced_by="move_files")
                # Reading while others write is the interleaving that matters.
                journal.history(3)
                len(journal)
        except BaseException as exc:  # noqa: BLE001 -- the assertion is "none"
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(w,), daemon=True) for w in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10.0)
        assert not w.is_alive()

    assert errors == [], f"{errors[:2]}"
    assert len(journal) == threads * per_thread
    assert len(UndoJournal(tmp_path / "undo.jsonl")) == threads * per_thread


def test_the_lock_is_reentrant_because_the_public_methods_nest(tmp_path):
    """`pop()` calls `peek()` and `commit()`, both of which take the lock. A
    plain Lock deadlocks on the first pop."""
    journal = _journal(tmp_path)
    journal.record(_undo(1), produced_by="move_files")
    done = threading.Event()

    def popper() -> None:
        journal.pop()
        done.set()

    thread = threading.Thread(target=popper, daemon=True)
    thread.start()
    assert done.wait(5.0), "pop() deadlocked; the journal lock is not reentrant"
    assert len(journal) == 0
