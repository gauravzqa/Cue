"""The audit rows that make "who authorized what" answerable.

The reconstruction requirement is a single query on `grant_id`, so every row a
grant touches carries it at the TOP LEVEL of the payload -- the same convention
`synthetic` already follows, and for the same reason: the questions asked six
months later should not need a JSON path.
"""

from __future__ import annotations

from daa.contracts import (
    Budget,
    Disposition,
    Grant,
    GrantScope,
    JobStatus,
    ResolvedAction,
    RiskAssessment,
    RiskTier,
)
from daa.safety.audit import (
    EVENT_KINDS,
    MAX_STRING,
    MemoryAudit,
    checkpoint_event,
    confirmation_event,
    disposition_event,
    execution_event,
    grant_event,
    grant_revoked_event,
    job_event,
    job_step_event,
    judgment_event,
    notice_event,
    record,
    rollback_event,
)

ACTION = ResolvedAction(tool="move_files", args={}, targets=("a.txt",), verb="move")
ASSESSMENT = RiskAssessment(
    blast_radius=1.0,
    unrecoverable=0.1,
    explicitly_requested=1.0,
    target_confidence="certain",
    confidence=0.9,
)
LONG_READBACK = (
    "You asked me to clear out the downloads folder. The most it could do is "
    "move files to the Trash, and some of them I'll have to guess about. I'll "
    "stay only in Downloads, under 60 steps, for about three minutes, and I "
    "won't spend anything. I'll still stop and ask before anything leaves the "
    "Trash for good. Say stop any time. Okay?"
)


def _grant() -> Grant:
    return Grant(
        id="g1",
        goal="clear out downloads",
        plan_summary=LONG_READBACK,
        ceiling=RiskTier.CONFIRM_VOICE,
        granted_via="voice",
        scope=GrantScope(
            tools=frozenset({"move_files", "move_to_trash"}),
            origins=frozenset({"https://mail.google.com"}),
            path_prefixes=("/Users/x/Downloads",),
        ),
        budget=Budget(steps=60, seconds=180.0, spend_cents=0),
        granted_at=1000.0,
        expires_at=1300.0,
        briefed_consequences=("overwrite",),
    )


def _payload(event) -> dict:
    return record(event)["payload"]


def test_the_spoken_readback_is_stored_verbatim():
    """A scope object answers "what was permitted". Only the sentence the user
    actually heard answers "what did they consent to", and MAX_STRING would
    otherwise elide exactly that."""
    assert len(LONG_READBACK) > MAX_STRING, "this test is pointless on a short readback"
    row = _payload(grant_event(_grant(), granted=True))
    assert row["plan_summary"] == LONG_READBACK


def test_a_verbatim_field_is_still_scrubbed_for_secrets():
    grant = _grant()
    grant = Grant(
        **{
            **{f: getattr(grant, f) for f in grant.__slots__},
            "plan_summary": "I'll pay with 4111 1111 1111 1111, okay?",
        }
    )
    row = _payload(grant_event(grant, granted=True))
    assert "4111" not in row["plan_summary"]
    assert "<redacted card number>" in row["plan_summary"]


def test_a_verbatim_field_is_still_capped():
    grant = _grant()
    grant = Grant(
        **{
            **{f: getattr(grant, f) for f in grant.__slots__},
            "plan_summary": "x" * 5000,
        }
    )
    row = _payload(grant_event(grant, granted=True))
    assert row["plan_summary"].startswith("<elided")


def test_the_goal_is_not_verbatim_because_it_is_the_users_own_words():
    grant = _grant()
    grant = Grant(
        **{
            **{f: getattr(grant, f) for f in grant.__slots__},
            "goal": "y" * 5000,
        }
    )
    row = _payload(grant_event(grant, granted=True))
    assert row["goal"].startswith("<elided")


