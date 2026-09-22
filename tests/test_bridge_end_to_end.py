"""A real `VoiceLoop` driven over a pipe by the frames the dock actually sends.

This is the test worth more than the unit tests around it. Everything below
the wire is the shipping code: the real `VoiceLoop`, the real `_dispatch`, the
real `_confirm_visual`, the real `_execute` with its assertions. Only the four
things that would need hardware or keys are fakes -- the tool, the model
and the policy -- and the frames pushed in are byte-for-byte the ones
`AppModel.swift` sends.

What it is here to catch:

  * a card that never reaches the dock, or reaches it unrenderable;
  * a `granted:true` that does not actually run the tool, or a `granted:false`
    that does;
  * a spoken "yes" that stops arriving through `_next_reply` because the mic
    seam changed shape;
  * a gate that gets skipped for an open microphone, or applied to a key press;
  * an `execution` row that reaches the dock without the `undo_id` that makes
    the ↩︎ button do something.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Self

from daa.config import Settings
from daa.contracts import (
    AuditEvent,
    Disposition,
    ResolvedAction,
    RiskTier,
    ToolResult,
    UndoAction,
)
from daa.ui import protocol
from daa.ui.bridge import Bridge
from daa.ui.protocol import Method, Request, Response
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import VoiceLoop
from test_bridge_wire import (  # the pipes and the stubs, shared deliberately
    ASSESSMENT,
    SCRIPT_BODY,
    PipeIn,
    PipeOut,
    ScriptTool,
    StubConfirm,
    StubRegistry,
    stub_policy,
)

TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: str


@dataclass(slots=True)
class RecordingJournal:
    """Enough of an UndoJournal for the `undo_id` to be real."""

    written: list[Any] = field(default_factory=list)

    def record(self, undo: Any, *, produced_by: str = "", checkpoint_id: Any = None) -> Any:
        self.written.append(undo)
        return JournalEntry(id=f"u{len(self.written)}")

    def peek(self) -> Any:
        return None


@dataclass(slots=True)
class StubWake:
    wake: bool = True
    end_of_turn: bool = True
    needs_planner: bool = False
    addressed_p: float = 0.99
    latency_ms: float = 1.0
    synthetic: bool = True


@dataclass(slots=True)
class StubGate:
    answer: StubWake = field(default_factory=StubWake)
    seen: list[str] = field(default_factory=list)

    def should_wake(self, utterance: str, context: Any) -> StubWake:
        self.seen.append(utterance)
        return self.answer


@dataclass(slots=True)
class UndoableTool(ScriptTool):
    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return ToolResult(
            ok=True,
            summary="Ran the script.",
            undo=UndoAction(description="undo the script", tool="run_applescript", args={}),
        )


@dataclass(slots=True)
class World:
    bridge: Bridge
    loop: VoiceLoop
    out: PipeOut
    stdin: PipeIn
    tool: Any
    gate: StubGate
    journal: RecordingJournal
    events: list[AuditEvent]
    thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self.thread = threading.Thread(target=self.bridge.serve, name="serve", daemon=True)
        self.thread.start()
        self.stdin.push(protocol.request("h1", Method.HELLO, proto=1, app="0.1.0"))
        self.out.wait_for_method(Method.READY)
        return self

    def __exit__(self, *exc: object) -> None:
        self.stdin.close()
        if self.thread is not None:
            self.thread.join(timeout=TIMEOUT)

    # -- the frames the dock sends --------------------------------------

    def typed(self, text: str) -> None:
        """`AppModel.sendTypedText`."""
        self.stdin.push(protocol.event(Method.CONTROL_TEXT, text=text, addressed=True))

    def said(self, text: str, *, addressed: bool, complete: bool = True) -> None:
        """`AppModel.sendUtterance`."""
        self.stdin.push(
            protocol.event(
                Method.MIC_UTTERANCE,
                text=text,
                confidence=0.93,
                startedAt=12.0,
                complete=complete,
                addressed=addressed,
            )
        )

    def answer_card(self, *, granted: bool, reason: str) -> str:
        card = self.out.wait(
            lambda fs: any(
                isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST for f in fs
            )
        )
        req = next(
            f for f in card if isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST
        )
        self.stdin.push(
            protocol.response(req.id, True, {"granted": granted, "reason": reason})
        )
        return req.id

    def audits(self, kind: str) -> list[dict[str, Any]]:
        return [
            f.params
            for f in self.out.frames()
            if getattr(f, "method", "") == Method.AUDIT and f.params.get("kind") == kind
        ]

    def wait_audit(self, kind: str) -> dict[str, Any]:
        self.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.AUDIT and f.params.get("kind") == kind
                for f in fs
            )
        )
        return self.audits(kind)[0]


def world(
    *,
    tier: RiskTier = RiskTier.CONFIRM_VISUAL,
    dry_run: bool = False,
    gate: StubGate | None = None,
    verdicts: list[str] | None = None,
    confirm_timeout_s: float = 30.0,
) -> World:
    tool = UndoableTool()
    registry = StubRegistry()
    registry.register(tool)
    gate = gate if gate is not None else StubGate()
    journal = RecordingJournal()
    events: list[AuditEvent] = []
    loop = VoiceLoop(
        settings=Settings(dry_run=dry_run),
        local_stt=None,
        gate=gate,
        router=None,
        risk=None,
        confirm=StubConfirm(verdicts=verdicts if verdicts is not None else ["yes"]),
        registry=registry,
        policy_decide=stub_policy(tier),
        journal=journal,
        audit=events.append,
        llm=FakeLLM(
            turns=[LLMTurn(tool_calls=(ToolCall("run_applescript", {"script": SCRIPT_BODY}),))]
        ),
    )
    out, stdin = PipeOut(), PipeIn()
    bridge = Bridge(
        loop=loop, stdin=stdin, stdout=out, confirm_timeout_s=confirm_timeout_s
    )
    return World(
        bridge=bridge,
        loop=loop,
        out=out,
        stdin=stdin,
        tool=tool,
        gate=gate,
        journal=journal,
        events=events,
    )


# ---------------------------------------------------------------------------
# The card, end to end
# ---------------------------------------------------------------------------


def test_a_typed_turn_raises_a_card_the_dock_can_render_in_full():
    with world() as w:
        w.typed("run the script")
        frames = w.out.wait(
            lambda fs: any(
                isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST for f in fs
            )
        )
        card = next(
            f for f in frames if isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST
        ).params

        # Everything `ApprovalCard.init?` refuses to render without.
        assert card["tool"] == "run_applescript"
        assert card["phrase"]
        assert card["tier"] == "CONFIRM_VISUAL"
        assert card["dryRun"] is False
        # The script, in full. A card assembled from an elision is a consent
        # record for something nobody saw.
        script = next(a for a in card["args"] if a["key"] == "script")
        assert script["value"] == SCRIPT_BODY
        assert script["isProgram"] is True
        assert card["assessment"]["synthetic"] is True
        assert 5_000 <= card["expiresInMs"] <= 300_000
        # And the dock is told to stop looking calm.
        assert any(
            getattr(f, "method", "") == Method.STATE and f.params.get("phase") == "awaiting"
            for f in w.out.frames()
        )
        w.answer_card(granted=False, reason="cancelled")


def test_a_held_approval_runs_the_action_and_the_undo_id_reaches_the_dock():
    """The ↩︎ affordance appears only when `execution` carried `undo_id` and
    `dry_run` was false. Without the field the button is decoration."""
    with world() as w:
        w.typed("run the script")
        w.answer_card(granted=True, reason="approved")
        w.out.wait(lambda fs: bool(w.tool.runs))

        assert len(w.tool.runs) == 1
        execution = w.wait_audit("execution")["payload"]
        assert execution["undo_id"] == "u1"
        assert execution["dry_run"] is False
        assert execution["summary"] == "Ran the script."
        assert w.audits("visual_confirm")[0]["payload"]["granted"] is True


def test_a_cancelled_card_runs_nothing_and_says_so_out_loud():
    with world() as w:
        w.typed("run the script")
        w.answer_card(granted=False, reason="cancelled")
        w.out.wait(
            lambda fs: any(getattr(f, "method", "") == Method.SPEAK for f in fs)
        )

        assert w.tool.runs == []
        spoken = [
            f.params["text"] for f in w.out.frames() if getattr(f, "method", "") == Method.SPEAK
        ]
        assert "Okay, leaving it." in spoken
        assert w.audits("visual_confirm")[0]["payload"]["granted"] is False
        assert w.audits("abandoned")


def test_a_response_the_dock_could_not_render_is_never_an_approval():
    """`ok:false` from the dock means the card was refused at the boundary.
    There is no consent to something that was not displayed."""
    with world() as w:
        w.typed("run the script")
        frames = w.out.wait(
            lambda fs: any(isinstance(f, Request) for f in fs)
        )
        req = next(f for f in frames if isinstance(f, Request))
        w.stdin.push(protocol.error(req.id, "unreadable", "the request could not be rendered"))
        w.out.wait(lambda fs: any(getattr(f, "method", "") == Method.SPEAK for f in fs))

        assert w.tool.runs == []
        assert w.audits("visual_confirm")[0]["payload"]["granted"] is False


def test_a_card_nobody_answers_expires_closed_and_is_withdrawn():
    """An approval you walked away from is not an approval."""
    with world(confirm_timeout_s=0.05) as w:
        w.typed("run the script")
        w.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.CONFIRM_CANCEL for f in fs
            )
        )
        w.out.wait(lambda fs: any(getattr(f, "method", "") == Method.SPEAK for f in fs))

        assert w.tool.runs == []
        cancel = next(
            f for f in w.out.frames() if getattr(f, "method", "") == Method.CONFIRM_CANCEL
        )
        assert cancel.params["reason"] == "timeout"
        # Distinguishable from a refusal, which is the whole point of auditing
        # the reason: `daa doctor` and the History window must be able to tell
        # a walked-away-from card from a user who said no.
        card = w.wait_audit("confirm_card")["payload"]
        assert card["reason"] == "timeout" and card["granted"] is False


def test_a_replayed_token_cannot_approve_a_second_action():
    with world() as w:
        w.typed("run the script")
        token = w.answer_card(granted=False, reason="cancelled")
        w.out.wait(lambda fs: any(getattr(f, "method", "") == Method.SPEAK for f in fs))
        # The same token again, this time claiming approval.
        w.stdin.push(protocol.response(token, True, {"granted": True, "reason": "approved"}))
        error = w.wait_audit("error")["payload"]

        assert error["where"] == "confirm"
        assert w.tool.runs == []


# ---------------------------------------------------------------------------
# The voice path still works
# ---------------------------------------------------------------------------


def test_a_spoken_yes_still_arrives_through_next_reply():
    """`run()` is untouched, so `self._segments` still exists, so a spoken
    answer to a CONFIRM_VOICE readback comes through the same path as any
    other utterance."""
    with world(tier=RiskTier.CONFIRM_VOICE) as w:
        w.said("move my screenshots", addressed=True)
        w.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.SPEAK and "Should I" in f.params["text"]
                for f in fs
            )
        )
        assert w.tool.runs == [], "it ran before anyone answered"

        w.said("yes", addressed=True)
        w.out.wait(lambda fs: bool(w.tool.runs))
        assert len(w.tool.runs) == 1
        assert w.loop.confirm.calls == ["yes"]


def test_a_spoken_yes_can_never_authorise_the_visual_tier():
    """The load-bearing one. Voice arrives on the same channel the request
    did, which is exactly what CONFIRM_VISUAL exists to exclude."""
    with world(tier=RiskTier.CONFIRM_VISUAL) as w:
        w.said("run the script", addressed=True)
        w.out.wait(
            lambda fs: any(
                isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST for f in fs
            )
        )
        w.said("yes", addressed=True)
        w.said("yes", addressed=True)
        # The card is still the only thing that can answer, so nothing ran.
        assert w.tool.runs == []
        w.answer_card(granted=False, reason="escaped")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_push_to_talk_skips_the_gate():
    """Holding a key is an unambiguous act of address -- the same argument
    `handle_text`'s docstring makes for typed text."""
    gate = StubGate(answer=StubWake(wake=False, addressed_p=0.01))
    with world(gate=gate) as w:
        w.said("run the script", addressed=True)
        w.out.wait(
            lambda fs: any(
                isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST for f in fs
            )
        )
        assert gate.seen == [], "a key press was put through the address gate"
        w.answer_card(granted=False, reason="cancelled")


