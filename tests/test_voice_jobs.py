"""The job registry, the notice board, and the step pump.

Determinism note, because it is the whole reason these tests are worth having:
NOTHING HERE SLEEPS. The clock is injected everywhere, and every handoff in the
threaded design is a queue put -- including the one that says a job ended -- so
a test can block until something really happened instead of guessing how long
to wait. A flaky concurrency test is worse than none, because it teaches people
to re-run.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from daa.contracts import JobStatus, ResolvedAction, RiskTier, ToolResult, Warrant
from daa.safety.grant import action_digest
from daa.voice.jobs import (
    InlineChannel,
    JobFinished,
    JobRegistry,
    JobRunner,
    NoticeBoard,
    QueueChannel,
    StepRequest,
    StepVerdict,
    coalesce,
)


class Clock:
    """An injected clock. Tests move time by assignment, never by sleeping."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _action(name: str = "move_files") -> ResolvedAction:
    return ResolvedAction(tool=name, args={}, targets=("a.txt",), verb="move")


def _warrant() -> Any:
    return Warrant(
        id="w1",
        action_digest=action_digest(_action()),
        tier=RiskTier.ANNOUNCE,
        issued_at=0.0,
        expires_at=1e12,
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_a_job_record_survives_a_restart_but_the_run_does_not(tmp_path):
    """The single most important property of this file. A browser agent that
    wakes up mid-checkout in a world that moved on is the worst thing this
    design could produce."""
    path = tmp_path / "jobs.jsonl"
    first = JobRegistry(path, now=Clock(1000.0))
    job = first.create(goal="clear out downloads")
    first.update(job.id, status=JobStatus.RUNNING)

    second = JobRegistry(path, now=Clock(2000.0))
    reloaded = second.get(job.id)
    assert reloaded is not None
    assert reloaded.status is JobStatus.INTERRUPTED, "a job must never silently resume"
    assert second.interrupted == (job.id,)
    assert second.any_live() is False


def test_the_jobs_file_inherits_the_0600_hardening(tmp_path):
    path = tmp_path / "jobs.jsonl"
    JobRegistry(path, now=Clock()).create(goal="x")
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert oct(path.parent.stat().st_mode)[-3:] == "700"


def test_only_one_job_at_a_time(tmp_path):
    """Two agents driving the same GUI is not a concurrency problem, it is a
    correctness problem, so a second request is a question, not a queue."""
    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock(), max_jobs=1)
    registry.create(goal="one")
    with pytest.raises(RuntimeError):
        registry.create(goal="two")


def test_revocation_is_idempotent_and_always_succeeds(tmp_path):
    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    assert registry.revoke(job.id, "you said stop") is True
    assert registry.revoke(job.id, "you said stop again") is True
    assert registry.cancelled(job.id) is True
    assert registry.get(job.id).status is JobStatus.CANCELLED


def test_revocation_rolls_nothing_back(tmp_path):
    """Undoing on "stop" would make stopping more destructive than letting it
    finish, which is precisely backwards."""
    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    registry.update(job.id, checkpoint_id="cp1")
    registry.revoke(job.id, "stop")
    assert registry.get(job.id).checkpoint_ids == ("cp1",), "the work is still on the books"


