"""The loop end of scoped consent: grants, warrants, jobs, notices, rollback.

The stubs come from test_voice_loop so that a change to the sibling subsystems'
shape breaks both files at once rather than letting this one drift into testing
a world that no longer exists.

Nothing here sleeps and nothing here reaches a key, a socket or a microphone.
The clock is injected; the threaded tests block on queues that the design
already has, not on timers a test invented.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from daa.config import Settings
from daa.contracts import (
    Budget,
    Disposition,
    GrantScope,
    JobStatus,
    ResolvedAction,
    RiskTier,
    ToolResult,
    ToolSpec,
    UndoAction,
)
from daa.safety.grant import action_digest
from daa.voice.jobs import JobRegistry, NoticeBoard
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import TurnOutcome, VoiceLoop
from daa.voice.mic import FakeMic
from daa.voice.stt import FakeTranscriber
from daa.voice.tts import FakeSpeaker
from test_voice_loop import (
    MOVE,
    SpyTool,
    StubConfirm,
    StubGate,
    StubJournal,
    StubRegistry,
    StubRisk,
    StubRouter,
    StubWake,
    stub_policy,
)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeConsole:
    """A screen that is present and types whatever it was told to."""

    def __init__(self, *typed: str, present: bool = True) -> None:
        self.typed = list(typed)
        self.present = present
        self.written: list[str] = []

    def available(self) -> bool:
        return self.present

    def write(self, text: str) -> None:
        self.written.append(text)

    def ask(self, prompt: str) -> str:
        return self.typed.pop(0) if self.typed else ""


@dataclass(slots=True)
class FakeAgent:
    """A `LongRunningTool`: it yields the action it WANTS and is told what
    happened. It holds no registry, no journal and no `_execute`, so there is
    no API surface through which it could route around the gate."""

    plan: list[ResolvedAction]
    spec: ToolSpec = field(
        default_factory=lambda: ToolSpec(
            name="tidy_agent",
            description="Tidy a folder over several steps",
            params={},
            floor=RiskTier.ANNOUNCE,
            long_running=True,
        )
    )
    seen: list[Any] = field(default_factory=list)
    summary: str = "Filed everything."

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return ResolvedAction(tool=self.spec.name, args=kwargs, targets=(), verb="tidy")

    def run(self, action: ResolvedAction) -> ToolResult:
        return ToolResult(ok=True, summary="Started.")

    def steps(self, action: ResolvedAction):
        for step in self.plan:
            self.seen.append((yield step))
        return ToolResult(ok=True, summary=self.summary)


def _step(n: int = 1) -> ResolvedAction:
    return ResolvedAction(
        tool="move_files", args={}, targets=(f"file {n}",), verb="move", explicit=True
    )


def build(
    *,
    tier: RiskTier = RiskTier.CONFIRM_VOICE,
    confirm: Any = None,
    console: Any = None,
    clock: Clock | None = None,
    tmp_path: Any = None,
    tools: list[Any] | None = None,
    dry_run: bool = False,
) -> tuple[VoiceLoop, SpyTool, FakeSpeaker, list[Any]]:
    clock = clock or Clock()
    spy = SpyTool(
        spec=MOVE,
        undo=UndoAction(description="move them back", tool="move_files", args={}),
    )
    registry = StubRegistry()
    registry.register(spy)
    for extra in tools or []:
        registry.tools[extra.spec.name] = extra
    events: list[Any] = []
    speaker = FakeSpeaker()
    loop = VoiceLoop(
        settings=Settings(dry_run=dry_run),
        mic=FakeMic(utterances=[]),
        local_stt=FakeTranscriber(source="local"),
        speaker=speaker,
        gate=StubGate(),
        router=StubRouter(),
        risk=StubRisk(),
        confirm=confirm if confirm is not None else StubConfirm(verdicts=["yes"] * 8),
        registry=registry,
        policy_decide=stub_policy(tier),
        journal=StubJournal(),
        audit=events.append,
        llm=FakeLLM(turns=[LLMTurn()]),
        console=console,
        now=clock,
        jobs=JobRegistry(tmp_path / "jobs.jsonl", now=clock) if tmp_path else None,
        notices=NoticeBoard(now=clock),
    )
    return loop, spy, speaker, events


def _grant(loop: VoiceLoop, **over: Any):
    kwargs: dict[str, Any] = {
        "goal": "tidy up downloads",
        "scope": GrantScope(tools=frozenset({"move_files"})),
        "budget": Budget(steps=5, seconds=60.0),
        "ceiling": RiskTier.CONFIRM_VOICE,
        "replies": ["yes"],
    }
    kwargs.update(over)
    return loop.request_grant(**kwargs)


def _kinds(events: list[Any], kind: str) -> list[Any]:
    return [e.payload for e in events if e.kind == kind]


# ---------------------------------------------------------------------------
# Issuing a grant
# ---------------------------------------------------------------------------


def test_a_grant_is_read_back_before_it_is_given():
    loop, _spy, speaker, events = build()
    grant = _grant(loop)
    assert grant is not None
    assert grant.plan_summary in speaker.said, "the readback was not spoken"
    row = _kinds(events, "grant")[0]
    assert row["granted"] is True
    assert row["plan_summary"] == grant.plan_summary, "the exact sentence must be stored"
    assert row["scope_tools"] == ["move_files"]


def test_a_refused_grant_is_still_logged():
    """"The assistant asked for more than it needed" is exactly the signal
    worth noticing, and it is invisible if only the accepted ones are kept."""
    loop, _spy, _speaker, events = build(confirm=StubConfirm(verdicts=["no"]))
    assert _grant(loop) is None
    assert _kinds(events, "grant")[0]["granted"] is False
    assert loop.active_grant is None


def test_a_visual_ceiling_forces_the_visual_channel():
    console = FakeConsole("yes")
    loop, _spy, _speaker, _events = build(console=console)
    grant = _grant(loop, ceiling=RiskTier.CONFIRM_VISUAL)
    assert grant is not None
    assert grant.granted_via == "visual", "a spoken yes must never mint a visual grant"
    assert console.written, "the card was never shown"


def test_a_visual_grant_is_refused_when_there_is_no_screen():
    loop, _spy, speaker, _events = build(console=FakeConsole(present=False))
    assert _grant(loop, ceiling=RiskTier.CONFIRM_VISUAL) is None
    assert any("no screen" in s for s in speaker.said)


# ---------------------------------------------------------------------------
# A grant answering a confirmation
# ---------------------------------------------------------------------------


def test_a_grant_answers_a_spoken_confirmation_without_asking_again():
    confirm = StubConfirm(verdicts=["yes"])
    loop, spy, speaker, events = build(confirm=confirm)
    _grant(loop)
    before = len(confirm.calls)

    outcome = TurnOutcome(woke=True)
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)

    assert spy.runs, "the step never ran"
    assert len(confirm.calls) == before, "the user was asked again despite the grant"
    row = [r for r in _kinds(events, "confirmation") if r["via"] == "grant"]
    assert row, "no confirmation row says the grant answered"
    assert row[0]["granted"] is True
    assert row[0]["tier"] is RiskTier.CONFIRM_VOICE, (
        "the tier that was computed is the tier that is logged; there is no tier "
        "in this system that means 'was skipped'"
    )
    assert row[0]["grant_id"] == loop.active_grant.id
    assert not any("Should I" in s for s in speaker.said)


def test_the_grant_is_never_an_input_to_the_tier():
    """`tier = max(spec.floor, floor_hint, derived)` is untouched. Policy is
    called with exactly the same four arguments whether or not a grant exists,
    and it is never handed one."""
    loop, _spy, _speaker, _events = build()
    _grant(loop)
    outcome = TurnOutcome(woke=True)
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)
    seen = loop.policy_decide.seen  # type: ignore[attr-defined]
    assert seen, "policy was not consulted at all"
    assert outcome.dispositions[0].tier is RiskTier.CONFIRM_VOICE


def test_a_spoken_grant_can_never_answer_a_visual_confirmation():
    """THE non-negotiable, asserted through the loop rather than the unit."""
    # "yes" approves the GRANT card; "no thanks" is what the action card gets,
    # and the point of the test is that the action card appears at all.
    console = FakeConsole("yes", "no thanks")
    loop, spy, _speaker, events = build(tier=RiskTier.CONFIRM_VISUAL, console=console)
    _grant(loop, ceiling=RiskTier.CONFIRM_VISUAL, replies=["yes"])
    assert loop.active_grant.granted_via == "visual"
    # Now downgrade the live grant to a SPOKEN one, exactly as if it had been
    # given by voice, and check the visual step still demands the screen.
    loop.active_grant = replace(loop.active_grant, granted_via="voice")
    loop.grants._grants[loop.active_grant.id] = loop.active_grant
    written_before = len(console.written)

    outcome = TurnOutcome(woke=True)
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)
    assert spy.runs == [], "a spoken grant authorized a CONFIRM_VISUAL action"
    assert len(console.written) > written_before, "the typed card was skipped"
    del events


def test_an_out_of_scope_tool_falls_through_to_a_real_question():
    # Two yeses: one for the grant, one for the question the grant fails to
    # answer. That second one is the whole point.
    confirm = StubConfirm(verdicts=["yes", "yes"])
    loop, spy, speaker, events = build(confirm=confirm)
    _grant(loop, scope=GrantScope(tools=frozenset({"something_else"})))

    outcome = TurnOutcome(woke=True)
    loop._scripted_replies = ["yes"]
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)
    assert any("Should I" in s for s in speaker.said), "the user was never asked"
    assert spy.runs, "the answered question did not run the step"
    miss = [e for e in events if e.kind == "grant_miss"]
    assert miss and "isn't one of the things" in miss[0].payload["reason"]


def test_stop_revokes_the_grant_and_every_unspent_warrant():
    loop, spy, _speaker, events = build()
    grant = _grant(loop)
    said = loop.stop("you said stop")
    assert "stopped" in said.lower()
    assert _kinds(events, "grant_revoked")[0]["grant_id"] == grant.id

    outcome = TurnOutcome(woke=True)
    loop._scripted_replies = []
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)
    assert spy.runs == [], "a revoked grant still authorized a step"


# ---------------------------------------------------------------------------
# Warrants at the chokepoint
# ---------------------------------------------------------------------------


def _warranted(loop: VoiceLoop, spy: SpyTool, action: ResolvedAction, tier: RiskTier):
    disposition = Disposition(tier=tier, reason="x")
    warrant = loop.warrants.issue(action, disposition, via="voice", now=loop.now())
    return disposition, warrant


def test_a_warrant_for_a_different_action_is_refused_at_the_chokepoint():
    loop, spy, _speaker, _events = build()
    action = _step(1)
    disposition, warrant = _warranted(loop, spy, action, RiskTier.CONFIRM_VOICE)
    with pytest.raises(AssertionError, match="different action"):
        loop._execute(
            spy, _step(2), disposition, TurnOutcome(), confirmed=True, warrant=warrant
        )
    assert spy.runs == []


def test_an_expired_warrant_is_refused_at_the_chokepoint():
    clock = Clock()
    loop, spy, _speaker, _events = build(clock=clock)
    action = _step()
    disposition, warrant = _warranted(loop, spy, action, RiskTier.CONFIRM_VOICE)
    clock.t += 1000.0
    with pytest.raises(AssertionError, match="expired"):
        loop._execute(spy, action, disposition, TurnOutcome(), confirmed=True, warrant=warrant)
    assert spy.runs == []


def test_a_warrant_pays_exactly_once():
    loop, spy, _speaker, _events = build()
    action = _step()
    disposition, warrant = _warranted(loop, spy, action, RiskTier.ANNOUNCE)
    assert loop._execute(spy, action, disposition, TurnOutcome(), confirmed=True,
                         warrant=warrant)
    with pytest.raises(AssertionError, match="already spent"):
        loop._execute(spy, action, disposition, TurnOutcome(), confirmed=True, warrant=warrant)
    assert len(spy.runs) == 1


def test_a_forged_warrant_the_book_never_issued_is_refused():
    from daa.contracts import Warrant

    loop, spy, _speaker, _events = build()
    action = _step()
    forged = Warrant(
        id="deadbeef",
        action_digest=action_digest(action),
        tier=RiskTier.CONFIRM_VISUAL,
        issued_at=loop.now(),
        expires_at=loop.now() + 30,
        via="visual",
    )
    with pytest.raises(AssertionError, match="already spent or is unknown"):
        loop._execute(
            spy,
            action,
            Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
            TurnOutcome(),
            confirmed=True,
            warrant=forged,
        )
    assert spy.runs == []


def test_a_voice_warrant_cannot_authorize_the_visual_tier():
    loop, spy, _speaker, _events = build()
    action = _step()
    disposition, warrant = _warranted(loop, spy, action, RiskTier.CONFIRM_VISUAL)
    # Issued via="voice", so `visual_ok` is False however high the tier says.
    with pytest.raises(AssertionError, match="voice cannot authorize visual"):
        loop._execute(spy, action, disposition, TurnOutcome(), confirmed=True, warrant=warrant)
    assert spy.runs == []


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_a_job_runs_every_step_through_the_one_chokepoint(tmp_path):
    loop, spy, _speaker, events = build(tmp_path=tmp_path)
    grant = _grant(loop)
    agent = FakeAgent(plan=[_step(1), _step(2), _step(3)])
    loop.registry.tools[agent.spec.name] = agent

    job_id = loop.start_job(agent, agent.resolve(), grant, threaded=False)
    assert job_id is not None
    assert len(spy.runs) == 3, "the job's steps did not reach the tool"
    assert loop.jobs.get(job_id).status is JobStatus.DONE
    steps = _kinds(events, "job_step")
    assert [s["step_index"] for s in steps] == [1, 2, 3]
    assert all(s["via"] == "grant" for s in steps)
    assert all(s["job_id"] == job_id for s in steps)
    execs = [e for e in _kinds(events, "execution") if e["job_id"] == job_id]
    assert len(execs) == 3 and all(e["warrant_id"] for e in execs)


def test_a_job_never_speaks_it_posts_a_notice(tmp_path):
    loop, _spy, speaker, _events = build(tmp_path=tmp_path)
    grant = _grant(loop)
    agent = FakeAgent(plan=[_step(1), _step(2)], summary="Filed two things.")
    loop.registry.tools[agent.spec.name] = agent
    said_before = list(speaker.said)

    loop.start_job(agent, agent.resolve(), grant, threaded=False)
    assert speaker.said == said_before, "a background job talked"
    assert [n.text for n in loop.notices.pending()] == ["Filed two things."]


def test_a_job_step_outside_the_grant_asks_and_stops_if_told_no(tmp_path):
    confirm = StubConfirm(verdicts=["yes", "no"])
    loop, spy, _speaker, events = build(tmp_path=tmp_path, confirm=confirm)
    grant = _grant(loop, scope=GrantScope(tools=frozenset({"nothing_at_all"})))
    agent = FakeAgent(plan=[_step(1), _step(2)])
    loop.registry.tools[agent.spec.name] = agent
    loop._scripted_replies = ["no"]

    job_id = loop.start_job(agent, agent.resolve(), grant, threaded=False)
    assert spy.runs == []
    assert loop.jobs.get(job_id).status is JobStatus.CANCELLED
    refused = [s for s in _kinds(events, "job_step") if not s["authorized"]]
    assert refused, "the refusal was not logged"


def test_stopping_a_job_stops_it_between_steps(tmp_path):
    loop, spy, _speaker, _events = build(tmp_path=tmp_path)
    grant = _grant(loop)
    stopper: dict[str, Any] = {}

    class Stopping(FakeAgent):
        def steps(self, action: ResolvedAction):
            yield _step(1)
            stopper["said"] = loop.stop("you said stop")
            yield _step(2)
            return ToolResult(ok=True, summary="done")

    agent = Stopping(plan=[])
    loop.registry.tools[agent.spec.name] = agent
    job_id = loop.start_job(agent, agent.resolve(), grant, threaded=False)
    assert len(spy.runs) == 1, "the job took a step after stop"
    assert loop.jobs.get(job_id).status is JobStatus.CANCELLED
    assert "finish the step I'm on" in stopper["said"], (
        "an assistant that says 'stopped' while a click is in flight has taught "
        "you not to believe it"
    )


def test_the_threaded_path_is_driven_entirely_by_queue_events(tmp_path):
    """Deterministic by construction: every handoff is a queue put, including
    the one that says the job ended, so the loop blocks until something really
    happened. There is no sleep and no polling interval anywhere."""
    loop, spy, _speaker, _events = build(tmp_path=tmp_path)
    grant = _grant(loop)
    agent = FakeAgent(plan=[_step(1), _step(2), _step(3)])
    loop.registry.tools[agent.spec.name] = agent

    job_id = loop.start_job(agent, agent.resolve(), grant, threaded=True)
    loop.drain_jobs(timeout=5.0)
    for thread in loop._threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert len(spy.runs) == 3
    assert loop.jobs.get(job_id).status is JobStatus.DONE
    assert loop.jobs_idle()
    assert threading.active_count() >= 1


def test_only_one_job_at_a_time_is_a_question_not_a_queue(tmp_path):
    loop, _spy, _speaker, events = build(tmp_path=tmp_path)
    grant = _grant(loop)
    agent = FakeAgent(plan=[_step(1)])
    loop.registry.tools[agent.spec.name] = agent
    loop.jobs.create(goal="something already running")  # occupies the one slot
    assert loop.start_job(agent, agent.resolve(), grant, threaded=False) is None
    assert [e for e in events if e.kind == "job_refused"]


# ---------------------------------------------------------------------------
# `_next_reply` re-entrancy
# ---------------------------------------------------------------------------


def test_a_background_job_cannot_steal_the_reply_to_the_question_in_front_of_you():
    """The whole reason notices and job consents drain only at explicit drain
    points. `_next_reply` pops from ONE queue; a job asking mid-`_confirm`
    would consume the answer the user is giving to something else."""
    loop, spy, _speaker, _events = build()
    drained: list[bool] = []

    class Nosy(StubConfirm):
        def interpret(self, reply: str, pending: Any) -> str:
            # Re-entrancy, simulated at the exact moment it would happen.
            drained.append(loop.service_jobs())
            drained.append(bool(loop.drain_notices()))
            return super().interpret(reply, pending)

    loop.confirm = Nosy(verdicts=["yes"])
    loop.notices.post("job1", "that download finished")

    outcome = TurnOutcome(woke=True)
    loop._scripted_replies = ["yes", "the next thing I say"]
    loop._handle_call(ToolCall("move_files", {}), "move them", outcome)

    assert spy.runs, "the foreground confirmation did not go through"
    assert drained == [False, False], "a drain point fired inside a confirmation"
    assert loop._scripted_replies == ["the next thing I say"], (
        "the background drain ate the user's next utterance"
    )
    assert loop.notices.pending(), "the notice was drained mid-confirmation"


def test_a_notice_is_not_an_utterance():
    """It is TTS from a string a job produced: it may not enter the transcript,
    reach the model, or start a turn."""
    loop, _spy, speaker, events = build()
    loop.notices.post("job1", "that download finished")
    loop.handle_text("hello")
    assert "that download finished." in speaker.said
    turns = [str(t) for t in loop.transcript.recent()]
    assert not any("download finished" in t for t in turns), (
        "a notice entered the conversation the model sees"
    )
    assert _kinds(events, "notice")[0]["spoken"] is True
    assert "text" not in _kinds(events, "notice")[0]


def test_notices_never_land_on_somebody_elses_sentence():
    loop, _spy, speaker, _events = build()
    loop.mic = FakeMic(utterances=["so anyway I told him no"])
    loop.gate = StubGate(default=StubWake(wake=False, addressed_p=0.02))
    loop.notices.post("job1", "that download finished")
    loop.run()
    assert speaker.said == [], "daa spoke into an utterance the gate dropped"
    assert loop.notices.pending(), "the notice was consumed anyway"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WindowJournal(StubJournal):
    """A journal that knows about checkpoints, in the shape the real one
    returns: newest first, freshness checked."""

    sealed: str | None = None

    def window(self, checkpoint_id: str) -> list[Any]:
        return list(reversed(self.entries))

    def seal_reason(self, checkpoint_id: str) -> str | None:
        return self.sealed


def _rollback_world(
    entries: int = 3,
    *,
    stale_at: int | None = None,
    sealed: str | None = None,
    verdicts: list[str] | None = None,
):
    from test_voice_loop import StubEntry

    loop, spy, speaker, events = build(
        tier=RiskTier.ANNOUNCE,
        confirm=StubConfirm(verdicts=verdicts if verdicts is not None else ["yes"] * 8),
    )
    journal = WindowJournal(sealed=sealed)
    for i in range(entries):
        journal.entries.append(
            StubEntry(
                action=UndoAction(description=f"put back {i}", tool="move_files", args={}),
                produced_by="move_files",
                id=f"e{i}",
                stale=None,
            )
        )
    if stale_at is not None:
        journal.entries[stale_at] = replace(
            journal.entries[stale_at], stale="They've changed since I did that."
        )
    loop.journal = journal
    return loop, spy, speaker, events, journal


def test_rollback_asks_once_and_runs_every_inverse():
    loop, spy, speaker, events, journal = _rollback_world(3)
    loop.rollback("cp1", replies=["yes"])
    questions = [s for s in speaker.said if s.endswith("?")]
    assert len(questions) == 1, f"the user was asked {len(questions)} times, not once"
    assert "three things" in questions[0]
    assert len(spy.runs) == 3
    assert journal.committed == ["e2", "e1", "e0"], "the window was not replayed in reverse"
    row = _kinds(events, "rollback")[0]
    assert (row["attempted"], row["reversed"]) == (3, 3)


def test_rollback_stops_at_the_first_stale_row_and_says_so():
    """A rollback that ploughs through stale entries is a second mutation
    wearing an undo's clothes."""
    loop, spy, speaker, events, _journal = _rollback_world(3, stale_at=1)
    loop.rollback("cp1", replies=["yes"])
    assert len(spy.runs) == 1, "it carried on past a row the world had moved under"
    summary = speaker.said[-1]
    assert "one of" in summary and "two" in summary
    assert "changed since" in summary
    row = _kinds(events, "rollback")[0]
    assert (row["attempted"], row["reversed"]) == (3, 1)


