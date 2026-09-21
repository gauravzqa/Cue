"""Tests for the audit log.

Two things are being defended here, and only one of them is obvious.

The obvious one: the log must exist, be append-only, and record every judgment,
disposition, confirmation and execution.

The other: this file accumulates a record of someone's whole day. A test that
only checks "the event was written" would happily pass while the log quietly
accumulated the contents of everything the user copied, read or dictated. So
several tests here assert on what is ABSENT.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import (
    AuditEvent,
    AuditSink,
    Disposition,
    ResolvedAction,
    RiskAssessment,
    RiskTier,
    ToolSpec,
    UndoAction,
)
from daa.safety import policy
from daa.safety.audit import (
    DEFAULT_AUDIT_PATH,
    EVENT_KINDS,
    JsonlAudit,
    MemoryAudit,
    NullAudit,
    confirmation_event,
    disposition_event,
    execution_event,
    judgment_event,
    undo_event,
)

ACTION = ResolvedAction(
    tool="move_to_trash",
    args={"paths": ["/Users/x/Desktop/Screenshot.png"]},
    targets=("Screenshot 2026-09-20.png",),
)
SPEC = ToolSpec(name="move_to_trash", description="", params={}, floor=RiskTier.ANNOUNCE)


@pytest.fixture()
def sink(tmp_path: Path) -> JsonlAudit:
    return JsonlAudit(tmp_path / "logs" / "audit.jsonl")


def lines(sink: JsonlAudit) -> list[dict]:
    return [json.loads(line) for line in sink.path.read_text().splitlines()]


# --- the sink contract ------------------------------------------------------


def test_satisfies_the_audit_sink_contract(sink: JsonlAudit) -> None:
    for candidate in (sink, NullAudit(), MemoryAudit()):
        assert callable(candidate), "AuditSink is Callable[[AuditEvent], None]"

    def takes_a_sink(s: AuditSink) -> None:
        s(AuditEvent(kind="ping", payload={}))

    takes_a_sink(sink)
    takes_a_sink(NullAudit())
    takes_a_sink(MemoryAudit())


def test_default_path_is_under_dot_daa() -> None:
    assert DEFAULT_AUDIT_PATH == Path.home() / ".daa" / "audit.jsonl"
    assert JsonlAudit(None).path == DEFAULT_AUDIT_PATH


def test_null_audit_writes_nothing_anywhere(tmp_path: Path) -> None:
    NullAudit()(AuditEvent(kind="x", payload={"a": 1}))
    assert list(tmp_path.iterdir()) == []


def test_is_append_only(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="one", payload={}))
    first = sink.path.read_text()
    sink(AuditEvent(kind="two", payload={}))
    assert sink.path.read_text().startswith(first)
    assert [r["kind"] for r in lines(sink)] == ["one", "two"]


def test_creates_parent_directories_and_one_line_per_event(sink: JsonlAudit) -> None:
    for i in range(3):
        sink(AuditEvent(kind="e", payload={"i": i, "note": "multi\nline\nvalue"}))
    assert len(sink.path.read_text().splitlines()) == 3
    assert [r["payload"]["i"] for r in lines(sink)] == [0, 1, 2]


def test_records_carry_time_and_id(sink: JsonlAudit) -> None:
    before = time.time()
    sink(AuditEvent(kind="e", payload={}))
    row = lines(sink)[0]
    assert row["at"] >= before
    assert len(row["id"]) == 12


def test_read_all_round_trips(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="e", payload={"n": 1}))
    assert sink.read_all()[0]["payload"]["n"] == 1
    assert JsonlAudit(sink.path.parent / "nothing.jsonl").read_all() == []


def test_a_write_failure_never_aborts_the_caller(tmp_path: Path) -> None:
    """A full disk must not be able to stop a mutation or an undo record."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad = JsonlAudit(blocker / "audit.jsonl")
    bad(AuditEvent(kind="e", payload={}))
    assert bad.errors == 1 and bad.last_error


def test_enums_are_logged_by_name_not_number(sink: JsonlAudit) -> None:
    """The numbers can be renumbered; a log read a year later cannot."""
    sink(AuditEvent(kind="disposition", payload={"tier": RiskTier.CONFIRM_VISUAL}))
    assert lines(sink)[0]["payload"]["tier"] == "CONFIRM_VISUAL"