def test_an_open_microphone_goes_through_the_gate_unchanged():
    """The privacy boundary. Speech that was not addressed to daa is dropped
    here and never leaves the laptop -- and never reaches the dock either."""
    gate = StubGate(answer=StubWake(wake=False, addressed_p=0.02))
    with world(gate=gate) as w:
        w.said("so anyway I told him no", addressed=False)
        w.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.AUDIT and f.params.get("kind") == "dropped"
                for f in fs
            )
        )
        assert gate.seen == ["so anyway I told him no"]
        assert w.tool.runs == []
        assert w.audits("woke") == [], "unaddressed speech reached the transcript"


def test_the_docks_vad_is_what_says_the_turn_ended():
    """`complete` is false when the segment was cut by a key release or the
    max-length timer rather than by silence, i.e. the user is still talking.
    Buffering half a sentence is always the safer read."""
    with world() as w:
        w.said("run the", addressed=True, complete=False)
        w.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.AUDIT and f.params.get("kind") == "buffered"
                for f in fs
            )
        )
        assert w.tool.runs == []

        w.said("script", addressed=True, complete=True)
        w.out.wait(
            lambda fs: any(
                isinstance(f, Request) and f.method == Method.CONFIRM_REQUEST for f in fs
            )
        )
        # And the gate saw the WHOLE sentence, not the tail of it.
        assert w.audits("woke")[0]["payload"]["text"] == "run the script"
        w.answer_card(granted=False, reason="cancelled")