def test_a_malformed_row_is_dropped_not_defaulted_into_running(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(
        '{"kind":"job","id":"a1","status":"who knows","goal":"g"}\n'
        "not json at all\n"
        '{"kind":"undo","id":"b2"}\n'
    )
    registry = JobRegistry(path, now=Clock())
    assert registry.get("b2") is None
    assert registry.get("a1").status is JobStatus.INTERRUPTED


def test_the_interrupted_sentence_is_said_once_and_spoken(tmp_path):
    path = tmp_path / "j.jsonl"
    first = JobRegistry(path, now=Clock())
    job = first.create(goal="clearing out Downloads")
    first.update(job.id, status=JobStatus.RUNNING)
    second = JobRegistry(path, now=Clock())
    line = coalesce(second.all())
    assert line == "I was part way through clearing out Downloads when I stopped."


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


def test_notices_coalesce_into_one_spoken_line():
    board = NoticeBoard(now=Clock())
    board.post("j1", "that download finished")
    board.post("j1", "the files are filed")
    text, spoken, expired = board.drain()
    assert text == "that download finished. Also, the files are filed."
    assert len(spoken) == 2
    assert expired == 0
    assert board.drain()[0] == "", "draining consumes; an interrupted notice is not retried"


def test_a_stale_notice_is_dropped_never_spoken_late():
    clock = Clock(1000.0)
    board = NoticeBoard(now=clock, ttl_s=60.0)
    board.post("j1", "that download finished")
    clock.t = 1100.0
    text, spoken, expired = board.drain()
    assert text == ""
    assert spoken == []
    assert expired == 1


def test_the_board_is_capped_and_says_how_many_it_dropped():
    board = NoticeBoard(now=Clock(), cap=2)
    for i in range(4):
        board.post("j1", f"thing {i}")
    text, spoken, _ = board.drain()
    assert len(spoken) == 2
    assert "2 other things finished" in text


def test_there_is_no_high_urgency():
    """No background job is important enough to talk over a human, and
    providing the option guarantees something eventually uses it."""
    board = NoticeBoard(now=Clock())
    notice = board.post("j1", "x", urgency="high")
    assert notice.urgency == "normal"


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Spy:
    """Records what it was asked and what it was told to do."""

    asked: list[ResolvedAction] = field(default_factory=list)
    performed: list[ResolvedAction] = field(default_factory=list)
    verdicts: list[StepVerdict] = field(default_factory=list)

    def ask(self, request: StepRequest) -> StepVerdict:
        self.asked.append(request.action)
        if self.verdicts:
            return self.verdicts.pop(0)
        return StepVerdict(warrant=_warrant())

    def perform(self, action: ResolvedAction, verdict: StepVerdict, index: int) -> ToolResult:
        self.performed.append(action)
        return ToolResult(ok=True, summary=f"step {index}")


def _stream(n: int, final: str = "All done."):
    """A LongRunningTool's generator: yields actions, receives results."""
    seen = []
    for i in range(n):
        result = yield _action(f"step_{i}")
        seen.append(result)
    return ToolResult(ok=True, summary=final)


def _runner(tmp_path, spy: Spy, *, steps: int = 3, max_steps: int = 10, notices=None):
    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="do the thing")
    return JobRunner(
        job_id=job.id,
        stream=_stream(steps),
        ask=spy.ask,
        perform=spy.perform,
        registry=registry,
        cancel=registry.cancel_event(job.id),
        notices=notices,
        max_steps=max_steps,
    ), registry, job


def test_the_agent_can_only_propose(tmp_path):
    """Every action reaches `perform` only after `ask` said yes, and there is
    no path from the generator to a tool that does not pass through both."""
    spy = Spy()
    runner, registry, job = _runner(tmp_path, spy, steps=3)
    assert runner.drive() is JobStatus.DONE
    assert len(spy.asked) == 3
    assert spy.performed == spy.asked, "something ran that was not authorized"
    assert registry.get(job.id).status is JobStatus.DONE
    assert registry.get(job.id).summary == "All done."


def test_a_refused_step_stops_the_job_and_nothing_after_it_runs(tmp_path):
    spy = Spy()
    spy.verdicts = [
        StepVerdict(warrant=_warrant()),
        StepVerdict(reason="Okay, leaving it.", status=JobStatus.CANCELLED),
    ]
    runner, registry, job = _runner(tmp_path, spy, steps=5)
    assert runner.drive() is JobStatus.CANCELLED
    assert len(spy.performed) == 1, "the job carried on past a refusal"
    assert registry.get(job.id).status is JobStatus.CANCELLED


def test_cancellation_is_checked_between_steps(tmp_path):
    spy = Spy()
    runner, registry, job = _runner(tmp_path, spy, steps=5)
    registry.cancel_event(job.id).set()
    assert runner.drive() is JobStatus.CANCELLED
    assert spy.performed == [], "a cancelled job must not take another step"


def test_the_step_budget_is_a_hard_stop(tmp_path):
    spy = Spy()
    runner, registry, job = _runner(tmp_path, spy, steps=100, max_steps=3)
    assert runner.drive() is JobStatus.EXPIRED
    assert len(spy.performed) == 3
    assert registry.get(job.id).status is JobStatus.EXPIRED


def test_an_agent_that_raises_fails_the_job_rather_than_the_loop(tmp_path):
    def exploding():
        yield _action()
        raise RuntimeError("the page changed under me")

    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    spy = Spy()
    runner = JobRunner(
        job_id=job.id,
        stream=exploding(),
        ask=spy.ask,
        perform=spy.perform,
        registry=registry,
        cancel=registry.cancel_event(job.id),
    )
    assert runner.drive() is JobStatus.FAILED
    assert registry.get(job.id).error == "the page changed under me"
    assert registry.get(job.id).summary == "That didn't work out."


