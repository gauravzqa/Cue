"""The loop, end to end, with zero hardware / network / keys.

Everything the loop depends on is a Protocol or a duck type, so every sibling
subsystem is stubbed here. That is not a workaround for the siblings not being
finished -- it is how these tests should stay even once they are, because a
test of the ORCHESTRATION must not be able to fail because a risk prompt got
retuned.

The load-bearing tests in this file are the ones that assert `tool.run` was
UNREACHABLE: test_confirm_no_never_runs, test_unclear_reasks_once_then_abandons,
test_refuse_never_runs, test_visual_tier_never_runs_from_voice and
test_missing_policy_refuses_everything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from daa.config import Settings
from daa.contracts import (
    AuditEvent,
    Disposition,
    ResolvedAction,
    RiskAssessment,
    RiskTier,
    ToolResult,
    ToolSpec,
    UndoAction,
)
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import TurnOutcome, VoiceLoop
from daa.voice.mic import FakeMic
from daa.voice.stt import FakeTranscriber

# ---------------------------------------------------------------------------
# Stubs for the three sibling subsystems
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubWake:
    wake: bool = True
    end_of_turn: bool = True
    needs_planner: bool = False
    addressed_p: float = 0.99
    latency_ms: float = 1.0
    synthetic: bool = True


@dataclass(slots=True)
class StubGate:
    decisions: list[StubWake] = field(default_factory=list)
    default: StubWake = field(default_factory=StubWake)
    seen: list[str] = field(default_factory=list)

    def should_wake(self, transcript: str, ctx: Mapping[str, Any]) -> StubWake:
        self.seen.append(transcript)
        return self.decisions.pop(0) if self.decisions else self.default


@dataclass(slots=True)
class StubRouter:
    keep: tuple[str, ...] | None = None
    seen: list[str] = field(default_factory=list)

    def activate(
        self, utterance: str, specs: Sequence[ToolSpec], ctx: Mapping[str, Any]
    ) -> list[ToolSpec]:
        self.seen.append(utterance)
        if self.keep is None:
            return list(specs)
        return [s for s in specs if s.name in self.keep]


SYNTHETIC_ASSESSMENT = RiskAssessment(
    blast_radius=0.6,
    unrecoverable=0.0,
    explicitly_requested=1.0,
    target_confidence="certain",
    confidence=0.95,
    synthetic=True,
)


@dataclass(slots=True)
class StubRisk:
    calls: list[ResolvedAction] = field(default_factory=list)

    def assess(self, action: ResolvedAction, utterance: str, history: Any) -> RiskAssessment:
        self.calls.append(action)
        return SYNTHETIC_ASSESSMENT


@dataclass(slots=True)
class StubConfirm:
    verdicts: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def interpret(self, reply: str, pending: Any) -> str:
        self.calls.append(reply)
        return self.verdicts.pop(0) if self.verdicts else "unclear"


@dataclass(slots=True)
class SpyTool:
    """Records every run(). The whole point is that `runs` stays 0."""

    spec: ToolSpec
    runs: list[ResolvedAction] = field(default_factory=list)
    resolves: list[Mapping[str, Any]] = field(default_factory=list)
    undo: UndoAction | None = None
    ok: bool = True

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        self.resolves.append(kwargs)
        return ResolvedAction(
            tool=self.spec.name,
            args=kwargs,
            targets=tuple(str(v) for v in kwargs.values()) or ("something",),
            explicit=True,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return ToolResult(
            ok=self.ok,
            summary="Moved three files." if self.ok else "",
            undo=self.undo,
            error=None if self.ok else "boom",
        )


@dataclass(slots=True)
class StubRegistry:
    """Mirrors the REAL ToolRegistry, which RAISES KeyError on a miss.

    The stub used to return None, which is why the loop was written expecting
    None and why one hallucinated tool name took the whole session down in
    production while this file stayed green.
    """

    tools: dict[str, SpyTool] = field(default_factory=dict)

    def register(self, tool: SpyTool) -> None:
        self.tools[tool.spec.name] = tool

    def get(self, name: str) -> SpyTool:
        try:
            return self.tools[name]
        except KeyError:
            raise KeyError(f"no tool named {name!r}") from None

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]


@dataclass(slots=True)
class StubEntry:
    """What UndoJournal.peek() hands back: a CLAIM about a past mutation."""

    action: UndoAction
    produced_by: str | None = "move_files"
    id: str = "entry1"
    stale: str | None = None

    @property
    def trusted(self) -> bool:
        return bool(self.produced_by and self.action.tool)

    @property
    def tool(self) -> str:
        return self.action.tool

    @property
    def args(self) -> Mapping[str, Any]:
        return self.action.args

    @property
    def description(self) -> str:
        return self.action.description


@dataclass(slots=True)
class StubJournal:
    """peek/commit, like the real one. pop() is present but must not be used
    by the loop: it consumes before anything has run."""

    entries: list[StubEntry] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    pops: int = 0

    def record(self, undo: UndoAction, context: Any = None, *, produced_by: str) -> StubEntry:
        entry = StubEntry(action=undo, produced_by=produced_by, id=f"e{len(self.entries)}")
        self.entries.append(entry)
        return entry

    def peek(self) -> StubEntry | None:
        return self.entries[-1] if self.entries else None

    def commit(self, entry: StubEntry) -> bool:
        self.committed.append(entry.id)
        self.entries = [e for e in self.entries if e.id != entry.id]
        return True

    def pop(self) -> StubEntry | None:
        self.pops += 1
        return self.entries.pop() if self.entries else None

    def history(self) -> list[StubEntry]:
        return list(self.entries)


def stub_policy(tier: RiskTier) -> Any:
    seen: list[tuple[str, Any]] = []

    def decide(action: ResolvedAction, spec: ToolSpec, assessment: Any, settings: Any):
        seen.append((action.tool, assessment))
        # Mirrors the real rule: policy may only ever RAISE above the floor.
        return Disposition(tier=max(tier, spec.floor), reason="stub", assessment=assessment)

    decide.seen = seen  # type: ignore[attr-defined]
    return decide


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOVE = ToolSpec(
    name="move_files",
    description="Move files between folders",
    params={"src": {"type": "string"}, "dest": {"type": "string"}},
    floor=RiskTier.SILENT,
    activation_hint="moving, filing, tidying downloads",
    # A move is its own inverse. The undo path checks this rather than
    # trusting the tool name written in the journal file.
    inverses=("move_files",),
)
WEATHER = ToolSpec(
    name="get_weather",
    description="Read the forecast",
    params={},
    floor=RiskTier.SILENT,
    activation_hint="weather, forecast, rain",
)


def build(
    *,
    tier: RiskTier = RiskTier.ANNOUNCE,
    utterances: Sequence[str] = (),
    gate: Any = None,
    confirm: Any = None,
    llm: Any = None,
    cloud: Any = None,
    policy: Any = "default",
    dry_run: bool = False,
    tools: Sequence[SpyTool] | None = None,
    specs_extra: Sequence[ToolSpec] = (),
) -> tuple[VoiceLoop, SpyTool, list[str], list[AuditEvent]]:
    spy = SpyTool(spec=MOVE, undo=UndoAction(description="move them back", tool="move_files",
                                             args={"src": "b", "dest": "a"}))
    registry = StubRegistry()
    for tool in tools if tools is not None else [spy]:
        registry.register(tool)
    for extra in specs_extra:
        registry.register(SpyTool(spec=extra))
    events: list[AuditEvent] = []
    loop = VoiceLoop(
        settings=Settings(dry_run=dry_run),
        mic=FakeMic(utterances=list(utterances)),
        local_stt=FakeTranscriber(source="local"),
        cloud_stt=cloud,
        gate=gate if gate is not None else StubGate(),
        router=StubRouter(),
        risk=StubRisk(),
        confirm=confirm if confirm is not None else StubConfirm(verdicts=["yes"]),
        registry=registry,
        policy_decide=stub_policy(tier) if policy == "default" else policy,
        journal=StubJournal(),
        audit=events.append,
        llm=llm
        if llm is not None
        else FakeLLM(turns=[LLMTurn(tool_calls=(ToolCall("move_files", {"src": "a", "dest": "b"}),))]),
    )
    return loop, spy, loop.said, events


# ---------------------------------------------------------------------------
# Gate: the privacy boundary
# ---------------------------------------------------------------------------


def test_unaddressed_speech_is_dropped_and_never_leaves_the_laptop():
    llm = FakeLLM()
    cloud = FakeTranscriber(source="cloud")
    loop, spy, said, events = build(
        utterances=["so anyway I told him no"],
        gate=StubGate(default=StubWake(wake=False, addressed_p=0.02)),
        llm=llm,
        cloud=cloud,
    )
    outcomes = loop.run()

    assert outcomes[0].woke is False
    assert outcomes[0].dropped_reason == "not addressed"
    # The three things that must NOT have happened.
    assert cloud.calls == [], "cloud STT ran on speech that never woke the gate"
    assert llm.seen == [], "the conversational model saw unaddressed speech"
    assert spy.runs == []
    assert said == []
    # And the drop is logged WITHOUT the text.
    dropped = [e for e in events if e.kind == "dropped"]
    assert len(dropped) == 1
    assert "text" not in dropped[0].payload


def test_mid_utterance_buffers_then_acts_on_the_whole_sentence():
    gate = StubGate(decisions=[StubWake(end_of_turn=False), StubWake()])
    loop, spy, _said, _events = build(
        utterances=["move the screenshots", "into the ferrari folder"],
        gate=gate,
        tier=RiskTier.SILENT,
    )
    loop.run()

    # The second gate call saw both halves joined, not just the tail.
    assert gate.seen[1] == "move the screenshots into the ferrari folder"
    assert len(spy.runs) == 1


def test_gate_failure_fails_closed():
    class Exploding:
        def should_wake(self, transcript, ctx):
            raise RuntimeError("typesafe is down")

    loop, spy, said, _events = build(utterances=["delete everything"], gate=Exploding())
    outcomes = loop.run()

    assert outcomes[0].woke is False
    assert spy.runs == []
    assert said == []


def test_cloud_rescore_is_what_reaches_the_tool():
    cloud = FakeTranscriber(source="cloud", rescore={"move the ferari screen shots":
                                                     "move the Ferrari screenshots"})
    llm = FakeLLM()
    loop, _spy, _said, _events = build(
        utterances=["move the ferari screen shots"], cloud=cloud, llm=llm, tier=RiskTier.SILENT
    )
    outcomes = loop.run()

    assert outcomes[0].rescored is True
    assert outcomes[0].utterance == "move the Ferrari screenshots"
    # And the model saw the good transcript, not the local one.
    assert llm.seen[0][0][-1]["content"] == "move the Ferrari screenshots"


# ---------------------------------------------------------------------------
# Dispositions
# ---------------------------------------------------------------------------


def test_silent_tier_runs_without_speaking():
    loop, spy, said, _events = build(utterances=["tidy up"], tier=RiskTier.SILENT)
    loop.run()

    assert len(spy.runs) == 1
    assert said == [], "SILENT tier spoke"


def test_announce_tier_runs_then_speaks_the_summary():
    loop, spy, said, _events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()

    assert len(spy.runs) == 1
    assert said == ["Moved three files."]


def test_confirm_yes_reads_back_resolved_targets_then_runs():
    confirm = StubConfirm(verdicts=["yes"])
    loop, spy, said, _events = build(
        utterances=["move them", "yeah go on"], tier=RiskTier.CONFIRM_VOICE, confirm=confirm
    )
    loop.run()

    # The readback is of RESOLVED targets, not of the raw utterance.
    assert "a, b" in said[0]
    assert confirm.calls == ["yeah go on"]
    assert len(spy.runs) == 1


def test_confirm_no_never_runs():
    confirm = StubConfirm(verdicts=["no"])
    loop, spy, said, events = build(
        utterances=["move them", "no don't"], tier=RiskTier.CONFIRM_VOICE, confirm=confirm
    )
    loop.run()

    assert spy.runs == [], "tool.run was reachable after a 'no'"
    assert "Okay, leaving it." in said
    assert any(e.kind == "abandoned" for e in events)


def test_unclear_reasks_exactly_once_then_abandons():
    confirm = StubConfirm(verdicts=["unclear", "unclear", "yes"])
    loop, spy, said, _events = build(
        utterances=["move them", "hmm", "uh"],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=confirm,
    )
    loop.run()

    assert spy.runs == [], "tool.run was reachable after two unclear answers"
    # Asked once, re-asked once, then stopped. Never a third prompt.
    assert len(confirm.calls) == 2
    assert said.count("Sorry — yes or no?") == 1
    assert said[-1] == "I'll leave it for now."


def test_confirm_with_no_answer_abandons():
    loop, spy, said, _events = build(
        utterances=["move them"], tier=RiskTier.CONFIRM_VOICE, confirm=StubConfirm()
    )
    loop.run()

    assert spy.runs == []
    assert said[-1] == "I didn't hear an answer, so I'll leave it."


def test_confirm_parser_failure_is_treated_as_unclear():
    class Exploding:
        def interpret(self, reply, pending):
            raise RuntimeError("jev down")

    loop, spy, _said, _events = build(
        utterances=["move them", "yes", "yes"],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=Exploding(),
    )
    loop.run()

    assert spy.runs == [], "a broken confirm parser authorized a mutation"


def test_refuse_never_runs():
    loop, spy, said, _events = build(utterances=["rm -rf /"], tier=RiskTier.REFUSE)
    loop.run()

    assert spy.runs == []
    assert said and said[0].startswith("I won't do that.")


def test_visual_tier_never_runs_from_voice():
    confirm = StubConfirm(verdicts=["yes", "yes", "yes"])
    loop, spy, said, events = build(
        utterances=["send it", "yes", "yes"],
        tier=RiskTier.CONFIRM_VISUAL,
        confirm=confirm,
    )
    loop.run()

    assert spy.runs == [], "a spoken yes authorized CONFIRM_VISUAL"
    assert confirm.calls == [], "the visual tier asked for a spoken confirmation"
    assert any(e.kind == "deferred_visual" for e in events)
    assert "screen" in said[0]


def test_missing_policy_refuses_everything():
    loop, spy, said, _events = build(utterances=["move them"], policy=None)
    loop.run()

    assert spy.runs == []
    assert said == ["I can't act right now, my safety policy isn't loaded."]


def test_policy_error_fails_closed():
    def exploding(action, spec, assessment, settings):
        raise ValueError("bad tier")

    loop, spy, _said, _events = build(utterances=["move them"], policy=exploding)
    loop.run()

    assert spy.runs == []


def test_policy_may_only_raise_above_the_tool_floor():
    """The stub policy honours the floor; assert the loop passes the spec through
    so the real policy can see it."""
    loop, _spy, _said, _events = build(utterances=["move them"], tier=RiskTier.SILENT)
    loop.run()
    seen = loop.policy_decide.seen  # type: ignore[attr-defined]
    assert seen and seen[0][0] == "move_files"
    assert seen[0][1] is SYNTHETIC_ASSESSMENT, "the risk assessment never reached policy"


# ---------------------------------------------------------------------------
# The invariant, asserted structurally
# ---------------------------------------------------------------------------


def test_execute_is_the_only_caller_of_tool_run():
    import inspect

    from daa.voice import loop as loop_mod

    source = inspect.getsource(loop_mod)
    # One call site, and it is inside _execute.
    assert source.count("tool.run(") == 1
    assert source.count(".run(action)") == 1
    body = inspect.getsource(loop_mod.VoiceLoop._execute)
    assert "tool.run(action)" in body


def test_execute_rejects_a_missing_disposition():
    loop, spy, _said, _events = build()
    action = spy.resolve(src="a", dest="b")
    with pytest.raises(AssertionError):
        loop._execute(spy, action, None, TurnOutcome(), confirmed=False)
    assert spy.runs == []


@pytest.mark.parametrize("tier", [RiskTier.REFUSE, RiskTier.CONFIRM_VISUAL])
def test_execute_rejects_unauthorizable_tiers(tier: RiskTier):
    loop, spy, _said, _events = build()
    action = spy.resolve(src="a", dest="b")
    with pytest.raises(AssertionError):
        loop._execute(spy, action, Disposition(tier=tier, reason="x"), TurnOutcome(),
                      confirmed=True)
    assert spy.runs == []


def test_execute_rejects_unconfirmed_confirm_tier():
    loop, spy, _said, _events = build()
    action = spy.resolve(src="a", dest="b")
    with pytest.raises(AssertionError):
        loop._execute(
            spy,
            action,
            Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x"),
            TurnOutcome(),
            confirmed=False,
        )
    assert spy.runs == []


# ---------------------------------------------------------------------------
# Undo, dry run, routing, audit
# ---------------------------------------------------------------------------


def test_undo_is_recorded_before_the_summary_is_spoken():
    loop, _spy, _said, _events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()

    assert len(loop.journal.entries) == 1
    assert loop.journal.entries[0].description == "move them back"
    # Attributed to the tool that made the change, which is the only thing that
    # makes the row checkable when it comes back off disk.
    assert loop.journal.entries[0].produced_by == "move_files"


def test_dry_run_never_reaches_the_tool():
    loop, spy, said, events = build(utterances=["move them"], dry_run=True)
    loop.run()

    assert spy.runs == [], "dry run mutated"
    assert spy.resolves, "dry run skipped resolve, so it narrated nothing real"
    assert said[0].startswith("Dry run:")
    assert any(e.kind == "dry_run" for e in events)


def test_router_narrows_what_the_model_sees():
    llm = FakeLLM()
    loop, _spy, _said, _events = build(
        utterances=["what's the weather"], llm=llm, specs_extra=[WEATHER]
    )
    loop.router.keep = ("get_weather",)
    loop.run()

    assert llm.seen[0][1] == ["get_weather"], "the whole registry was sent to the model"


def test_router_failure_falls_back_to_every_tool():
    class Exploding:
        def activate(self, utterance, specs, ctx):
            raise RuntimeError("jev down")

    llm = FakeLLM()
    loop, _spy, _said, _events = build(utterances=["move them"], llm=llm, specs_extra=[WEATHER])
    loop.router = Exploding()
    loop.run()

    assert set(llm.seen[0][1]) == {"move_files", "get_weather"}


def test_unknown_tool_is_handled_not_crashed():
    llm = FakeLLM(turns=[LLMTurn(tool_calls=(ToolCall("nope", {}),))])
    loop, spy, said, events = build(utterances=["do a thing"], llm=llm)
    loop.run()

    assert spy.runs == []
    assert said == ["I don't have a tool for that."]
    assert any(e.kind == "error" and e.payload.get("where") == "registry" for e in events)


def test_resolve_failure_asks_instead_of_running():
    class BadTool(SpyTool):
        def resolve(self, **kwargs):
            raise ValueError("which folder?")

    bad = BadTool(spec=MOVE)
    loop, _spy, said, _events = build(utterances=["move them"], tools=[bad])
    loop.run()

    assert bad.runs == []
    assert said == ["I couldn't work out what you meant by that."]


def test_model_text_is_spoken_only_when_no_tool_spoke():
    llm = FakeLLM(turns=[LLMTurn(text="It's raining.")])
    loop, spy, said, _events = build(utterances=["weather?"], llm=llm)
    loop.run()

    assert said == ["It's raining."]
    assert spy.runs == []


def test_a_tool_that_spoke_suppresses_the_model_chatter():
    llm = FakeLLM(
        turns=[
            LLMTurn(
                text="Sure, moving them now.",
                tool_calls=(ToolCall("move_files", {"src": "a", "dest": "b"}),),
            )
        ]
    )
    loop, _spy, said, _events = build(utterances=["move them"], llm=llm)
    loop.run()

    assert said == ["Moved three files."]


def test_audit_trail_covers_the_whole_path():
    loop, _spy, _said, events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()

    kinds = [e.kind for e in events]
    for expected in ("heard", "woke", "judgment", "disposition", "execution", "spoke"):
        assert expected in kinds, f"no {expected!r} audit event"


def test_audit_sink_failure_does_not_break_the_loop():
    def exploding(event):
        raise OSError("disk full")

    loop, spy, _said, _events = build(utterances=["move them"])
    loop.audit = exploding
    loop.run()

    assert len(spy.runs) == 1


def test_undo_last_goes_through_policy_too():
    loop, spy, _said, _events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()
    spy.runs.clear()

    # "yes", because undo is never below CONFIRM_VOICE: the instruction came
    # from a file on disk, not from the user.
    loop.undo_last(replies=["yes"])
    assert len(spy.runs) == 1
    # The original entry was committed; what is in the journal now is the
    # inverse of the undo, i.e. a redo. Undo is not a special case here.
    assert loop.journal.committed == ["e0"]
    assert len(loop.journal.entries) == 1


def test_undo_with_nothing_to_undo():
    loop, _spy, said, _events = build()
    loop.undo_last()
    assert said == ["There's nothing to undo."]


# ---------------------------------------------------------------------------
# Integration with the real siblings, when they exist
# ---------------------------------------------------------------------------


def test_real_modules_wire_together():
    """Skipped until the sibling subsystems land; then it is the canary that
    the signatures in this file still match theirs."""
    pytest.importorskip("daa.jev.client")
    pytest.importorskip("daa.jev.gate")
    pytest.importorskip("daa.tools.registry")
    pytest.importorskip("daa.safety.policy")

    from daa.voice.loop import build_loop

    loop = build_loop(Settings(dry_run=True), mic=FakeMic(utterances=["what time is it"]))
    assert getattr(loop, "missing", None) == [], f"half-wired: {loop.missing}"
    assert loop.policy_decide is not None
    assert loop.registry is not None
    loop.run()


def test_real_policy_and_confirm_gate_a_real_tool(tmp_path):
    """The signature canary.

    Drives the REAL registry, risk gate, policy and confirm parser through the
    loop with a scripted tool call. dry_run keeps it off the machine, but every
    signature on the path -- resolve/assess/decide/interpret -- is the real one,
    so a sibling changing an argument breaks here rather than in production.
    """
    pytest.importorskip("daa.jev.client")
    pytest.importorskip("daa.safety.policy")

    from daa.tools.registry import REGISTRY
    from daa.voice.loop import build_loop

    victim = tmp_path / "Screenshot 2026-09-20.png"
    victim.write_bytes(b"png")

    loop = build_loop(Settings(dry_run=True), mic=FakeMic())
    loop.llm = FakeLLM(
        turns=[LLMTurn(tool_calls=(ToolCall("move_to_trash", {"paths": [str(victim)]}),))]
    )
    outcome = loop.handle_text("bin that screenshot", replies=["yes"])

    # NOT the module-global REGISTRY: build_loop binds a fresh registry to the
    # settings it was handed, so the tools cannot answer to the environment
    # while the loop answers to the caller. Asserting identity with the global
    # would be asserting that divergence back into place.
    assert loop.registry is not REGISTRY
    assert {n.name for n in loop.registry.specs()} == {
        n.name for n in REGISTRY.specs()
    }, "same tools, different binding"
    assert loop.registry.get("move_to_trash").dry_run is True, (
        "registry tools must honour the caller's Settings, not os.environ"
    )
    assert outcome.dispositions, "the real policy returned nothing"
    tier = outcome.dispositions[0].tier
    # move_to_trash declares a CONFIRM_VOICE floor, and policy may only raise.
    assert tier >= RiskTier.CONFIRM_VOICE
    assert victim.exists(), "dry run touched the filesystem"


def test_tool_schema_matches_the_real_registry_params():
    """`llm.tool_schema` has to eat whatever tools/ actually registers."""
    pytest.importorskip("daa.tools.registry")

    import json

    from daa.tools.registry import REGISTRY
    from daa.voice.llm import tool_schema

    for spec in REGISTRY.specs():
        schema = tool_schema(spec)
        assert schema["function"]["name"] == spec.name
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        # `required` is a shorthand key on each param, not a JSON-schema one;
        # it must be lifted out rather than shipped to the model.
        for prop in params["properties"].values():
            assert "required" not in prop
        json.dumps(schema)


# ---------------------------------------------------------------------------
# The undo journal is UNTRUSTED INPUT
#
# ~/.daa/undo.jsonl is a file. Anything running as the user can append a row to
# it, and `daa undo` reads a tool name and a bag of arguments out of that row
# and executes them. Every test below was written against a reproduction: a
# forged row naming `set_clipboard` ran with no confirmation at all.
# ---------------------------------------------------------------------------

CLIPBOARD = ToolSpec(
    name="set_clipboard",
    description="Put text on the clipboard",
    params={"text": {"type": "string"}},
    floor=RiskTier.ANNOUNCE,
    activation_hint="copy, clipboard, paste",
)


def _undo_world(
    *,
    entry: StubEntry,
    tier: RiskTier = RiskTier.ANNOUNCE,
    dry_run: bool = False,
    verdicts: Sequence[str] = ("yes",),
):
    """A loop whose journal already contains `entry`, with both a legitimate
    tool and the forger's tool of choice registered."""
    mover = SpyTool(spec=MOVE)
    clipboard = SpyTool(spec=CLIPBOARD)
    loop, _spy, said, events = build(
        tier=tier,
        dry_run=dry_run,
        tools=[mover, clipboard],
        confirm=StubConfirm(verdicts=list(verdicts)),
    )
    loop.journal.entries = [entry]
    return loop, mover, clipboard, said, events