def test_onset_is_accepted_and_changes_nothing_now_that_nothing_plays():
    """daa has no audio out, so there is nothing for onset to cut off.

    The frame is still part of the protocol and the dock still sends it, so
    the bridge must swallow it without noticing -- an onset that raised, or
    that dropped the turn on the floor, would be worse than one that does
    nothing at all.
    """
    with world(tier=RiskTier.ANNOUNCE) as w:
        w.stdin.push(protocol.event(Method.MIC_ONSET))
        w.stdin.push(protocol.request("d1", Method.DOCTOR))  # a fence: ordered after onset
        w.out.wait(lambda fs: any(isinstance(f, Response) and f.id == "d1" for f in fs))
        w.typed("run the script")
        w.out.wait(lambda fs: bool(w.tool.runs))


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


def test_the_dock_gets_a_transcript_rather_than_a_row_of_redactions():
    with world(tier=RiskTier.ANNOUNCE) as w:
        w.typed("run the script")
        w.out.wait(lambda fs: bool(w.tool.runs))

        assert w.wait_audit("woke")["payload"]["text"] == "run the script"
        assert w.wait_audit("execution")["payload"]["summary"] == "Ran the script."
        spoken = [
            f.params["text"] for f in w.out.frames() if getattr(f, "method", "") == Method.SPEAK
        ]
        assert "Ran the script." in spoken