def test_an_agent_that_yields_junk_is_not_executed(tmp_path):
    def junk():
        yield {"tool": "move_files", "args": {}}

    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    spy = Spy()
    runner = JobRunner(
        job_id=job.id,
        stream=junk(),
        ask=spy.ask,
        perform=spy.perform,
        registry=registry,
        cancel=registry.cancel_event(job.id),
    )
    assert runner.drive() is JobStatus.FAILED
    assert spy.asked == [] and spy.performed == []


def test_progress_is_spoken_english_not_a_log_line(tmp_path):
    spy = Spy()
    spy.verdicts = [StepVerdict(reason="stop", status=JobStatus.CANCELLED)]
    runner, registry, job = _runner(tmp_path, spy, steps=2)
    runner.drive()
    progress = registry.get(job.id).progress
    assert progress is not None
    assert progress.phase == "move", "the phase is the verb the user would hear"
    assert progress.step == 1


def test_the_job_posts_one_notice_and_never_speaks(tmp_path):
    board = NoticeBoard(now=Clock())
    spy = Spy()
    runner, _registry, job = _runner(tmp_path, spy, steps=2, notices=board)
    runner.drive()
    pending = board.pending()
    assert [n.text for n in pending] == ["All done."]
    assert pending[0].job_id == job.id


# ---------------------------------------------------------------------------
# The channels
# ---------------------------------------------------------------------------


def test_the_inline_channel_produces_the_same_order_as_the_queue_one(tmp_path):
    """The seam removes the handoff and nothing else: propose, authorize,
    execute, repeat -- in that order, on one thread."""
    order: list[str] = []
    spy = Spy()

    def ask(request: StepRequest) -> StepVerdict:
        order.append(f"ask{request.step_index}")
        return StepVerdict(warrant=_warrant())

    def perform(action: ResolvedAction, verdict: StepVerdict, index: int) -> ToolResult:
        order.append(f"do{index}")
        return ToolResult(ok=True, summary="")

    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    channel = InlineChannel(ask)
    runner = JobRunner(
        job_id=job.id,
        stream=_stream(3),
        ask=channel.ask,
        perform=perform,
        registry=registry,
        cancel=registry.cancel_event(job.id),
        finished=channel.finish,
    )
    runner.drive()
    assert order == ["ask1", "do1", "ask2", "do2", "ask3", "do3"]
    assert [e.status for e in channel.finished] == [JobStatus.DONE]
    del spy


def test_a_worker_whose_loop_stopped_answering_fails_closed(tmp_path):
    """A worker that assumed permission because nobody answered is the single
    worst bug this file could contain."""
    events: queue.Queue = queue.Queue()
    channel = QueueChannel(events, timeout_s=0.05)
    verdict = channel.ask(
        StepRequest(job_id="j", step_index=1, action=_action(), reply=queue.Queue(maxsize=1))
    )
    assert verdict.warrant is None
    assert verdict.status is JobStatus.EXPIRED


def test_the_finish_notification_rides_the_same_queue_as_the_requests(tmp_path):
    """This is what makes the threaded tests deterministic: a loop draining
    this queue is woken by the job ending, so there is no polling interval to
    tune and no sleep to flake on."""
    events: queue.Queue = queue.Queue()
    channel = QueueChannel(events)
    registry = JobRegistry(tmp_path / "j.jsonl", now=Clock())
    job = registry.create(goal="x")
    runner = JobRunner(
        job_id=job.id,
        stream=_stream(1),
        ask=channel.ask,
        perform=lambda a, v, i: ToolResult(ok=True, summary=""),
        registry=registry,
        cancel=registry.cancel_event(job.id),
        finished=channel.finish,
    )
    thread = threading.Thread(target=runner.drive, daemon=True)
    thread.start()

    first = events.get(timeout=5.0)
    assert isinstance(first, StepRequest)
    first.reply.put(StepVerdict(warrant=_warrant()))
    second = events.get(timeout=5.0)
    assert isinstance(second, JobFinished)
    assert second.status is JobStatus.DONE
    thread.join(timeout=5.0)
    assert not thread.is_alive()