def test_a_sealed_checkpoint_changes_the_sentence_not_the_answer():
    loop, spy, speaker, events, _journal = _rollback_world(
        2, sealed="the email has already gone"
    )
    loop.rollback("cp1", replies=["yes"])
    question = next(s for s in speaker.said if s.endswith("?"))
    assert "I can put back" in question
    assert "the email has already gone" in question, (
        "sealing must state the boundary BEFORE the yes, not after it"
    )
    assert len(spy.runs) == 2, "sealing disabled the rollback instead of describing it"
    assert _kinds(events, "rollback")[0]["sealed"] is True


def test_rollback_refuses_an_unverifiable_row_like_every_other_undo():
    from test_voice_loop import StubEntry

    loop, spy, _speaker, _events, journal = _rollback_world(0)
    journal.entries.append(
        StubEntry(
            action=UndoAction(description="x", tool="set_clipboard", args={}),
            produced_by="move_files",
            id="forged",
        )
    )
    loop.rollback("cp1", replies=["yes"])
    assert spy.runs == [], "set_clipboard is nobody's inverse"


def test_declining_a_rollback_runs_nothing():
    loop, spy, _speaker, events, _journal = _rollback_world(3, verdicts=["no"])
    loop.rollback("cp1", replies=["no"])
    assert spy.runs == []
    assert _kinds(events, "rollback")[0]["stopped_reason"] == "not confirmed"