def test_heard_reaches_the_dock_as_a_shape_and_never_as_content():
    with world(tier=RiskTier.ANNOUNCE) as w:
        w.typed("my card number is 4111 1111 1111 1111")
        w.out.wait(lambda fs: bool(w.tool.runs))
        heard = w.wait_audit("heard")["payload"]
        assert "text" not in heard
        assert set(heard) >= {"chars", "words", "sha256_8"}
        assert "4111" not in str(heard)


def test_a_dry_run_says_so_on_the_wire_and_carries_no_undo_id():
    with world(dry_run=True, tier=RiskTier.ANNOUNCE) as w:
        w.typed("run the script")
        w.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.AUDIT and f.params.get("kind") == "execution"
                for f in fs
            )
        )
        assert w.tool.runs == [], "a dry run touched the machine"
        execution = w.audits("execution")[0]["payload"]
        assert execution["dry_run"] is True
        assert execution["undo_id"] is None


def test_a_refusal_reaches_the_dock_with_the_reason_it_renders():
    with world(tier=RiskTier.REFUSE) as w:
        w.typed("run the script")
        refused = w.wait_audit("refused")["payload"]
        assert refused["reason"] == "because the stub said so"
        assert w.tool.runs == []


def phases_of(w: World) -> list[str]:
    return [f.params["phase"] for f in w.out.frames() if getattr(f, "method", "") == Method.STATE]