def test_dataclasses_and_paths_are_flattened(sink: JsonlAudit) -> None:
    sink(
        AuditEvent(
            kind="execution",
            payload={"undo": UndoAction("put it back", "move_file", {"n": 1}), "p": Path("/tmp")},
        )
    )
    row = lines(sink)[0]["payload"]
    assert row["undo"]["tool"] == "move_file"
    assert row["p"] == "/tmp"


# --- synthetic provenance ---------------------------------------------------


def test_synthetic_is_always_present(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="e", payload={}))
    assert lines(sink)[0]["synthetic"] is False


def test_synthetic_is_hoisted_from_a_nested_assessment(sink: JsonlAudit) -> None:
    """An eval sweep writes the same event kinds as a live session. Six months
    later, a grep for '"synthetic":true' has to be enough to tell them apart."""
    a = RiskAssessment(1.0, 0.0, 1.0, "certain", 0.9, synthetic=True)
    sink(disposition_event(ACTION, Disposition(RiskTier.ANNOUNCE, "ok.", a)))
    assert lines(sink)[0]["synthetic"] is True


def test_synthetic_is_found_however_deeply_it_is_nested(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="e", payload={"a": {"b": [{"c": {"synthetic": True}}]}}))
    assert lines(sink)[0]["synthetic"] is True


def test_a_live_assessment_is_not_marked_synthetic(sink: JsonlAudit) -> None:
    a = RiskAssessment(1.0, 0.0, 1.0, "certain", 0.9, synthetic=False)
    sink(judgment_event(ACTION, a, latency_ms=12.0))
    row = lines(sink)[0]
    assert row["synthetic"] is False
    assert row["payload"]["assessment"]["synthetic"] is False


def test_a_real_decide_call_on_a_synthetic_assessment_stays_marked(sink: JsonlAudit) -> None:
    a = RiskAssessment(3.0, 0.9, 0.1, "guessing", 0.2, synthetic=True)
    d = policy.decide(ACTION, SPEC, a, Settings(dry_run=False))
    sink(disposition_event(ACTION, d))
    row = lines(sink)[0]
    assert row["synthetic"] is True
    assert row["payload"]["tier"] == "CONFIRM_VISUAL"


# --- privacy ----------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["clipboard", "content", "contents", "file_contents", "text", "body", "stdout", "transcript"],
)
def test_content_bearing_keys_are_redacted(sink: JsonlAudit, key: str) -> None:
    secret = "the launch code is 0451 and my password is hunter2"
    sink(AuditEvent(kind="execution", payload={key: secret, "path": "/tmp/notes.txt"}))
    blob = sink.path.read_text()
    assert "0451" not in blob and "hunter2" not in blob
    assert "/tmp/notes.txt" in blob, "paths are the point of the log"
    assert lines(sink)[0]["payload"][key] == f"<redacted {len(secret)} chars>"


def test_redaction_keeps_the_shape_so_the_log_is_still_debuggable(sink: JsonlAudit) -> None:
    """A container under a redacted key keeps its shape and loses its contents.

    Swapping the whole list for "<redacted 3 items>" was safe but told you
    nothing; redacting element by element says how many, of what size, and
    under which keys -- which is what you actually need when reading the log
    back -- while every leaf is still gone.
    """
    sink(AuditEvent(kind="e", payload={"contents": ["aa", "b", "c"], "body": {"x": 1}}))
    row = lines(sink)[0]["payload"]
    assert row["contents"] == ["<redacted 2 chars>", "<redacted 1 chars>", "<redacted 1 chars>"]
    assert row["body"] == {"x": "<redacted int>"}


def test_redaction_reaches_nested_structures(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="e", payload={"a": {"b": [{"clipboard": "SECRETVALUE"}]}}))
    assert "SECRETVALUE" not in sink.path.read_text()


def test_long_strings_are_elided_whatever_they_are_called(sink: JsonlAudit) -> None:
    """The leak path is always a key nobody thought of. Length is the backstop."""
    sink(AuditEvent(kind="e", payload={"harmless_looking_key": "L" * 5000}))
    row = lines(sink)[0]["payload"]["harmless_looking_key"]
    assert row == "<elided 5000 chars>"
    assert len(sink.path.read_text()) < 500


