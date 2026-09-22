"""The brain, taking more than one step.

Before this file daa got ONE shot per utterance: the model was asked once,
whatever it called was run, and the result went nowhere. It could not do
anything that took two steps, could not react to what it read, and could not
recover from a refusal -- which is most of what driving a browser or a GUI
actually is.

The tests here are about four things, in descending order of how much they
would cost to get wrong:

  1. WHAT GOES BACK TO THE MODEL. Feeding results back is an egress. A tool
     result may carry daa's own sentence about the step and the SHAPE of the
     structured data, and nothing else. `test_page_text_never_reaches_the_model`
     and `test_control_labels_never_reach_the_model` are the load-bearing ones.
  2. THE CHAIN STILL RUNS, EVERY STEP. resolve -> risk -> policy -> the tier's
     ceremony -> `_execute`. Step six is not cheaper than step one.
  3. IT STOPS. A step budget, a wall clock and a repetition stop, each of which
     ENDS THE TURN OUT LOUD rather than going quiet.
  4. NOTHING ASKS FOR A GRANT. The machinery is right there and wiring it in
     would turn per-action consent into one question per plan. That is a
     product decision nobody has made.

Everything is stubbed, as in test_voice_loop.py, which is where these stubs
come from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from daa.config import Settings
from daa.contracts import (
    AuditEvent,
    ResolvedAction,
    RiskTier,
    ToolResult,
    ToolSpec,
)
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import VoiceLoop
from daa.voice.stt import FakeTranscriber
from daa.voice.transcript import TOOL_PREFIX
from test_voice_loop import (
    MOVE,
    WEATHER,
    SpyTool,
    StubConfirm,
    StubGate,
    StubJournal,
    StubRegistry,
    StubRisk,
    StubRouter,
    stub_policy,
)

# ---------------------------------------------------------------------------
# A tool whose result is entirely under the test's control
# ---------------------------------------------------------------------------

READ = ToolSpec(
    name="read_page",
    description="Read the page that is open",
    params={},
    floor=RiskTier.SILENT,
    activation_hint="read the page",
)
PRESS = ToolSpec(
    name="press_button",
    description="Press a control",
    params={"label": {"type": "string"}},
    floor=RiskTier.ANNOUNCE,
    activation_hint="press the button",
)


@dataclass(slots=True)
class ScriptedTool:
    """Returns exactly the ToolResult the test wrote, and counts its runs."""

    spec: ToolSpec
    results: list[ToolResult] = field(default_factory=list)
    fallback: ToolResult = field(
        default_factory=lambda: ToolResult(ok=True, summary="Done.", data={})
    )
    runs: list[ResolvedAction] = field(default_factory=list)
    targets: tuple[str, ...] = ("something",)
    reason: str = ""

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return ResolvedAction(
            tool=self.spec.name,
            args=dict(kwargs) | ({"reason": self.reason} if self.reason else {}),
            targets=self.targets,
            verb="do the thing",
            explicit=True,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return self.results.pop(0) if self.results else self.fallback


def build(
    *,
    tools: Sequence[Any],
    turns: Sequence[LLMTurn],
    tier: RiskTier = RiskTier.ANNOUNCE,
    settings: Settings | None = None,
    confirm: Any = None,
    router: Any = None,
    now: Any = None,
    replies: Sequence[str] = (),
) -> tuple[VoiceLoop, FakeLLM, list[AuditEvent]]:
    registry = StubRegistry()
    for tool in tools:
        registry.register(tool)
    events: list[AuditEvent] = []
    llm = FakeLLM(turns=list(turns))
    loop = VoiceLoop(
        settings=settings if settings is not None else Settings(dry_run=False),
        local_stt=FakeTranscriber(source="local"),
        gate=StubGate(),
        router=router if router is not None else StubRouter(),
        risk=StubRisk(),
        confirm=confirm if confirm is not None else StubConfirm(verdicts=list(replies) and []),
        registry=registry,
        policy_decide=stub_policy(tier),
        journal=StubJournal(),
        audit=events.append,
        llm=llm,
        now=now,
    )
    return loop, llm, events


def asks(llm: FakeLLM) -> list[list[Mapping[str, str]]]:
    return [messages for messages, _specs in llm.seen]


def tool_lines(llm: FakeLLM, ask: int = -1) -> list[str]:
    return [m["content"] for m in asks(llm)[ask] if str(m["content"]).startswith(TOOL_PREFIX)]


# ---------------------------------------------------------------------------
# 1. results go back, and the model reacts to them
# ---------------------------------------------------------------------------


def test_the_result_of_a_step_reaches_the_next_ask():
    tool = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="Six headings.")])
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(text="Six headings on that page."),
        ],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("what's on this page")

    assert len(asks(llm)) == 2, "the model was never asked again after the step"
    assert tool_lines(llm) == [
        f"{TOOL_PREFIX} read_page: ok — Six headings."
    ]
    assert loop.said == ["Six headings on that page."]


def test_a_two_step_task_runs_both_steps_in_order():
    read = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="I can see an ok button.")])
    press = ScriptedTool(spec=PRESS, results=[ToolResult(ok=True, summary="Pressed ok.")])
    loop, llm, _ = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Pressed it."),
        ],
        tier=RiskTier.SILENT,
    )
    outcome = loop.handle_text("press ok on that page")

    assert len(read.runs) == 1 and len(press.runs) == 1
    assert outcome.steps == 2
    assert outcome.stopped == ""
    assert len(asks(llm)) == 3
    # The read was SILENT and said nothing; the press is an ANNOUNCE tool, so
    # it narrated itself; the last word is the model's, written having seen
    # both results.
    assert loop.said == ["Pressed ok.", "Pressed it."]


def test_a_refusal_comes_back_as_a_result_the_model_can_recover_from():
    tool = ScriptedTool(spec=PRESS)
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "pay"}),)),
            LLMTurn(text="I won't press that one."),
        ],
        tier=RiskTier.REFUSE,
    )
    loop.handle_text("press pay")

    assert tool.runs == [], "a refused action ran"
    line = tool_lines(llm)[0]
    assert "press_button: blocked" in line
    assert "I won't do that" in line


def test_a_declined_confirmation_comes_back_as_a_result():
    tool = ScriptedTool(spec=PRESS)
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Left it alone."),
        ],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=StubConfirm(verdicts=["no"]),
    )
    loop.handle_text("press ok", replies=["no thanks"])

    assert tool.runs == []
    line = tool_lines(llm)[0]
    assert "blocked" in line
    assert "leaving it" in line.lower(), "the model was not told the user said no"


def test_an_action_that_resolved_to_nothing_comes_back_as_a_result():
    # The sentence the brief names: "I could not find the ok button" has to be
    # something the model can react to, not a dead end.
    tool = ScriptedTool(
        spec=PRESS, targets=(), reason="I could not find the ok button."
    )
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="I can't see an ok button."),
        ],
    )
    loop.handle_text("press ok")

    assert tool.runs == []
    assert "I could not find the ok button." in tool_lines(llm)[0]


def test_a_failed_step_comes_back_as_failed():
    tool = ScriptedTool(
        spec=PRESS, results=[ToolResult(ok=False, summary="The page never loaded.", error="timeout")]
    )
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="That page wouldn't load."),
        ],
    )
    loop.handle_text("press ok")
    assert "press_button: failed" in tool_lines(llm)[0]
    assert "The page never loaded." in tool_lines(llm)[0]


# ---------------------------------------------------------------------------
# 2. the egress boundary
# ---------------------------------------------------------------------------


def test_page_text_never_reaches_the_model():
    """`read_page` is SILENT; `summarise_page` is ANNOUNCE, and the difference
    IS the announcement that page text is about to be sent somewhere. If the
    agent loop shipped `data["text"]` back, read_page would quietly become
    summarise_page with no announcement and no confirmation."""
    body = "Dear Sanjay, your balance is 4,012 pounds and your card ends 4242."
    tool = ScriptedTool(
        spec=READ,
        results=[
            ToolResult(
                ok=True,
                summary="That page is about 900 words, and you're signed in.",
                data={
                    "text": body,
                    "title": "Statements — April",
                    "url": "https://bank.example/statements/april",
                    "site": "bank.example",
                    "headings": ["April", "May"],
                    "words": 900,
                    "logged_in": True,
                    "private": True,
                },
            )
        ],
    )
    loop, llm, _ = build(
        tools=[tool],
        turns=[LLMTurn(tool_calls=(ToolCall("read_page", {}),)), LLMTurn(text="You're signed in.")],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("what's on this page")

    everything = "\n".join(str(m["content"]) for m in asks(llm)[-1])
    assert body not in everything
    assert "balance" not in everything
    assert "Statements" not in everything
    assert "statements/april" not in everything
    # And what IS there is shape, which is what a next step is chosen from.
    line = tool_lines(llm)[0]
    assert "logged_in=true" in line and "words=900" in line


def test_control_labels_never_reach_the_model():
    tool = ScriptedTool(
        spec=READ,
        results=[
            ToolResult(
                ok=True,
                summary="There are four controls on that window.",
                data={
                    "controls": ["Delete everything", "Pay £4,012 now", "Cancel", "Help"],
                    "labels": ["Delete everything"],
                    "count": 4,
                },
            )
        ],
    )
    loop, llm, _ = build(
        tools=[tool],
        turns=[LLMTurn(tool_calls=(ToolCall("read_page", {}),)), LLMTurn(text="Four controls.")],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("what's on screen")

    line = tool_lines(llm)[0]
    assert "Pay" not in line and "Delete everything" not in line
    assert "controls_count=4" in line


def test_what_goes_back_is_capped():
    tool = ScriptedTool(
        spec=READ, results=[ToolResult(ok=True, summary="word " * 500, data={})]
    )
    loop, llm, _ = build(
        tools=[tool],
        turns=[LLMTurn(tool_calls=(ToolCall("read_page", {}),)), LLMTurn(text="Done.")],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("read it")
    assert len(tool_lines(llm)[0]) <= loop.transcript.max_tool_chars


# ---------------------------------------------------------------------------
# 3. the chain, on every step
# ---------------------------------------------------------------------------


def test_every_step_goes_through_risk_and_policy_not_just_the_first():
    read = ScriptedTool(spec=READ)
    press = ScriptedTool(spec=PRESS)
    registry = StubRegistry()
    registry.register(read)
    registry.register(press)
    events: list[AuditEvent] = []
    risk = StubRisk()
    policy = stub_policy(RiskTier.SILENT)
    loop = VoiceLoop(
        settings=Settings(dry_run=False),
        local_stt=FakeTranscriber(source="local"),
        gate=StubGate(),
        router=StubRouter(),
        risk=risk,
        confirm=StubConfirm(),
        registry=registry,
        policy_decide=policy,
        journal=StubJournal(),
        audit=events.append,
        llm=FakeLLM(turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Done."),
        ]),
    )
    loop.handle_text("press ok")

    assert [a.tool for a in risk.calls] == ["read_page", "press_button"]
    assert [name for name, _ in policy.seen] == ["read_page", "press_button"]
    assert len([e for e in events if e.kind == "execution"]) == 2


def test_a_mutating_step_still_confirms_and_a_read_only_step_still_stays_silent():
    read = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="I can see ok.")])
    press = ScriptedTool(spec=PRESS, results=[ToolResult(ok=True, summary="Pressed ok.")])
    registry = StubRegistry()
    registry.register(read)
    registry.register(press)
    # SILENT for the read (its floor), CONFIRM_VOICE for the press.
    def policy(action, spec, assessment, settings):
        from daa.contracts import Disposition

        tier = RiskTier.CONFIRM_VOICE if spec.name == "press_button" else RiskTier.SILENT
        return Disposition(tier=tier, reason="stub", assessment=assessment)

    loop = VoiceLoop(
        settings=Settings(dry_run=False),
        local_stt=FakeTranscriber(source="local"),
        gate=StubGate(),
        router=StubRouter(),
        risk=StubRisk(),
        confirm=StubConfirm(verdicts=["yes"]),
        registry=registry,
        policy_decide=policy,
        journal=StubJournal(),
        audit=[].append,
        llm=FakeLLM(turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Pressed it."),
        ]),
    )
    loop.handle_text("press ok on that page", replies=["yes"])

    # The read said NOTHING -- it did not interrupt to narrate a step.
    assert "I can see ok." not in loop.said
    # The mutation asked, out loud, for itself.
    assert any(line.startswith("Should I") for line in loop.said)
    assert press.runs and read.runs


def test_a_tool_the_router_did_not_activate_still_escalates_on_a_later_step():
    read = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="I can see ok.")])
    press = ScriptedTool(spec=PRESS)
    loop, _llm, events = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.SILENT,
        router=StubRouter(keep=("read_page",)),
        confirm=StubConfirm(verdicts=["no"]),
    )
    loop.handle_text("read it then press ok", replies=["no"])

    misses = [e for e in events if e.kind == "router_miss"]
    assert [e.payload["tool"] for e in misses] == ["press_button"], (
        "the router-miss escalation stopped firing after the first step"
    )
    raised = [e for e in events if e.kind == "tier_raised"]
    assert raised and raised[0].payload["to"] == "CONFIRM_VOICE"
    assert press.runs == []


def test_the_router_runs_once_for_the_whole_turn():
    read = ScriptedTool(spec=READ)
    press = ScriptedTool(spec=PRESS)
    router = StubRouter()
    loop, _llm, _ = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.SILENT,
        router=router,
    )
    loop.handle_text("press ok")
    assert router.seen == ["press ok"], "the router was asked more than once per utterance"


# ---------------------------------------------------------------------------
# 4. it stops, and it says so
# ---------------------------------------------------------------------------


def _always_calls(name: str, **args: Any) -> FakeLLM:
    """A model that never stops asking for the same kind of work."""
    return FakeLLM(turns=[
        LLMTurn(tool_calls=(ToolCall(name, dict(args, n=i)),)) for i in range(50)
    ])


def test_the_step_budget_ends_the_turn_out_loud():
    tool = ScriptedTool(spec=READ)
    registry = StubRegistry()
    registry.register(tool)
    events: list[AuditEvent] = []
    loop = VoiceLoop(
        settings=Settings(dry_run=False, agent_max_steps=3),
        local_stt=FakeTranscriber(source="local"),
        gate=StubGate(),
        router=StubRouter(),
        risk=StubRisk(),
        confirm=StubConfirm(),
        registry=registry,
        policy_decide=stub_policy(RiskTier.SILENT),
        journal=StubJournal(),
        audit=events.append,
        llm=_always_calls("read_page"),
    )
    outcome = loop.handle_text("keep going")

    assert outcome.steps == 3
    assert len(tool.runs) == 3
    assert outcome.stopped == "steps"
    assert loop.said, "the loop ran out of budget and went quiet"
    assert "carry on" in loop.said[-1]
    stops = [e for e in events if e.kind == "agent_stopped"]
    assert stops and stops[0].payload == {"reason": "steps", "steps": 3}


def test_the_wall_clock_ends_the_turn_out_loud():
    tool = ScriptedTool(spec=READ)
    registry = StubRegistry()
    registry.register(tool)
    ticks = iter([0.0] + [i * 10.0 for i in range(1, 40)])
    loop = VoiceLoop(
        settings=Settings(dry_run=False, agent_max_steps=100, agent_max_seconds=25.0),
        local_stt=FakeTranscriber(source="local"),
        gate=StubGate(),
        router=StubRouter(),
        risk=StubRisk(),
        confirm=StubConfirm(),
        registry=registry,
        policy_decide=stub_policy(RiskTier.SILENT),
        journal=StubJournal(),
        audit=[].append,
        llm=_always_calls("read_page"),
        now=lambda: next(ticks),
    )
    outcome = loop.handle_text("keep going")

    assert outcome.stopped == "time"
    assert outcome.steps < 100
    assert "longer than I should" in loop.said[-1]


def test_the_same_call_twice_in_a_row_stops_the_loop():
    tool = ScriptedTool(spec=READ)
    loop, _llm, events = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {"tab": "1"}),)),
            LLMTurn(tool_calls=(ToolCall("read_page", {"tab": "1"}),)),
            LLMTurn(text="never reached"),
        ],
        tier=RiskTier.SILENT,
    )
    outcome = loop.handle_text("read it")

    assert len(tool.runs) == 1, "the identical repeat was executed anyway"
    assert outcome.stopped == "repeat"
    assert "twice in a row" in loop.said[-1]
    assert [e.payload["reason"] for e in events if e.kind == "agent_stopped"] == ["repeat"]


def test_argument_order_does_not_disguise_a_repeat():
    tool = ScriptedTool(spec=PRESS)
    loop, _llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"a": 1, "b": 2}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"b": 2, "a": 1}),)),
        ],
        tier=RiskTier.SILENT,
    )
    outcome = loop.handle_text("press it")
    assert outcome.stopped == "repeat"
    assert len(tool.runs) == 1


def test_a_different_call_in_between_is_not_a_repeat():
    read = ScriptedTool(spec=READ)
    press = ScriptedTool(spec=PRESS)
    loop, _llm, _ = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.SILENT,
    )
    outcome = loop.handle_text("press ok and check")
    assert outcome.stopped == ""
    assert len(read.runs) == 2 and len(press.runs) == 1


def test_a_dry_run_tells_the_model_so_rather_than_inviting_a_retry():
    tool = ScriptedTool(spec=PRESS)
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="That was a dry run."),
        ],
        settings=Settings(dry_run=True),
    )
    loop.handle_text("press ok")

    assert tool.runs == [], "dry run reached the tool"
    line = tool_lines(llm)[0]
    assert "DRY RUN" in line
    assert "do not retry" in line.lower()


# ---------------------------------------------------------------------------
# 5. what the loop deliberately does NOT do
# ---------------------------------------------------------------------------


def test_the_agent_loop_never_asks_for_a_scoped_grant():
    """Per-action confirmation, on purpose.

    `request_grant` would let daa ask once for a whole plan. That trades the
    documented top risk (consent fatigue) for a different one -- a single yes
    covering work the user has stopped picturing -- and nobody has made that
    call. If this test ever fails, it should be because somebody decided to
    change it.
    """
    press = ScriptedTool(spec=PRESS)
    loop, _llm, events = build(
        tools=[press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "next"}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.CONFIRM_VOICE,
        confirm=StubConfirm(verdicts=["yes", "yes"]),
    )
    loop.handle_text("press ok then next", replies=["yes", "yes"])

    assert loop.active_grant is None
    assert list(loop.grants.live(now=loop.now())) == []
    assert [e.kind for e in events if e.kind == "grant"] == []
    # Two mutations, two questions. That is the whole policy.
    assert len([line for line in loop.said if line.startswith("Should I")]) == 2


def test_the_loop_never_emits_a_speaking_phase():
    # The dock contract has no `speaking` phase any more -- emitting one renders
    # as `degraded`. While the agent loop works, the phase is `thinking`, and
    # daa's own words travel as `spoke`.
    tool = ScriptedTool(spec=PRESS)
    loop, _llm, events = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Pressed it."),
        ],
    )
    loop.handle_text("press ok")
    assert not any("speaking" in str(e.kind) for e in events)
    assert not any("speaking" in str(e.payload) for e in events)
    assert [e.kind for e in events if e.kind == "spoke"], "daa said nothing at all"


def test_intermediate_steps_are_visible_through_the_existing_audit_events():
    from daa.ui.protocol import LOAD_BEARING_KINDS

    read = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="Six headings.")])
    press = ScriptedTool(spec=PRESS, results=[ToolResult(ok=True, summary="Pressed ok.")])
    loop, _llm, events = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("read it then press ok")

    # Both steps reach the dock through kinds it already renders. No parallel
    # channel was invented for progress.
    executions = [e for e in events if e.kind == "execution"]
    assert len(executions) == 2
    assert "execution" in LOAD_BEARING_KINDS


def test_a_single_step_turn_is_unchanged_apart_from_one_extra_ask():
    """The old behaviour is a special case of the new one.

    A model that calls one tool and then has nothing more to say must still
    produce exactly the tool's sentence and nothing else -- in particular, the
    sentence it wrote BEFORE policy had an opinion is still not spoken.
    """
    tool = ScriptedTool(spec=PRESS, results=[ToolResult(ok=True, summary="Pressed ok.")])
    loop, _llm, _ = build(
        tools=[tool],
        turns=[LLMTurn(text="Sure!", tool_calls=(ToolCall("press_button", {"label": "ok"}),))],
    )
    loop.handle_text("press ok")
    assert loop.said == ["Pressed ok."]


# ---------------------------------------------------------------------------
# 6. daa say, end to end
# ---------------------------------------------------------------------------


def test_daa_say_drives_a_two_tool_task_end_to_end(monkeypatch, capsys):
    from daa import cli

    read = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="I can see an ok button.")])
    press = ScriptedTool(spec=PRESS, results=[ToolResult(ok=True, summary="Pressed ok.")])
    loop, _llm, _ = build(
        tools=[read, press],
        turns=[
            LLMTurn(tool_calls=(ToolCall("read_page", {}),)),
            LLMTurn(tool_calls=(ToolCall("press_button", {"label": "ok"}),)),
            LLMTurn(text="Pressed the ok button for you."),
        ],
    )
    monkeypatch.setattr(cli, "_build", lambda settings, mic=None: loop)

    assert cli.main(["say", "press", "ok", "on", "that", "page"]) == 0
    out = capsys.readouterr().out
    assert "daa> Pressed ok." in out
    assert "daa> Pressed the ok button for you." in out
    assert len(read.runs) == 1 and len(press.runs) == 1


@pytest.mark.parametrize("tool_name", ["read_page", "no_such_tool"])
def test_a_hallucinated_tool_name_is_a_result_not_the_end_of_the_turn(tool_name):
    tool = ScriptedTool(spec=READ, results=[ToolResult(ok=True, summary="Six headings.")])
    loop, llm, _ = build(
        tools=[tool],
        turns=[
            LLMTurn(tool_calls=(ToolCall(tool_name, {}),)),
            LLMTurn(text="Done."),
        ],
        tier=RiskTier.SILENT,
    )
    loop.handle_text("read it")
    assert len(asks(llm)) == 2
    line = tool_lines(llm)[0]
    if tool_name == "no_such_tool":
        assert "blocked" in line and "don't have a tool" in line
    else:
        assert "ok" in line


def test_an_unused_spec_import_stays_referenced():
    # MOVE / WEATHER / SpyTool come from test_voice_loop; touching them here
    # keeps the shared fixtures honest if that file is refactored.
    assert MOVE.name == "move_files" and WEATHER.name == "get_weather"
    assert SpyTool(spec=MOVE).spec is MOVE