def _forged(tool: str = "set_clipboard", produced_by: str | None = "move_files") -> StubEntry:
    return StubEntry(
        action=UndoAction(description="put it back", tool=tool, args={"text": "attacker"}),
        produced_by=produced_by,
    )


def test_a_forged_journal_row_naming_a_mutating_tool_never_executes():
    """The reproduction. `set_clipboard` is nobody's inverse."""
    loop, _mover, clipboard, said, events = _undo_world(entry=_forged())

    loop.undo_last()

    assert clipboard.runs == [], "a forged undo row executed a mutating tool"
    assert said and "doesn't check out" in said[0]
    rejected = [e for e in events if e.kind == "undo_rejected"]
    assert rejected and rejected[0].payload["reason"] == "not an inverse of produced_by"


def test_a_journal_row_with_no_producer_is_refused():
    """A row that will not say what made it cannot be checked, and an
    uncheckable claim from a world-readable file is a hostile one."""
    loop, mover, _clip, said, events = _undo_world(
        entry=_forged(tool="move_files", produced_by=None)
    )

    loop.undo_last()

    assert mover.runs == [], "an unattributed journal row executed"
    assert "doesn't check out" in said[0]
    reasons = [e.payload["reason"] for e in events if e.kind == "undo_rejected"]
    assert reasons == ["journal says untrusted"] or reasons == ["no produced_by"]