def test_ordinary_short_strings_survive(sink: JsonlAudit) -> None:
    """Redaction that ate everything would make the log useless, which is the
    other way to end up with no audit trail."""
    sink(disposition_event(ACTION, Disposition(RiskTier.CONFIRM_VOICE, "I'm checking first.")))
    row = lines(sink)[0]["payload"]
    assert row["reason"] == "I'm checking first."
    assert row["targets"] == ["Screenshot 2026-09-20.png"]
    assert row["target_count"] == 1


def test_context_is_not_mistaken_for_content(sink: JsonlAudit) -> None:
    """'text' is redacted; 'context' must not be -- substring matching on key
    names would silently blind the log."""
    sink(AuditEvent(kind="e", payload={"context": {"app": "Finder"}, "target_count": 4}))
    row = lines(sink)[0]["payload"]
    assert row["context"] == {"app": "Finder"}
    assert row["target_count"] == 4


def test_memory_audit_sees_exactly_what_disk_would(tmp_path: Path) -> None:
    """A privacy test that passes in memory must also hold on disk."""
    mem = MemoryAudit()
    disk = JsonlAudit(tmp_path / "audit.jsonl")
    event = AuditEvent(kind="e", payload={"clipboard": "SECRET", "path": "/tmp/x"})
    mem(event)
    disk(event)
    assert mem.records == disk.read_all()
    assert mem.kinds() == ["e"]


# --- the four things that must always be logged -----------------------------


def test_the_whole_lifecycle_is_recordable(sink: JsonlAudit) -> None:
    a = RiskAssessment(2.0, 0.9, 1.0, "certain", 0.8, synthetic=True)
    d = policy.decide(ACTION, SPEC, a, Settings(dry_run=False))
    sink(judgment_event(ACTION, a, latency_ms=31.4))
    sink(disposition_event(ACTION, d))
    sink(confirmation_event(ACTION, tier=d.tier, granted=True, via="visual", confidence=0.97))
    sink(
        execution_event(
            ACTION,
            ok=True,
            summary="Moved one screenshot to the trash.",
            undo=UndoAction("put it back", "move_file", {"n": 1}),
        )
    )
    sink(undo_event(tool="move_file", ok=True, entry_id="abc123"))
    rows = lines(sink)
    assert [r["kind"] for r in rows] == [
        "judgment",
        "disposition",
        "confirmation",
        "execution",
        "undo",
    ]
    assert rows[1]["payload"]["tier"] == "CONFIRM_VISUAL"
    assert rows[2]["payload"]["granted"] is True and rows[2]["payload"]["via"] == "visual"
    assert rows[3]["payload"]["undoable"] is True
    # Judgment and disposition both came off a synthetic answer; the execution
    # and the confirmation did not claim to.
    assert [r["synthetic"] for r in rows] == [True, True, False, False, False]


def test_a_missing_assessment_is_still_a_judgment_worth_logging(sink: JsonlAudit) -> None:
    """The Jev-is-down case is the one you most want in the log afterwards."""
    sink(judgment_event(ACTION, None))
    row = lines(sink)[0]
    assert row["payload"]["assessment"] is None
    assert row["synthetic"] is False


def test_execution_records_counts_and_never_results(sink: JsonlAudit) -> None:
    sink(
        execution_event(
            ACTION,
            ok=False,
            error="the file was already gone",
            dry_run=True,
            summary="I couldn't find it.",
        )
    )
    row = lines(sink)[0]["payload"]
    assert row["ok"] is False and row["dry_run"] is True and row["undoable"] is False
    assert row["target_count"] == 1


def test_a_refused_confirmation_is_logged_too(sink: JsonlAudit) -> None:
    """'The user said no' is the single most useful line in an incident log."""
    sink(confirmation_event(ACTION, tier=RiskTier.CONFIRM_VOICE, granted=False, via="voice"))
    assert lines(sink)[0]["payload"]["granted"] is False


# --- the leaks that were confirmed in a real audit.jsonl ---------------------