def test_the_state_machine_ends_where_it_started():
    with world(tier=RiskTier.ANNOUNCE) as w:
        w.typed("run the script")
        w.out.wait(lambda fs: bool(w.tool.runs))
        w.out.wait(
            lambda fs: [
                f.params["phase"]
                for f in fs
                if getattr(f, "method", "") == Method.STATE
            ][-1]
            == "idle"
        )
        phases = [
            f.params["phase"] for f in w.out.frames() if getattr(f, "method", "") == Method.STATE
        ]
        assert phases[0] == "idle" and phases[-1] == "idle"
        assert "thinking" in phases


def test_nothing_the_bridge_sends_is_a_phase_the_dock_renders_as_degraded():
    """An unrecognised phase renders as `degraded`, not as idle: a dock that
    looks calm for a state it does not understand is lying."""
    known = {"idle", "listening", "thinking", "awaiting", "working", "degraded"}
    with world(tier=RiskTier.ANNOUNCE) as w:
        w.typed("run the script")
        w.out.wait(lambda fs: bool(w.tool.runs))
        phases = {
            f.params["phase"] for f in w.out.frames() if getattr(f, "method", "") == Method.STATE
        }
        assert phases <= known, f"the dock would render {phases - known} as degraded"


def test_an_assessment_that_is_missing_entirely_still_produces_a_card():
    """Fail closed and SAY so: the card reports the worst case rather than
    quietly omitting the judgment."""

    def no_assessment(action: Any, spec: Any, assessment: Any, settings: Any) -> Disposition:
        return Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="no judgment available")

    with world() as w:
        w.loop.policy_decide = no_assessment
        w.typed("run the script")
        frames = w.out.wait(lambda fs: any(isinstance(f, Request) for f in fs))
        card = next(f for f in frames if isinstance(f, Request)).params
        assert card["assessment"]["synthetic"] is True
        assert card["assessment"]["blastRadius"] == 3.0
        w.answer_card(granted=False, reason="cancelled")


def test_the_assessment_the_judgment_layer_produced_is_what_the_card_shows():
    with world() as w:
        w.typed("run the script")
        frames = w.out.wait(lambda fs: any(isinstance(f, Request) for f in fs))
        card = next(f for f in frames if isinstance(f, Request)).params
        assert card["assessment"]["blastRadius"] == ASSESSMENT.blast_radius
        assert card["assessment"]["targetConfidence"] == ASSESSMENT.target_confidence
        w.answer_card(granted=False, reason="cancelled")


def test_an_audit_sink_that_throws_does_not_cost_the_dock_its_transcript():
    """"An audit sink that throws must never take the loop down with it" --
    and it must not take the dock's copy down either, which is why the tee's
    `finally` is load bearing."""

    def broken(event: AuditEvent) -> None:
        raise OSError("the disk is full")

    with world(tier=RiskTier.ANNOUNCE) as w:
        w.loop.audit = w.bridge._tee(broken)
        w.typed("run the script")
        w.out.wait(lambda fs: bool(w.tool.runs))
        assert w.wait_audit("execution")["payload"]["summary"] == "Ran the script."