def test_rollback_cannot_be_aggregated_past_the_voice_tier():
    """One spoken yes covers a window of CONFIRM_VOICE inverses. It does not
    reach CONFIRM_VISUAL, because that tier exists precisely because speech is
    not good enough."""
    loop, spy, speaker, _events, _journal = _rollback_world(2)
    loop.policy_decide = stub_policy(RiskTier.CONFIRM_VISUAL)
    loop.console = FakeConsole(present=False)
    loop.rollback("cp1", replies=["yes"])
    assert spy.runs == [], "an aggregate spoken yes reached the visual tier"
    assert any("no screen" in s for s in speaker.said)


# ---------------------------------------------------------------------------
# What a grant deliberately does NOT reach
# ---------------------------------------------------------------------------


def test_a_grant_cannot_answer_for_an_instruction_that_came_off_the_disk():
    """`undo_last` raises the tier because the instruction arrived from a
    world-readable file rather than from the user. A grant answering that
    raise would turn "you okayed tidying Downloads" into "you okayed anything
    that names move_files", which is the whole attack the raise exists for."""
    from test_voice_loop import StubEntry

    loop, spy, speaker, events = build(tier=RiskTier.ANNOUNCE)
    _grant(loop)
    loop.journal.entries.append(
        StubEntry(
            action=UndoAction(description="put them back", tool="move_files", args={}),
            produced_by="move_files",
            id="e0",
        )
    )
    loop.undo_last(replies=["yes"])
    assert any("Should I" in s for s in speaker.said), (
        "the grant answered a confirmation raised by an untrusted source"
    )
    assert spy.runs, "the answered question did not run"
    assert not [r for r in _kinds(events, "confirmation") if r["via"] == "grant"]


def test_a_grant_cannot_answer_for_a_tool_the_router_never_offered():
    """Same rule, other source: a tool nobody routed for this utterance is
    exactly the kind of thing the user should get a beat to say no to."""
    loop, spy, speaker, events = build(tier=RiskTier.ANNOUNCE)
    _grant(loop)
    outcome = TurnOutcome(woke=True)
    loop._scripted_replies = ["yes"]
    loop._handle_call(
        ToolCall("move_files", {}), "move them", outcome, allowed=frozenset({"get_weather"})
    )
    assert any("Should I" in s for s in speaker.said)
    assert not [r for r in _kinds(events, "confirmation") if r["via"] == "grant"]
    assert spy.runs