@pytest.mark.parametrize(
    "key", ["before", "after", "reply", "summary", "error", "input_text", "script", "purpose"]
)
def test_the_keys_a_spoken_card_number_was_actually_found_under(
    sink: JsonlAudit, key: str
) -> None:
    """Every one of these was carrying user content into the log.

    `before`/`after` are the raw transcript either side of a cloud re-score,
    `reply` is whatever the user said when asked to confirm, `summary` and
    `error` are written by tools that quote their arguments, and input_text /
    script / purpose are arguments to the two tools that take arbitrary text
    and arbitrary code.
    """
    spoken = "my card is 4111 1111 1111 1111 and the pin is 9crimson"
    sink(AuditEvent(kind="e", payload={key: spoken, "tool": "set_clipboard"}))
    blob = sink.path.read_text()
    assert "4111" not in blob and "9crimson" not in blob
    assert lines(sink)[0]["payload"]["tool"] == "set_clipboard", "references still survive"


def test_a_list_under_a_redacted_key_is_redacted_element_by_element(sink: JsonlAudit) -> None:
    """Replacing the whole container was safe; it was also the only thing that
    was safe. A dict UNDER a redacted key has to lose its values too."""
    sink(
        AuditEvent(
            kind="e",
            payload={"reply": ["yes, 4111111111111111", {"note": "and hunter2"}]},
        )
    )
    blob = sink.path.read_text()
    assert "4111111111111111" not in blob and "hunter2" not in blob
    row = lines(sink)[0]["payload"]["reply"]
    assert isinstance(row, list) and len(row) == 2, "the shape is kept"
    assert row[1] == {"note": "<redacted 11 chars>"}


def test_content_survives_no_depth_of_nesting_under_a_redacted_key(sink: JsonlAudit) -> None:
    sink(AuditEvent(kind="e", payload={"body": {"parts": [{"deep": ["SECRETVALUE"]}]}}))
    assert "SECRETVALUE" not in sink.path.read_text()


def test_a_card_number_is_redacted_even_under_a_key_nobody_classified(
    sink: JsonlAudit,
) -> None:
    """THE `targets` LEAK. set_clipboard's resolver puts a preview of its own
    argument into ResolvedAction.targets, so "copy my card number" walks a real
    card number into a field that exists to hold speakable filenames. `targets`
    cannot simply be redacted -- it is the only human-readable record of WHAT
    was acted on -- so the value has to be caught by its shape instead."""
    action = ResolvedAction(
        tool="set_clipboard",
        args={"text": "4111 1111 1111 1111"},
        targets=("4111 1111 1111 1111",),
        verb="copy",
    )
    sink(execution_event(action, ok=True, summary="Copied."))
    blob = sink.path.read_text()
    assert "4111" not in blob
    assert lines(sink)[0]["payload"]["targets"] == ["<redacted card number>"]


def test_shape_redaction_does_not_eat_ordinary_filenames(sink: JsonlAudit) -> None:
    """Redaction that ate the filenames would leave a log nobody can read,
    which is the other way to end up with no audit trail."""
    sink(AuditEvent(kind="e", payload={"targets": ["Screenshot 2026-09-20 at 11.04.55.png"]}))
    assert lines(sink)[0]["payload"]["targets"] == ["Screenshot 2026-09-20 at 11.04.55.png"]


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----",
        "123-45-6789",
    ],
)
def test_secrets_are_caught_by_shape_wherever_they_appear(sink: JsonlAudit, secret: str) -> None:
    sink(AuditEvent(kind="e", payload={"targets": [f"the value is {secret}"]}))
    assert secret not in sink.path.read_text()


# --- the log is not readable by the rest of the machine ---------------------


def test_the_audit_log_is_not_world_readable(tmp_path: Path) -> None:
    """This file is a transcript of someone's day. 0644 means every process on
    the machine can read it."""
    p = tmp_path / "daa" / "audit.jsonl"
    JsonlAudit(p)(AuditEvent(kind="e", payload={}))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_a_log_left_behind_at_0644_is_tightened_when_it_is_next_written(tmp_path: Path) -> None:
    d = tmp_path / "daa"
    d.mkdir(mode=0o755)
    p = d / "audit.jsonl"
    p.write_text('{"kind":"old"}\n')
    os.chmod(p, 0o644)
    os.chmod(d, 0o755)

    sink = JsonlAudit(p)
    sink(AuditEvent(kind="e", payload={}))

    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert len(sink.read_all()) == 2, "tightening must not cost the history"