def test_a_journal_row_naming_an_unknown_producer_is_refused():
    loop, mover, _clip, _said, events = _undo_world(
        entry=_forged(tool="move_files", produced_by="tool_that_does_not_exist")
    )

    loop.undo_last()

    assert mover.runs == []
    assert [e.payload["reason"] for e in events if e.kind == "undo_rejected"] == [
        "unknown produced_by"
    ]


def test_a_legitimate_inverse_still_runs():
    """The check has to let real undos through or it is just a broken feature."""
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={"src": "b"}),
        produced_by="move_files",
    )
    loop, mover, _clip, _said, _events = _undo_world(entry=entry)

    loop.undo_last(replies=["yes"])

    assert len(mover.runs) == 1


def test_undo_is_never_executed_below_confirm_voice():
    """However permissive policy is, the instruction came from a file."""
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    # SILENT: the stub policy would happily run this without saying a word.
    loop, mover, _clip, _said, events = _undo_world(
        entry=entry, tier=RiskTier.SILENT, verdicts=["no"]
    )

    outcome = loop.undo_last(replies=["no thanks"])

    assert outcome.dispositions[0].tier >= RiskTier.CONFIRM_VOICE, (
        "a journal row authorized an execution with no confirmation"
    )
    assert mover.runs == [], "undo ran without a spoken yes"
    assert any(e.kind == "tier_raised" for e in events)