def test_the_grant_row_carries_the_whole_bargain():
    row = _payload(grant_event(_grant(), granted=True, tools=("move_files", "send_email")))
    assert row["grant_id"] == "g1"
    assert row["ceiling"] == "CONFIRM_VOICE"
    assert row["channel_cap"] == "CONFIRM_VOICE"
    assert row["max_satisfiable"] == "CONFIRM_VOICE"
    assert row["scope_tools"] == ["move_files", "move_to_trash"]
    assert row["scope_origins"] == ["https://mail.google.com"]
    assert row["scope_paths"] == ["/Users/x/Downloads"]
    assert row["budget_steps"] == 60
    assert row["budget_spend_cents"] == 0
    assert row["briefed_consequences"] == ["overwrite"]
    assert row["offered_tools"] == ["move_files", "send_email"]


def test_a_refused_grant_is_recorded_too():
    row = _payload(grant_event(_grant(), granted=False))
    assert row["granted"] is False


def test_every_step_row_carries_the_linkage_at_the_top_level():
    """One query on `grant_id` has to reach the judgment, the disposition, the
    confirmation and the execution."""
    link = {"grant_id": "g1", "warrant_id": "w1", "job_id": "j1", "step_index": 4}
    for event in (
        judgment_event(ACTION, ASSESSMENT, **link),
        disposition_event(ACTION, Disposition(tier=RiskTier.ANNOUNCE, reason="x"), **link),
        confirmation_event(ACTION, tier=RiskTier.CONFIRM_VOICE, granted=True, via="grant", **link),
        execution_event(ACTION, ok=True, **link),
    ):
        row = _payload(event)
        for key, value in link.items():
            assert row[key] == value, f"{event.kind} lost {key}"


def test_the_linkage_is_none_on_an_ordinary_foreground_action():
    """None is the honest answer: that action ran under a fresh confirmation,
    not under a grant."""
    row = _payload(judgment_event(ACTION, ASSESSMENT))
    assert row["grant_id"] is None
    assert row["job_id"] is None


def test_confirmation_via_grant_is_distinguishable_from_a_real_yes():
    """A run that is all `via="grant"` rows under one grant is exactly the
    habituation failure the design worries about, and this is what makes it
    countable."""
    rows = [
        _payload(confirmation_event(ACTION, tier=RiskTier.CONFIRM_VOICE, granted=True, via=v))
        for v in ("voice", "visual", "grant", "implicit")
    ]
    assert [r["via"] for r in rows] == ["voice", "visual", "grant", "implicit"]


def test_the_new_kinds_are_first_class():
    for kind in ("grant", "grant_revoked", "job", "job_step", "checkpoint", "rollback", "notice"):
        assert kind in EVENT_KINDS


def test_a_notice_row_never_carries_the_text():
    """A notice is written to be spoken, which means it quotes whatever the job
    was working on, which makes it content."""
    row = _payload(notice_event(job_id="j1", spoken=True))
    assert "text" not in row
    assert row["spoken"] is True


def test_a_rollback_row_separates_attempted_from_reversed():
    """A rollback that stops at the first stale row is CORRECT behaviour, and a
    log that only said "rollback happened" could not tell that apart from one
    that reversed everything."""
    row = _payload(
        rollback_event(
            checkpoint_id="cp1",
            attempted=6,
            reversed_count=4,
            stopped_reason="they've changed since",
            sealed=True,
        )
    )
    assert (row["attempted"], row["reversed"]) == (6, 4)
    assert row["sealed"] is True


def test_the_remaining_builders_produce_their_kinds():
    sink = MemoryAudit()
    sink(grant_revoked_event(grant_id="g1", reason="you said stop", steps_completed=3))
    sink(job_event(job_id="j1", status=JobStatus.DONE, goal="x"))
    sink(job_step_event(job_id="j1", step_index=1, tool="move_files"))
    sink(checkpoint_event(checkpoint_id="cp1", opened=True))
    assert sink.kinds() == ["grant_revoked", "job", "job_step", "checkpoint"]
    assert sink.records[1]["payload"]["status"] == "DONE", "enums log as their NAME"