# --- the event builders carry what the loop needs ---------------------------


def test_judgment_event_carries_the_whole_assessment(sink: JsonlAudit) -> None:
    """The RiskAssessment that authorized an action was being logged nowhere at
    all: the loop hand-rolled its payloads and kept only the tier. A tier says
    what we did; it never says what we believed, and 'why did it not ask me?'
    is unanswerable without the numbers."""
    a = RiskAssessment(2.25, 0.8, 0.1, "guessing", 0.42, synthetic=True)
    sink(judgment_event(ACTION, a, latency_ms=31.4))
    row = lines(sink)[0]["payload"]
    assert row["blast_radius"] == 2.25
    assert row["unrecoverable"] == 0.8
    assert row["explicitly_requested"] == 0.1
    assert row["target_confidence"] == "guessing"
    assert row["confidence"] == 0.42
    assert row["synthetic"] is True
    assert row["explicit"] is True
    assert row["assessment"]["blast_radius"] == 2.25
    assert lines(sink)[0]["synthetic"] is True


def test_judgment_event_without_an_assessment_says_so_in_every_field(sink: JsonlAudit) -> None:
    sink(judgment_event(ACTION, None))
    row = lines(sink)[0]["payload"]
    assert row["blast_radius"] is None and row["target_confidence"] is None
    assert row["synthetic"] is False


def test_disposition_event_keeps_the_assessment_beside_the_tier(sink: JsonlAudit) -> None:
    a = RiskAssessment(0.2, 0.0, 1.0, "certain", 0.9)
    d = policy.decide(ACTION, SPEC, a, Settings(dry_run=False))
    sink(disposition_event(ACTION, d))
    row = lines(sink)[0]["payload"]
    assert row["tier"] == "ANNOUNCE"
    assert row["assessment"]["target_confidence"] == "certain"


def test_the_builders_speak_the_verb_and_consequences_the_user_heard(sink: JsonlAudit) -> None:
    """The readback is the consent. If the log does not record what was said,
    it cannot answer what the user agreed to."""
    action = ResolvedAction(
        tool="move_files",
        args={},
        targets=("budget.xlsx",),
        verb="move to the Desktop",
        consequences={"overwrite": "replacing 1 file that is already there"},
    )
    sink(judgment_event(action, None))
    row = lines(sink)[0]["payload"]
    assert row["verb"] == "move to the Desktop"
    assert row["consequences"]["overwrite"] == "replacing 1 file that is already there"


def test_undo_is_a_first_class_event_kind() -> None:
    assert "undo" in EVENT_KINDS
    assert undo_event(tool="move_files", ok=True).kind == "undo"
    # Pinned rather than open-ended: a new kind should be a deliberate edit
    # here, because EVENT_KINDS is what `daa audit` and the readers downstream
    # of it are written against.
    assert set(EVENT_KINDS) == {
        "judgment",
        "disposition",
        "confirmation",
        "execution",
        "undo",
        "grant",
        "grant_revoked",
        "job",
        "job_step",
        "checkpoint",
        "rollback",
        "notice",
    }


def test_undo_event_records_the_provenance_check_and_whether_it_was_consumed(
    sink: JsonlAudit,
) -> None:
    """An undo line that says only "we ran move_files" cannot answer the two
    questions that matter afterwards: did the journal row check out, and is the
    entry still there to try again?"""
    sink(
        undo_event(
            tool="run_shell",
            ok=False,
            entry_id="forged",
            produced_by=None,
            trusted=False,
            description="put it back",
            committed=False,
            dry_run=True,
        )
    )
    row = lines(sink)[0]["payload"]
    assert row["trusted"] is False and row["produced_by"] is None
    assert row["committed"] is False, "a refused undo must still be retryable"
    assert row["dry_run"] is True


def test_execution_event_records_the_tier_that_authorized_it(sink: JsonlAudit) -> None:
    sink(execution_event(ACTION, ok=True, tier=RiskTier.CONFIRM_VOICE, undo_id="abc123"))
    row = lines(sink)[0]["payload"]
    assert row["tier"] == "CONFIRM_VOICE"
    assert row["undo_id"] == "abc123"