def test_undo_never_lowers_a_tier_policy_raised():
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    loop, mover, _clip, _said, _events = _undo_world(
        entry=entry, tier=RiskTier.REFUSE, verdicts=["yes"]
    )

    outcome = loop.undo_last(replies=["yes"])

    assert outcome.dispositions[0].tier is RiskTier.REFUSE
    assert mover.runs == []


# --- and it must not eat the entry it did not use --------------------------


def test_a_dry_run_undo_keeps_the_journal_entry():
    """dry_run=True is the SHIPPED DEFAULT. Consuming the entry here means a
    stock install permanently discards the real undo record while undoing
    nothing at all."""
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    loop, mover, _clip, said, _events = _undo_world(entry=entry, dry_run=True)

    loop.undo_last(replies=["yes"])

    assert mover.runs == []
    assert loop.journal.committed == [], "a dry run spent the undo record"
    assert loop.journal.peek() is entry, "the undo record is gone"
    assert any(line.startswith("Dry run:") for line in said)


def test_a_refused_undo_keeps_the_journal_entry():
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    loop, mover, _clip, _said, events = _undo_world(entry=entry, verdicts=["no"])

    loop.undo_last(replies=["no"])

    assert mover.runs == []
    assert loop.journal.committed == []
    assert loop.journal.peek() is entry
    assert any(e.kind == "undo_retained" for e in events)


def test_an_unverifiable_undo_keeps_the_journal_entry():
    entry = _forged()
    loop, _mover, _clip, _said, _events = _undo_world(entry=entry)

    loop.undo_last()

    assert loop.journal.committed == []
    assert loop.journal.peek() is entry, "a refused row was consumed anyway"


def test_a_failed_undo_keeps_the_journal_entry():
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    mover = SpyTool(spec=MOVE, ok=False)
    loop, _spy, _said, _events = build(tools=[mover], confirm=StubConfirm(verdicts=["yes"]))
    loop.journal.entries = [entry]

    loop.undo_last(replies=["yes"])

    assert len(mover.runs) == 1
    assert loop.journal.committed == [], "an undo that failed was marked done"


def test_the_undo_path_never_calls_pop():
    """pop() consumes before anything has run, which is the whole bug."""
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    loop, _mover, _clip, _said, _events = _undo_world(entry=entry, dry_run=True)

    loop.undo_last(replies=["yes"])

    assert loop.journal.pops == 0


# ---------------------------------------------------------------------------
# A hallucinated tool name is routine, not exceptional
# ---------------------------------------------------------------------------


def test_a_hallucinated_tool_name_does_not_end_the_session():
    """ToolRegistry.get RAISES KeyError. One bad name from the model used to
    traceback out of `daa listen`, losing the mic and the whole session."""
    llm = FakeLLM(
        turns=[
            LLMTurn(tool_calls=(ToolCall("summon_a_dragon", {}),)),
            LLMTurn(tool_calls=(ToolCall("move_files", {"src": "a", "dest": "b"}),)),
        ]
    )
    loop, spy, said, events = build(
        utterances=["do the impossible", "now move them"], llm=llm, tier=RiskTier.ANNOUNCE
    )

    outcomes = loop.run()

    assert len(outcomes) == 2, "the loop died on the first utterance"
    assert said[0] == "I don't have a tool for that."
    assert len(spy.runs) == 1, "the session did not survive to the next utterance"
    assert any(e.kind == "error" and e.payload.get("where") == "registry" for e in events)


def test_a_tool_the_router_did_not_activate_is_escalated_not_waved_through():
    """The router's narrowing has to mean something.

    It is NOT a hard refusal: ToolRouter returns an empty list when Jev is
    unavailable, so refusing every un-activated call would make an outage into
    an inert assistant. Instead the miss is logged and the tier is raised.
    """
    llm = FakeLLM(turns=[LLMTurn(tool_calls=(ToolCall("move_files", {"src": "a"}),))])
    loop, spy, _said, events = build(
        utterances=["what's the weather"],
        llm=llm,
        tier=RiskTier.SILENT,
        specs_extra=[WEATHER],
        confirm=StubConfirm(verdicts=["no"]),
    )
    loop.router.keep = ("get_weather",)

    outcomes = loop.run()

    assert any(e.kind == "router_miss" for e in events), "the router miss was not recorded"
    assert outcomes[0].dispositions[0].tier >= RiskTier.CONFIRM_VOICE
    assert spy.runs == [], "an un-routed tool ran with no confirmation"


def test_an_un_routed_tool_still_runs_once_the_user_says_yes():
    llm = FakeLLM(turns=[LLMTurn(tool_calls=(ToolCall("move_files", {"src": "a"}),))])
    loop, spy, _said, _events = build(
        utterances=["what's the weather", "yes"],
        llm=llm,
        tier=RiskTier.SILENT,
        specs_extra=[WEATHER],
        confirm=StubConfirm(verdicts=["yes"]),
    )
    loop.router.keep = ("get_weather",)

    loop.run()

    assert len(spy.runs) == 1


# ---------------------------------------------------------------------------
# The readback has to say what will HAPPEN, not just to what
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VerbTool:
    """A resolver that fills in `verb` and `consequences`, as tools/ now does."""

    spec: ToolSpec
    runs: list[ResolvedAction] = field(default_factory=list)

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return ResolvedAction(
            tool=self.spec.name,
            args=kwargs,
            targets=("report.pdf",),
            explicit=True,
            verb="move to the Trash",
            consequences={"overwrite": "replacing one file that's already there"},
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return ToolResult(ok=True, summary="Done.")


TRASH = ToolSpec(
    name="move_files",
    description="Move files to the Trash",
    params={},
    floor=RiskTier.SILENT,
    activation_hint="trash, delete, bin",
)


def test_the_confirmation_reads_back_a_verb_not_a_noun_phrase():
    """"report.pdf - should I go ahead?" reads identically for showing a file
    and for shredding it. The question has to carry the verb."""
    loop, _spy, said, _events = build(
        utterances=["bin that", "yes"],
        tier=RiskTier.CONFIRM_VOICE,
        tools=[VerbTool(spec=TRASH)],
        confirm=StubConfirm(verdicts=["yes"]),
    )
    loop.run()

    assert said[0] == (
        "Should I move to the Trash report.pdf, replacing one file that's already there?"
    )


def test_a_verbless_action_is_still_a_grammatical_question():
    """Without a verb, describe() falls back to the TOOL NAME, which is a noun.
    "Should I move files a, b?" is luck; "I would screenshots from today" is
    what that luck looks like when it runs out."""
    loop, _spy, said, _events = build(
        utterances=["move them", "yes"],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=StubConfirm(verdicts=["yes"]),
    )
    loop.run()

    assert said[0] == "Should I use the move files tool on a, b?"


def test_the_dry_run_line_is_a_sentence():
    loop, _spy, said, _events = build(
        utterances=["bin that"], dry_run=True, tools=[VerbTool(spec=TRASH)]
    )
    loop.run()

    assert said == [
        "Dry run: I would move to the Trash report.pdf, replacing one file that's already there."
    ]


def test_the_dry_run_line_is_a_sentence_without_a_verb_too():
    loop, _spy, said, _events = build(utterances=["move them"], dry_run=True)
    loop.run()

    assert said == ["Dry run: I would use the move files tool on a, b."]


def test_the_model_does_not_agree_to_what_we_just_refused():
    """Observed: "Okay, leaving it." followed by the model's own "Sure."."""
    llm = FakeLLM(
        turns=[
            LLMTurn(
                text="Sure, binning it now.",
                tool_calls=(ToolCall("move_files", {"src": "a"}),),
            )
        ]
    )
    loop, spy, said, _events = build(
        utterances=["bin that", "no don't"],
        tier=RiskTier.CONFIRM_VOICE,
        llm=llm,
        confirm=StubConfirm(verdicts=["no"]),
    )
    loop.run()

    assert spy.runs == []
    assert said[-1] == "Okay, leaving it."
    assert "Sure, binning it now." not in said


def test_the_model_does_not_talk_over_a_refusal_either():
    llm = FakeLLM(
        turns=[LLMTurn(text="Done!", tool_calls=(ToolCall("move_files", {"src": "a"}),))]
    )
    loop, spy, said, _events = build(
        utterances=["rm -rf /"], tier=RiskTier.REFUSE, llm=llm
    )
    loop.run()

    assert spy.runs == []
    assert "Done!" not in said


def test_the_model_does_not_talk_over_an_unknown_tool():
    llm = FakeLLM(turns=[LLMTurn(text="On it!", tool_calls=(ToolCall("nope", {}),))])
    loop, _spy, said, _events = build(utterances=["do a thing"], llm=llm)
    loop.run()

    assert said == ["I don't have a tool for that."]


# ---------------------------------------------------------------------------
# Raw speech must not reach the audit sink
#
# Redaction in safety/audit.py is the SECOND line of defence. A debug sink, a
# second sink, or a telemetry hook gets whatever the loop hands it, so the loop
# hands over shape and never substance.
# ---------------------------------------------------------------------------

CARD = "4111 1111 1111 1111"


def _all_payloads(events: Sequence[AuditEvent]) -> str:
    return repr([dict(e.payload) for e in events])


def test_unaddressed_speech_is_not_written_down_at_all():
    """`_emit("heard", text=...)` fired BEFORE the gate. In always-on mode that
    is a record of every sentence spoken in the room."""
    loop, _spy, _said, events = build(
        utterances=[f"my card is {CARD}"],
        gate=StubGate(default=StubWake(wake=False)),
        llm=FakeLLM(),
    )
    loop.run()

    assert CARD not in _all_payloads(events), "ambient speech reached the audit sink"
    heard = [e for e in events if e.kind == "heard"]
    assert heard and "text" not in heard[0].payload
    # Shape is still logged: enough to correlate, useless to a snoop.
    assert heard[0].payload["chars"] == len(f"my card is {CARD}")
    assert len(heard[0].payload["sha256_8"]) == 8


def test_a_confirmation_reply_is_never_written_down():
    """The reply is raw speech: usually "yeah", sometimes the rest of whatever
    the user happened to be saying."""
    loop, _spy, _said, events = build(
        utterances=["move them", f"yes and my card is {CARD}"],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=StubConfirm(verdicts=["yes"]),
    )
    loop.run()

    assert CARD not in _all_payloads(events)
    confirms = [e for e in events if e.kind == "confirm"]
    assert confirms and "reply" not in confirms[0].payload
    assert confirms[0].payload["verdict"] == "yes"


def test_a_rescore_logs_neither_transcript():
    """The rescore is the one place the loop holds two transcripts of the same
    sentence at once, and it used to write both of them down verbatim."""
    cloud = FakeTranscriber(source="cloud", rescore={"my card is one two": f"my card is {CARD}"})
    loop, _spy, _said, events = build(
        utterances=["my card is one two"], cloud=cloud, llm=FakeLLM()
    )
    loop.run()

    rescored = [e for e in events if e.kind == "rescored"]
    assert rescored, "no rescore happened, so this test proves nothing"
    payload = dict(rescored[0].payload)
    assert "before" not in payload and "after" not in payload
    assert CARD not in repr(payload)
    # Shape survives: you can still see that the transcript changed.
    assert payload["after_chars"] == len(f"my card is {CARD}")


# ---------------------------------------------------------------------------
# The audit builders, and provenance
# ---------------------------------------------------------------------------


def test_the_assessment_that_authorized_the_action_is_logged():
    """The loop used to hand-roll every payload, and the RiskAssessment that
    authorized the action was never written down at all."""
    loop, _spy, _said, events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()

    judgments = [e for e in events if e.kind == "judgment"]
    assert judgments, "no judgment event"
    assert judgments[0].payload["assessment"] is SYNTHETIC_ASSESSMENT
    assert judgments[0].payload["target_confidence"] == "certain"


def test_a_fully_synthetic_session_is_greppable_as_synthetic():
    """An eval sweep writes the same event kinds as a live session. Six months
    later `grep '"synthetic":true'` is the only thing that can tell them apart."""
    audit = pytest.importorskip("daa.safety.audit")
    loop, _spy, _said, events = build(utterances=["move them"], tier=RiskTier.ANNOUNCE)
    loop.run()

    records = [audit.record(e) for e in events]
    flagged = {r["kind"] for r in records if r["synthetic"] is True}
    assert "judgment" in flagged, "the judgment did not carry its provenance"
    assert "woke" in flagged, "the gate decision did not carry its provenance"


def test_every_stage_of_the_path_uses_the_shared_builders():
    audit = pytest.importorskip("daa.safety.audit")
    loop, _spy, _said, events = build(
        utterances=["move them", "yes"],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=StubConfirm(verdicts=["yes"]),
    )
    loop.run()

    kinds = [e.kind for e in events]
    # The kinds ONE ORDINARY TURN must produce. `undo` needs an undo, and the
    # grant/job/checkpoint/rollback/notice kinds need scoped consent or
    # background work -- none of which a single foreground confirmation
    # involves. Listing them here rather than skipping them keeps this test
    # about "the loop uses the builders" instead of about EVENT_KINDS' length.
    per_turn = {"judgment", "disposition", "confirmation", "execution"}
    assert per_turn <= set(audit.EVENT_KINDS)
    for kind in sorted(per_turn):
        assert kind in kinds, f"no {kind!r} event; the loop is still hand-rolling payloads"


def test_the_undo_attempt_is_logged_with_its_provenance():
    entry = StubEntry(
        action=UndoAction(description="move them back", tool="move_files", args={}),
        produced_by="move_files",
    )
    loop, _mover, _clip, _said, events = _undo_world(entry=entry, dry_run=True)

    loop.undo_last(replies=["yes"])

    undos = [e for e in events if e.kind == "undo"]
    assert undos, "an undo attempt went unlogged"
    assert undos[0].payload["produced_by"] == "move_files"
    assert undos[0].payload["committed"] is False


# ---------------------------------------------------------------------------
# CONFIRM_VISUAL: a typed approval, or an honest refusal
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeConsole:
    typed: str = "yes\n"
    is_available: bool = True
    written: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def write(self, text: str) -> None:
        self.written.append(text)

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.typed


SCRIPTED = ToolSpec(
    name="run_applescript",
    description="Run an AppleScript",
    params={"script": {"type": "string"}},
    floor=RiskTier.CONFIRM_VISUAL,
    activation_hint="applescript, automation",
)


@dataclass(slots=True)
class ScriptTool:
    spec: ToolSpec = SCRIPTED
    runs: list[ResolvedAction] = field(default_factory=list)

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return ResolvedAction(
            tool=self.spec.name,
            args=kwargs,
            targets=("System Events",),
            verb="run this script",
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return ToolResult(ok=True, summary="Ran it.")


SCRIPT_BODY = 'tell application "System Events"\n  keystroke "x"\nend tell'


def _visual_world(console: FakeConsole, *, verdicts: Sequence[str] = ("yes",)):
    tool = ScriptTool()
    llm = FakeLLM(
        turns=[LLMTurn(tool_calls=(ToolCall("run_applescript", {"script": SCRIPT_BODY}),))]
    )
    loop, _spy, said, events = build(
        utterances=["do the thing", "yes", "yes"],
        tier=RiskTier.CONFIRM_VISUAL,
        tools=[tool],
        llm=llm,
        confirm=StubConfirm(verdicts=list(verdicts)),
    )
    loop.console = console
    return loop, tool, said, events


def test_the_visual_tier_is_reachable_by_typing_yes():
    """Before the fix this tier was a permanent silent refusal: _dispatch spoke
    one sentence and returned, so nothing at CONFIRM_VISUAL could ever run."""
    console = FakeConsole(typed="yes\n")
    loop, tool, _said, events = _visual_world(console)

    loop.run()

    assert len(tool.runs) == 1, "CONFIRM_VISUAL is still unreachable"
    assert any(e.kind == "visual_confirm" and e.payload["granted"] for e in events)


def test_the_visual_approval_shows_the_whole_script():
    """The point of the tier is that the human SEES what will run."""
    console = FakeConsole(typed="yes\n")
    loop, _tool, _said, _events = _visual_world(console)

    loop.run()

    printed = "".join(console.written)
    for line in SCRIPT_BODY.splitlines():
        assert line in printed, "the script was summarised instead of shown"
    assert "run_applescript" in printed
    assert "System Events" in printed
    assert "CONFIRM_VISUAL" in printed


def test_the_visual_approval_is_typed_and_never_spoken():
    console = FakeConsole(typed="yes\n")
    loop, _tool, _said, _events = _visual_world(console, verdicts=["yes", "yes"])

    loop.run()

    assert loop.confirm.calls == [], "the visual tier asked the voice channel"


def test_anything_other_than_yes_cancels_the_visual_tier():
    console = FakeConsole(typed="y\n")  # not "yes". The friction is the feature.
    loop, tool, said, events = _visual_world(console)

    loop.run()

    assert tool.runs == []
    assert "Okay, leaving it." in said
    assert any(e.kind == "abandoned" for e in events)


def test_an_empty_answer_cancels_the_visual_tier():
    console = FakeConsole(typed="")  # EOF, e.g. a closed pipe
    loop, tool, _said, _events = _visual_world(console)

    loop.run()

    assert tool.runs == []


def test_with_no_terminal_the_visual_tier_refuses_and_says_why():
    console = FakeConsole(is_available=False)
    loop, tool, said, events = _visual_world(console)

    loop.run()

    assert tool.runs == []
    assert console.prompts == [], "we asked a console that isn't there"
    assert "screen" in said[0]
    assert any(e.kind == "deferred_visual" for e in events)


def test_a_console_that_explodes_is_a_refusal():
    class Exploding:
        def available(self):
            return True

        def write(self, text):
            raise OSError("broken pipe")

        def ask(self, prompt):
            return "yes"

    loop, tool, _said, _events = _visual_world(FakeConsole())
    loop.console = Exploding()

    loop.run()

    assert tool.runs == []


def test_a_spoken_yes_cannot_forge_the_visual_token():
    """_execute's assertion is the enforcement point, not the dispatch order."""
    loop, _spy, _said, _events = build()
    spy = SpyTool(spec=MOVE)
    action = spy.resolve(src="a", dest="b")
    with pytest.raises(AssertionError):
        loop._execute(
            spy,
            action,
            Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
            TurnOutcome(),
            confirmed=True,
            visual=object(),
        )
    assert spy.runs == []


# ---------------------------------------------------------------------------
# CONFIRM_VISUAL: a console that renders the action itself
#
# A console with a `present` attribute gets the OBJECTS -- the ResolvedAction
# and the Disposition -- instead of `_visual_detail`'s 68-column blob, so it
# can gate on the end of the script, flag the consequences separately and say
# out loud that the judgment was synthetic. None of those is possible with a
# pre-rendered string.
#
# The contract is the same one the typed path has: True, and only True, is an
# approval. `TerminalConsole` deliberately does NOT grow this attribute, so
# every test above still exercises the write/ask branch.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PresentConsole:
    """A dock-shaped console. `write`/`ask` exist only so a test can prove
    they are never reached."""

    answer: Any = True
    raises: BaseException | None = None
    is_available: bool = True
    seen: list[tuple[Any, Any]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def present(self, action: Any, disposition: Any) -> Any:
        self.seen.append((action, disposition))
        if self.raises is not None:
            raise self.raises
        return self.answer

    def write(self, text: str) -> None:
        self.written.append(text)

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "yes\n"


def _watch_execute(loop: VoiceLoop) -> dict[str, Any]:
    """Capture the kwargs `_dispatch` hands `_execute`, without changing it."""
    captured: dict[str, Any] = {}
    real = loop._execute

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(*args, **kwargs)

    loop._execute = spy  # type: ignore[method-assign]
    return captured


def test_a_present_console_approves_and_still_carries_the_visual_token():
    from daa.voice.loop import _VISUAL_OK

    console = PresentConsole(answer=True)
    loop, tool, _said, events = _visual_world(console)
    captured = _watch_execute(loop)

    loop.run()

    assert len(tool.runs) == 1
    assert captured["visual"] is _VISUAL_OK, "the dock's boolean lost the token on the way in"
    assert any(e.kind == "visual_confirm" and e.payload["granted"] for e in events)
    # The objects, not a blob: the card has to be able to gate on the script.
    action, disposition = console.seen[0]
    assert action.args["script"] == SCRIPT_BODY
    assert disposition.tier is RiskTier.CONFIRM_VISUAL


def test_a_present_console_that_says_false_refuses_and_says_so():
    console = PresentConsole(answer=False)
    loop, tool, said, events = _visual_world(console)

    loop.run()

    assert tool.runs == []
    assert "Okay, leaving it." in said
    assert any(e.kind == "abandoned" for e in events)
    assert any(e.kind == "visual_confirm" and not e.payload["granted"] for e in events)


@pytest.mark.parametrize(
    "answer",
    [None, "yes", "YES", 1, 1.0, ["yes"], object()],
    ids=["none", "yes-string", "YES-string", "one", "one-float", "list", "object"],
)
def test_only_the_literal_True_approves(answer: Any):
    """`is True`, not truthiness.

    A `present` that returns a non-empty string, a Mock, or 1 is a BUG, and
    the safe reading of a bug on this path is "not approved". A test double
    that silently approves a real action is the worst failure available here.
    """
    console = PresentConsole(answer=answer)
    loop, tool, said, _events = _visual_world(console)

    loop.run()

    assert tool.runs == [], f"present() returning {answer!r} approved an action"
    assert "Okay, leaving it." in said


def test_a_present_that_raises_is_a_refusal_and_is_logged():
    console = PresentConsole(raises=OSError("the dock died"))
    loop, tool, said, events = _visual_world(console)

    loop.run()

    assert tool.runs == []
    assert "Okay, leaving it." in said
    errors = [e for e in events if e.kind == "error" and e.payload.get("where") == "console"]
    assert len(errors) == 1
    assert "the dock died" in errors[0].payload["error"]


def test_a_present_console_is_never_asked_to_write_or_ask():
    """The two branches are exclusive. A console that got both would be a
    console whose behaviour depended on the order of two getattrs."""
    console = PresentConsole(answer=True)
    loop, _tool, _said, _events = _visual_world(console)

    loop.run()

    assert console.written == [], "the dock was sent a pre-rendered 68-column blob"
    assert console.prompts == [], "the dock was asked to read from stdin"
    assert len(console.seen) == 1


def test_a_present_console_with_no_screen_still_refuses_before_presenting():
    console = PresentConsole(answer=True, is_available=False)
    loop, tool, said, events = _visual_world(console)

    loop.run()

    assert tool.runs == []
    assert console.seen == [], "a card was raised on a console that said it wasn't there"
    assert "screen" in said[0]
    assert any(e.kind == "deferred_visual" for e in events)


def test_the_terminal_console_has_no_present_attribute():
    """The reason the ten-line edit is safe. If TerminalConsole ever grows a
    `present`, every console test above silently reroutes to the other branch
    and stops testing what it says it tests."""
    from daa.voice.loop import TerminalConsole

    assert not hasattr(TerminalConsole, "present")
    assert not hasattr(TerminalConsole(), "present")
    assert hasattr(TerminalConsole, "write") and hasattr(TerminalConsole, "ask")
