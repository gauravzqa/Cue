"""The orchestration. This is the only module allowed to know about all four
subsystems at once, and it holds that privilege by never importing any of them
at module scope -- everything arrives through the constructor, and `build_loop`
does the wiring with lazy imports so a half-built tree still imports and tests.

Control flow for one segment of speech:

    mic segment
      -> local STT                         (cheap, offline, runs on everything)
      -> AddressGate                       (ONE Jev call: wake? end of turn?)
           not woken -> DROPPED HERE. Nothing was uploaded, nothing was stored.
           not ended -> buffer and wait for the next segment
      -> cloud STT rescore                 (quality, only now that we're awake)
      -> ToolRouter                        (which few tools are even plausible)
      -> DeepSeek                          (thinking disabled; activated tools only)
      -> tool.resolve()                    (concrete targets, still no mutation)
      -> RiskGate.assess()                 (judgment about the RESOLVED action)
      -> policy.decide()                   (the only thing that may authorize)
      -> SILENT: run | ANNOUNCE: run+speak | CONFIRM_VOICE: read back, ask
                                           | CONFIRM_VISUAL: print it, type yes
      -> run -> UndoJournal.record -> TTS

The single most important invariant in this file: `_execute` is the ONLY place
that calls the tool, and it will not do so without a Disposition object whose
tier permits it. CONFIRM_VOICE additionally requires `confirmed=True`, which
only `_confirm` can produce; CONFIRM_VISUAL additionally requires the private
`_VISUAL_OK` token, which only `_confirm_visual` can produce, and which a
spoken "yes" can never manufacture. There is no other path to a mutation.

Three inputs to this file are UNTRUSTED and are treated identically:

    the model's tool calls      -- may name tools that do not exist
    the user's raw speech       -- may contain anything, and is never logged
                                   before the address gate has said it was
                                   meant for us
    ~/.daa/undo.jsonl           -- a world-readable file any local process can
                                   append to, so a row claiming to be an undo
                                   is a claim, not a fact. See `_undo_refusal`.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from daa.contracts import (
    AuditEvent,
    Disposition,
    ResolvedAction,
    RiskTier,
    ToolResult,
    ToolSpec,
)
from daa.voice.llm import LLM, LLMTurn, ToolCall
from daa.voice.mic import AudioChunk, AudioSource
from daa.voice.stt import STTUnavailable, Transcriber, Transcription
from daa.voice.transcript import Transcript
from daa.voice.tts import Speaker

# Spoken when a safety component is missing. Fail closed and SAY so -- silent
# degradation of the safety layer is how an assistant deletes something.
_NO_POLICY = "I can't act right now, my safety policy isn't loaded."
_NO_CONFIRM = "I can't confirm that right now, so I'm not doing it."
_NO_TOOL = "I don't have a tool for that."
# The model named a real tool that the router did not activate for this
# utterance. See `_handle_call` for why that escalates rather than refuses.
_ROUTER_MISS = "That isn't one of the things I lined up for what you said, so I'm checking."
_NO_SCREEN = (
    "That one has to be approved on screen and there's no screen here, "
    "so I'm leaving it."
)
_NEEDS_SCREEN = (
    "That one has to be approved on screen. I've printed the details in the "
    "terminal — type yes there if you want it."
)
# Said out loud when the undo journal hands us a row the registry will not
# vouch for. Deliberately not detailed: the detail goes to the audit log, and
# the user gets a clear refusal plus a way forward.
_UNDO_UNVERIFIED = (
    "That undo record doesn't check out, so I'm not running it. "
    "You'll want to reverse that one yourself."
)
_UNDO_REASON = "That came from the undo journal on disk rather than from you, so I'm checking."

# The ONLY thing that authorizes a CONFIRM_VISUAL execution. A spoken "yes"
# cannot produce it, and neither can a forged journal row; only the typed
# approval in `_confirm_visual` returns it.
_VISUAL_OK = object()

# Args whose value is a program rather than a reference to one. The visual
# confirmation prints every arg in full, but these are called out first because
# they are the reason the tier exists.
_SCRIPT_KEYS = ("script", "applescript", "code", "command", "argv", "source")


@dataclass(slots=True)
class TurnOutcome:
    """Everything that happened for one utterance. Returned so tests and the
    CLI can assert on the decision path rather than on side effects."""

    utterance: str = ""
    woke: bool = False
    addressed_p: float = 0.0
    end_of_turn: bool = True
    rescored: bool = False
    activated: tuple[str, ...] = ()
    reply: str = ""
    dispositions: list[Disposition] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    dropped_reason: str = ""
    # Tool calls we took responsibility for, whether or not they ran. Once this
    # is non-zero the model's own sentence must not be spoken: it was written
    # before the safety layer had an opinion, so "Sure." after "Okay, leaving
    # it." is the assistant agreeing to what it has just declined.
    handled_calls: int = 0

    @property
    def ran_anything(self) -> bool:
        return bool(self.results)


class TerminalConsole:
    """stdin/stdout, used only for the typed CONFIRM_VISUAL approval.

    Separate from Speaker because the whole point of the tier is that the
    approval does NOT travel over the audio channel. Injected so tests can
    drive both the approve and the no-terminal path without a pty.
    """

    name = "terminal"

    def available(self) -> bool:
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:  # noqa: BLE001 -- a detached stdio is "no terminal"
            return False

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def ask(self, prompt: str) -> str:
        self.write(prompt)
        return sys.stdin.readline()


class VoiceLoop:
    """Everything is injected. There are no defaults that reach the network,
    the microphone, or the filesystem, which is what makes the whole pipeline
    testable with zero hardware and zero keys."""

    def __init__(
        self,
        *,
        settings: Any,
        mic: AudioSource | None = None,
        local_stt: Transcriber | None = None,
        cloud_stt: Transcriber | None = None,
        speaker: Speaker | None = None,
        gate: Any = None,
        router: Any = None,
        risk: Any = None,
        confirm: Any = None,
        registry: Any = None,
        policy_decide: Any = None,
        journal: Any = None,
        audit: Any = None,
        llm: LLM | None = None,
        transcript: Transcript | None = None,
        console: Any = None,
    ) -> None:
        self.settings = settings
        self.mic = mic
        self.local_stt = local_stt
        self.cloud_stt = cloud_stt
        self.speaker = speaker
        self.gate = gate
        self.router = router
        self.risk = risk
        self.confirm = confirm
        self.registry = registry
        self.policy_decide = policy_decide
        self.journal = journal
        self.audit = audit
        self.llm = llm
        self.transcript = transcript if transcript is not None else Transcript()
        self.console = console if console is not None else TerminalConsole()

        # Text carried over from a segment the gate judged "not finished yet".
        self._pending = ""
        self._segments: Iterator[AudioChunk] | None = None
        # Replies for confirmation when there is no mic (the `daa say` path).
        self._scripted_replies: list[str] = []
        self.barge_ins = 0
        self.outcomes: list[TurnOutcome] = []

    # -- plumbing ----------------------------------------------------------

    def _send(self, event: AuditEvent) -> None:
        if self.audit is None:
            return
        try:
            self.audit(event)
        except Exception:  # noqa: BLE001, S110 -- an audit sink that throws
            # must never take the loop down with it, and there is nowhere left
            # to report the failure to.
            pass

    def _emit(self, kind: str, **payload: Any) -> None:
        self._send(AuditEvent(kind=kind, payload=payload))

    def _emit_built(self, builder: str, *args: Any, **kwargs: Any) -> None:
        """Log through safety/audit's event builders rather than hand-rolling.

        The builders are what hoist `synthetic` and what decide which fields a
        record carries; a payload assembled here by hand is a payload nobody
        redacted and a `synthetic` flag nobody set. Imported lazily and
        tolerated absent, like every other sibling in this file.
        """
        if self.audit is None:
            return
        module = _audit_module()
        build = getattr(module, builder, None) if module is not None else None
        if build is None:
            return
        try:
            event = build(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            # A builder that chokes on an odd assessment must not cost us the
            # action; log the failure and carry on.
            self._emit("error", where="audit_builder", builder=builder, error=str(exc))
            return
        self._send(event)

    def _speak(self, text: str, outcome: TurnOutcome | None = None) -> None:
        if not text:
            return
        if outcome is not None:
            outcome.spoken.append(text)
        self.transcript.add_assistant(text)
        self._emit("spoke", text=text)
        if self.speaker is not None:
            try:
                self.speaker.say(text)
            except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
                # The tool may already have run. Losing the sentence is bad;
                # raising after a mutation is worse.
                self._emit("error", where="speak", error=str(exc))

    def _on_speech_start(self) -> None:
        """Barge-in. Wired to the mic's VAD onset, fires on any thread."""
        if self.speaker is not None and self.speaker.is_speaking():
            self.barge_ins += 1
            self.speaker.stop()
            self._emit("barge_in", at=time.time())

    def _context(self, **extra: Any) -> Mapping[str, Any]:
        return self.transcript.as_state(
            always_on=getattr(self.settings, "always_on", False),
            dry_run=getattr(self.settings, "dry_run", True),
            speaking=bool(self.speaker is not None and self.speaker.is_speaking()),
            pending_text=self._pending or None,
            **extra,
        )

    # -- the run loop ------------------------------------------------------

    def run(self, *, max_segments: int | None = None) -> list[TurnOutcome]:
        """Consume the mic until it is exhausted (FakeMic) or closed."""
        if self.mic is None:
            raise RuntimeError("VoiceLoop.run needs a mic; use handle_text for the say path")
        self.mic.set_speech_listener(self._on_speech_start)
        self._segments = self.mic.segments()
        for seen, chunk in enumerate(self._segments, start=1):
            self.handle_chunk(chunk)
            if max_segments is not None and seen >= max_segments:
                break
        return self.outcomes

    def handle_chunk(self, chunk: AudioChunk) -> TurnOutcome:
        """mic segment -> local STT -> gate -> (maybe) the rest of the world."""
        heard = self._transcribe(self.local_stt, chunk)
        outcome = TurnOutcome(utterance=heard.text)
        if heard.empty:
            outcome.dropped_reason = "silence"
            self.outcomes.append(outcome)
            return outcome

        # Join with anything the gate previously judged unfinished, so the gate
        # sees the whole sentence rather than the tail of it.
        utterance = f"{self._pending} {heard.text}".strip() if self._pending else heard.text
        outcome.utterance = utterance
        # SHAPE ONLY, and that is the whole point. In always-on mode this line
        # runs for every sentence spoken in the room, including ones meant for
        # someone else, and the module docstring promises those are dropped
        # rather than stored. The text is emitted after the gate says yes.
        self._emit("heard", source=heard.source, partial=heard.partial, **_shape(utterance))

        decision = self._gate(utterance)
        outcome.addressed_p = float(getattr(decision, "addressed_p", 1.0))
        outcome.woke = bool(getattr(decision, "wake", True))
        outcome.end_of_turn = bool(getattr(decision, "end_of_turn", True))

        if not outcome.woke:
            # The privacy boundary. Everything above this line stayed local;
            # we now forget it entirely rather than logging the text.
            self._pending = ""
            outcome.dropped_reason = "not addressed"
            self._emit("dropped", addressed_p=outcome.addressed_p, chars=len(utterance))
            self.outcomes.append(outcome)
            return outcome

        if not outcome.end_of_turn:
            self._pending = utterance
            outcome.dropped_reason = "mid-utterance"
            self._emit("buffered", chars=len(utterance))
            self.outcomes.append(outcome)
            return outcome

        carried, self._pending = self._pending, ""
        # Only now is it worth the network: the user addressed us, so pay for
        # accuracy on the sentence we are about to act on. Only THIS chunk is
        # rescored -- the carried-over prefix was already judged good enough to
        # wake us, and re-uploading it would mean buffering audio we promised
        # not to keep.
        best = utterance
        if self.cloud_stt is not None:
            rescored = self._transcribe(self.cloud_stt, chunk)
            if not rescored.empty and rescored.text != heard.text:
                best = f"{carried} {rescored.text}".strip() if carried else rescored.text
                outcome.rescored = True
                # Both halves of a rescore are raw speech. What is useful in a
                # log is THAT the transcript changed, not what it changed to.
                self._emit(
                    "rescored",
                    source=rescored.source,
                    before_chars=len(heard.text),
                    after_chars=len(rescored.text),
                )
        outcome.utterance = best

        self._act(
            best,
            outcome,
            needs_planner=bool(getattr(decision, "needs_planner", False)),
            synthetic=bool(getattr(decision, "synthetic", False)),
        )
        self.outcomes.append(outcome)
        return outcome

    def handle_text(self, text: str, *, gated: bool = False, replies: Sequence[str] = ()) -> TurnOutcome:
        """The no-mic path, used by `daa say`.

        The gate is OFF by default here and that is a considered choice: typing
        into the CLI is an unambiguous act of address, and making the fastest
        dev loop depend on a Jev round trip would make people stop using it.
        `--gate` turns it back on for exercising that path deliberately.
        """
        outcome = TurnOutcome(utterance=text, woke=True)
        self._scripted_replies = list(replies)
        self._emit("heard", source="typed", **_shape(text))
        if gated:
            decision = self._gate(text)
            outcome.woke = bool(getattr(decision, "wake", True))
            outcome.addressed_p = float(getattr(decision, "addressed_p", 1.0))
            if not outcome.woke:
                outcome.dropped_reason = "not addressed"
                self._emit("dropped", addressed_p=outcome.addressed_p)
                self.outcomes.append(outcome)
                return outcome
        self._act(text, outcome, needs_planner=False)
        self.outcomes.append(outcome)
        return outcome

    # -- stages ------------------------------------------------------------

    def _transcribe(self, transcriber: Transcriber | None, chunk: AudioChunk) -> Transcription:
        if transcriber is None:
            return Transcription(text="", source="none")
        try:
            return transcriber.transcribe(chunk)
        except STTUnavailable as exc:
            # Expected failure mode (no model, no key, no network): degrade.
            self._emit("error", where="stt", error=str(exc))
            return Transcription(text="", source="none")
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="stt", error=str(exc))
            return Transcription(text="", source="none")

    def _gate(self, utterance: str) -> Any:
        """ONE Jev call per segment. No gate wired == always awake, which is
        only ever the case in a test or a degraded tree."""
        if self.gate is None:
            return _AlwaysWake()
        try:
            return self.gate.should_wake(utterance, self._context())
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="gate", error=str(exc))
            # Fail CLOSED on the gate: a broken judgment layer must not turn
            # the machine into a hot mic that acts on ambient conversation.
            return _NeverWake()

    def _activate(self, utterance: str) -> list[ToolSpec]:
        specs: list[ToolSpec] = list(self.registry.specs()) if self.registry is not None else []
        if self.router is None or not specs:
            return specs
        try:
            active = list(self.router.activate(utterance, specs, self._context()))
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="router", error=str(exc))
            # Router failure costs prefix-cache efficiency, not correctness --
            # every tool still has to clear the risk gate and policy. Note that
            # this falls back OPEN, which is what makes it safe for
            # `_handle_call` to treat a non-activated tool as unreachable: a
            # Jev outage can never narrow the menu, only widen it.
            return specs
        return active

    def _act(
        self,
        utterance: str,
        outcome: TurnOutcome,
        *,
        needs_planner: bool,
        synthetic: bool = False,
    ) -> None:
        self.transcript.add_user(utterance)
        specs = self._activate(utterance)
        outcome.activated = tuple(s.name for s in specs)
        # Past the gate, so the text is ours to keep. `text` is a redacted key
        # in safety/audit.py, which is the second line of defence, not the first.
        self._emit(
            "woke",
            text=utterance,
            activated=outcome.activated,
            planner=needs_planner,
            synthetic=synthetic,
        )

        turn = self._ask_llm(specs)
        outcome.reply = turn.text

        # The model may only call what the router put on the menu.
        allowed = frozenset(outcome.activated)
        for call in turn.tool_calls:
            self._handle_call(call, utterance, outcome, allowed=allowed)

        # Speak the model's words only when we did not take a tool call off its
        # hands. The model wrote that sentence before policy had an opinion, so
        # speaking it after a refusal makes us agree to what we just declined.
        if turn.text and not outcome.handled_calls:
            self._speak(turn.text, outcome)

    def _ask_llm(self, specs: Sequence[ToolSpec]) -> LLMTurn:
        if self.llm is None:
            return LLMTurn()
        try:
            return self.llm.respond(self.transcript.messages(system=_system_prompt()), specs)
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="llm", error=str(exc))
            return LLMTurn(text="Sorry, I couldn't reach the model.")

    def _lookup(self, name: str, outcome: TurnOutcome) -> Any:
        """Find a tool by name, treating "there is no such tool" as ROUTINE.

        A model naming a tool that does not exist is an ordinary Tuesday, not
        an exceptional condition: it happens on a mishearing, on a prompt that
        drifts, on any registry change. ToolRegistry.get RAISES, so the loop
        must catch it here or one hallucinated name ends the whole session.
        """
        if self.registry is None:
            self._emit("error", where="registry", tool=name, error="no registry wired")
            self._speak(_NO_TOOL, outcome)
            return None
        try:
            tool = self.registry.get(name)
        except Exception as exc:  # noqa: BLE001 -- KeyError today, anything tomorrow
            self._emit("error", where="registry", tool=name, error=str(exc))
            self._speak(_NO_TOOL, outcome)
            return None
        if tool is None or not isinstance(getattr(tool, "spec", None), ToolSpec):
            self._emit("error", where="registry", tool=name, error="unknown tool")
            self._speak(_NO_TOOL, outcome)
            return None
        return tool

    def _handle_call(
        self,
        call: ToolCall,
        utterance: str,
        outcome: TurnOutcome,
        *,
        allowed: frozenset[str] | None = None,
        min_tier: RiskTier | None = None,
        raise_reason: str = "",
    ) -> bool:
        """One tool call, from the model or from the undo journal.

        Returns True only if the tool ACTUALLY RAN and reported success -- not
        for a dry run, not for a refusal. The undo path uses that answer to
        decide whether the journal entry has been spent.
        """
        outcome.handled_calls += 1
        tool = self._lookup(call.name, outcome)
        if tool is None:
            return False

        if allowed is not None and call.name not in allowed:
            # The router narrowed the menu and the model ordered off it.
            #
            # NOT a refusal, deliberately. ToolRouter.activate returns an EMPTY
            # list when the Jev provider is unavailable -- it fails closed, by
            # design, so that a degraded turn cannot pick a destructive tool at
            # random. Refusing every un-activated call would therefore turn a
            # Jev outage into a completely inert assistant, and would let a
            # router miss bypass the risk gate and policy, which are the actual
            # authority here.
            #
            # So the miss is logged and the chain runs in full -- with the tier
            # RAISED to at least a spoken confirmation, because a tool nobody
            # routed for this utterance is exactly the kind of thing the user
            # should get a beat to say no to. Raising is always allowed.
            self._emit("router_miss", tool=call.name, activated=sorted(allowed))
            if min_tier is None or min_tier < RiskTier.CONFIRM_VOICE:
                min_tier = RiskTier.CONFIRM_VOICE
                raise_reason = raise_reason or _ROUTER_MISS

        # resolve() is contractually side-effect free, so it is safe to run
        # before any judgment -- and it has to, because the risk gate and the
        # spoken readback must both see the RESOLVED targets.
        try:
            action = tool.resolve(**dict(call.args))
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="resolve", tool=call.name, error=str(exc))
            self._speak("I couldn't work out what you meant by that.", outcome)
            return False

        assessment = self._assess(action, utterance)
        # The assessment is the evidence the disposition rests on. Logging the
        # verdict without it leaves an audit log that cannot be argued with.
        self._emit_built("judgment_event", action, assessment)

        disposition = self._decide(action, tool.spec, assessment)
        if min_tier is not None and disposition.tier < min_tier:
            # RAISING ONLY, always. `min_tier` exists for instructions that did
            # not come out of the user's mouth.
            reason = f"{raise_reason} {disposition.reason}".strip()
            disposition = replace(disposition, tier=min_tier, reason=reason)
            self._emit(
                "tier_raised",
                tool=call.name,
                to=min_tier.name,
                why=raise_reason or "untrusted source",
            )
        outcome.dispositions.append(disposition)
        self._emit_built("disposition_event", action, disposition)
        return self._dispatch(tool, action, disposition, outcome)

    def _assess(self, action: ResolvedAction, utterance: str) -> Any:
        if self.risk is None:
            return None
        try:
            return self.risk.assess(action, utterance, self.transcript.recent())
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="risk", error=str(exc))
            return None

    def _decide(self, action: ResolvedAction, spec: ToolSpec, assessment: Any) -> Disposition:
        """Policy is the ONLY authority. No policy module, no action."""
        if self.policy_decide is None:
            return Disposition(tier=RiskTier.REFUSE, reason="safety policy unavailable")
        try:
            return self.policy_decide(action, spec, assessment, self.settings)
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="policy", error=str(exc))
            # Fail closed, loudly.
            return Disposition(tier=RiskTier.REFUSE, reason=f"policy error: {exc}")

    def _dispatch(
        self,
        tool: Any,
        action: ResolvedAction,
        disposition: Disposition,
        outcome: TurnOutcome,
    ) -> bool:
        tier = disposition.tier

        if tier is RiskTier.REFUSE:
            self._speak(
                _NO_POLICY if disposition.reason == "safety policy unavailable"
                else f"I won't do that. {disposition.reason}",
                outcome,
            )
            self._emit("refused", tool=tool.spec.name, reason=disposition.reason)
            return False

        if tier is RiskTier.CONFIRM_VISUAL:
            # Voice alone can never authorize this tier -- see RiskTier's
            # docstring. The approval has to arrive over a different channel
            # than the request did, so it is typed, on screen, in full.
            if not self._confirm_visual(tool, action, disposition, outcome):
                return False
            return self._execute(
                tool, action, disposition, outcome, confirmed=True, visual=_VISUAL_OK
            )

        if tier is RiskTier.CONFIRM_VOICE:
            if self.confirm is None:
                self._speak(_NO_CONFIRM, outcome)
                self._emit("refused", tool=tool.spec.name, reason="no confirm parser")
                return False
            if not self._confirm(action, outcome):
                self._emit("abandoned", tool=tool.spec.name, target_count=len(action.targets))
                return False
            return self._execute(tool, action, disposition, outcome, confirmed=True)

        return self._execute(tool, action, disposition, outcome, confirmed=False)

    def _confirm(self, action: ResolvedAction, outcome: TurnOutcome) -> bool:
        """Read the resolved action back and wait for a yes.

        The readback is VERB-FIRST (contracts.ResolvedAction.describe), because
        a bare noun phrase -- "report.pdf, should I go ahead?" -- reads the same
        for revealing a file and for shredding it, and the user says yes to the
        wrong one exactly once.

        Asks once. If the answer is unclear, re-asks ONCE and then abandons --
        never loops. A user who has been asked the same question twice has
        already told us the interaction is broken, and a third prompt is how a
        voice assistant becomes something people unplug.
        """
        self._speak(f"Should I {_phrase(action)}?", outcome)
        for attempt in (0, 1):
            reply = self._next_reply()
            if reply is None:
                self._speak("I didn't hear an answer, so I'll leave it.", outcome)
                self._confirmation_logged(action, granted=False, via="voice")
                return False
            try:
                verdict = self.confirm.interpret(reply, action)
            except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
                self._emit("error", where="confirm", error=str(exc))
                # Fail closed: an unreadable yes is a no.
                verdict = "unclear"
            # The reply is RAW SPEECH. It is usually "yeah"; it is sometimes the
            # rest of a sentence the user was already saying. Shape only.
            self._emit("confirm", verdict=str(verdict), attempt=attempt, **_shape(reply))
            if verdict == "yes":
                self._confirmation_logged(action, granted=True, via="voice")
                return True
            if verdict == "no":
                self._speak("Okay, leaving it.", outcome)
                self._confirmation_logged(action, granted=False, via="voice")
                return False
            if attempt == 0:
                self._speak("Sorry — yes or no?", outcome)
        self._speak("I'll leave it for now.", outcome)
        self._confirmation_logged(action, granted=False, via="voice")
        return False

    def _confirm_visual(
        self,
        tool: Any,
        action: ResolvedAction,
        disposition: Disposition,
        outcome: TurnOutcome,
    ) -> bool:
        """The typed confirmation. Deliberately NOT voice.

        CONFIRM_VISUAL means "a spoken yes is not good enough", so the only
        honest implementation on a terminal app is to print the whole action --
        every argument, every target, the script if there is one -- and require
        the word `yes` to be typed. Where there is no terminal we refuse and
        say why, rather than quietly downgrading to the channel the tier exists
        to exclude.
        """
        console = self.console
        if console is None or not _console_available(console):
            self._speak(_NO_SCREEN, outcome)
            self._emit(
                "deferred_visual",
                tool=tool.spec.name,
                target_count=len(action.targets),
                reason="no terminal",
            )
            self._confirmation_logged(action, granted=False, via="visual")
            return False

        self._speak(_NEEDS_SCREEN, outcome)
        typed = ""
        try:
            console.write(_visual_detail(action, disposition))
            typed = console.ask("type yes to approve, anything else to cancel: ")
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="console", error=str(exc))
            typed = ""
        # Exactly "yes". Not "y", not "sure": the friction is the feature.
        granted = str(typed).strip().lower() == "yes"
        self._emit(
            "visual_confirm",
            tool=tool.spec.name,
            granted=granted,
            target_count=len(action.targets),
        )
        self._confirmation_logged(action, granted=granted, via="visual")
        if not granted:
            self._speak("Okay, leaving it.", outcome)
            self._emit("abandoned", tool=tool.spec.name, target_count=len(action.targets))
        return granted

    def _confirmation_logged(self, action: ResolvedAction, *, granted: bool, via: str) -> None:
        tier = RiskTier.CONFIRM_VISUAL if via == "visual" else RiskTier.CONFIRM_VOICE
        self._emit_built("confirmation_event", action, tier=tier, granted=granted, via=via)

    # -- undo --------------------------------------------------------------

    def undo_last(self, *, replies: Sequence[str] = ()) -> TurnOutcome:
        """Reverse the most recent recorded mutation.

        The journal is a file on disk that any local process can append to, so
        every row is a CLAIM. Three things follow, and all three are load
        bearing:

        1. The claim is checked against the registry before it is executed: the
           tool that made the mutation has to list this tool as one of its
           inverses. A row naming `set_clipboard` as the inverse of anything is
           a forged row, and `daa undo` would otherwise be a way to run a
           mutating tool with no confirmation at all.
        2. The tier is RAISED to at least CONFIRM_VOICE, whatever policy says,
           because the instruction arrived from a file rather than from the
           user. Raising is always allowed; lowering never is.
        3. The entry is PEEKED, not popped, and is only committed once the undo
           has actually run. Consuming first means the shipped dry_run=True
           default silently discards the real undo record while undoing
           nothing -- and an abandoned or refused undo loses it just as
           permanently.
        """
        outcome = TurnOutcome(utterance="undo", woke=True)
        self._scripted_replies = list(replies)
        if self.journal is None:
            self._speak("I don't have an undo journal.", outcome)
            self.outcomes.append(outcome)
            return outcome

        try:
            entry = self.journal.peek()
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="journal", error=str(exc))
            self._speak("I couldn't read the undo journal.", outcome)
            self.outcomes.append(outcome)
            return outcome

        if entry is None:
            self._speak("There's nothing to undo.", outcome)
            self.outcomes.append(outcome)
            return outcome

        entry_id = str(getattr(entry, "id", "") or "")
        tool_name = str(getattr(entry, "tool", "") or "")
        produced_by = getattr(entry, "produced_by", None)
        stale = getattr(entry, "stale", None)

        refusal = self._undo_refusal(entry)
        if refusal is not None:
            self._speak(_UNDO_UNVERIFIED, outcome)
            self._emit("undo_rejected", tool=tool_name, reason=refusal, entry_id=entry_id)
            self._emit_built(
                "undo_event",
                tool=tool_name,
                ok=False,
                stale=stale,
                entry_id=entry_id,
                produced_by=produced_by if isinstance(produced_by, str) else None,
                trusted=False,
                committed=False,
                error=refusal,
            )
            self.outcomes.append(outcome)
            return outcome

        if stale:
            # Say it BEFORE the confirmation question, so the yes is a yes to
            # the world as it is now rather than as it was when we recorded.
            self._speak(str(stale), outcome)

        ran = self._handle_call(
            ToolCall(name=tool_name, args=dict(getattr(entry, "args", {}) or {})),
            str(getattr(entry, "description", "") or "undo"),
            outcome,
            min_tier=RiskTier.CONFIRM_VOICE,
            raise_reason=_UNDO_REASON,
        )
        committed = False
        if ran:
            committed = self._commit_undo(entry)
        else:
            # Dry run, refusal, abandonment or failure: the record survives, so
            # "undo that" still means something after the user turns dry_run off.
            self._emit("undo_retained", tool=tool_name, entry_id=entry_id)
        self._emit_built(
            "undo_event",
            tool=tool_name,
            ok=ran,
            stale=stale,
            entry_id=entry_id,
            produced_by=produced_by if isinstance(produced_by, str) else None,
            trusted=True,
            committed=committed,
            dry_run=bool(getattr(self.settings, "dry_run", False)),
        )

        self.outcomes.append(outcome)
        return outcome

    def _undo_refusal(self, entry: Any) -> str | None:
        """None if this row may be executed, else a short machine-readable why.

        Treated exactly like an LLM tool call, because it has exactly the same
        trust level: something outside this process chose the tool name.
        """
        tool_name = getattr(entry, "tool", None)
        if not isinstance(tool_name, str) or not tool_name:
            return "no tool name"
        if getattr(entry, "trusted", True) is False:
            # The journal's own verdict: the row is not even well formed enough
            # to check. It is not permission when it is True, but it is a
            # refusal when it is False.
            return "journal says untrusted"
        produced_by = getattr(entry, "produced_by", None)
        if not isinstance(produced_by, str) or not produced_by:
            # A row that will not say what made it cannot be checked, and an
            # uncheckable claim from a world-readable file is a hostile one.
            return "no produced_by"
        if self.registry is None:
            return "no registry to verify against"
        try:
            origin = self.registry.get(produced_by)
        except Exception:  # noqa: BLE001 -- unknown producer is a refusal, not a crash
            return "unknown produced_by"
        spec = getattr(origin, "spec", None) if origin is not None else None
        if not isinstance(spec, ToolSpec):
            return "unknown produced_by"
        if tool_name not in tuple(spec.inverses):
            # THE check. `set_clipboard` is nobody's inverse.
            return "not an inverse of produced_by"
        return None

    def _record_undo(self, tool: Any, result: ToolResult) -> str | None:
        """Write the inverse down, attributed to the tool that just mutated.

        `produced_by` is what makes the row checkable at undo time; a row
        without it is refused by `_undo_refusal`, so recording one would be
        recording an undo that can never run.
        """
        if result.undo is None or self.journal is None:
            return None
        name = getattr(tool.spec, "name", "")
        try:
            try:
                entry = self.journal.record(result.undo, produced_by=name)
            except TypeError:
                # A journal from before attribution existed. Still recorded, so
                # `daa undo --list` shows it and the user can act on it.
                entry = self.journal.record(result.undo)
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="journal", error=str(exc))
            return None
        return str(getattr(entry, "id", "") or "") or None

    def _commit_undo(self, entry: Any) -> bool:
        """Spend the journal entry, and only ever after the undo really ran."""
        commit = getattr(self.journal, "commit", None)
        try:
            if callable(commit):
                return bool(commit(entry))
            # Journal without the split API: pop() consumes the newest entry,
            # which is the one peek() handed us. Reached only on success, which
            # is the property that matters.
            self.journal.pop()
            return True
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="journal", error=str(exc))
            return False

    def _next_reply(self) -> str | None:
        """Pull the next utterance, for a confirmation answer.

        Deliberately LOCAL STT only: this is a yes/no at the most
        latency-sensitive moment in the interaction, and the cloud round trip
        buys nothing on a one-word answer. Ambiguity is ConfirmParser's job.
        """
        if self._scripted_replies:
            return self._scripted_replies.pop(0)
        if self._segments is None:
            return None
        chunk = next(self._segments, None)
        if chunk is None:
            return None
        heard = self._transcribe(self.local_stt, chunk)
        return None if heard.empty else heard.text

    def _execute(
        self,
        tool: Any,
        action: ResolvedAction,
        disposition: Disposition,
        outcome: TurnOutcome,
        *,
        confirmed: bool,
        visual: object | None = None,
    ) -> bool:
        """THE ONLY CALLER OF THE TOOL IN THIS CODEBASE.

        The asserts are not defensive noise -- they are the enforcement point
        for the one rule that makes the rest of the design mean anything. If a
        future refactor routes around policy, the process dies here rather than
        quietly deleting something.

        Returns True only when the tool really ran and really succeeded; a dry
        run returns False, because nothing happened.
        """
        assert isinstance(disposition, Disposition), "tool.run requires a Disposition"
        assert disposition.tier is not RiskTier.REFUSE, "refused actions must never run"
        assert disposition.tier is not RiskTier.CONFIRM_VISUAL or visual is _VISUAL_OK, (
            "voice cannot authorize visual tier"
        )
        if disposition.tier is RiskTier.CONFIRM_VOICE:
            assert confirmed, "confirm-tier actions require an affirmative confirmation"

        ran_for_real = False
        if getattr(self.settings, "dry_run", False):
            # Dry run stops at the boundary and narrates instead. It is not a
            # softer tier -- the disposition was still computed and honoured.
            result = ToolResult(ok=True, summary=f"Dry run: I would {_phrase(action)}.")
            self._emit("dry_run", tool=tool.spec.name, target_count=len(action.targets))
            self._emit_built(
                "execution_event",
                action,
                ok=True,
                summary=result.summary,
                dry_run=True,
                tier=disposition.tier,
            )
        else:
            try:
                result = tool.run(action)
            except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
                self._emit("error", where="run", tool=tool.spec.name, error=str(exc))
                self._emit_built(
                    "execution_event",
                    action,
                    ok=False,
                    error=str(exc),
                    dry_run=False,
                    tier=disposition.tier,
                )
                self._speak("That didn't work.", outcome)
                return False
            ran_for_real = bool(result.ok)
            # Record BEFORE speaking: if TTS hangs, the undo must still exist.
            entry_id = self._record_undo(tool, result)
            self._emit_built(
                "execution_event",
                action,
                ok=bool(result.ok),
                summary=result.summary,
                error=result.error,
                dry_run=False,
                undo=result.undo,
                tier=disposition.tier,
                undo_id=entry_id,
            )

        outcome.results.append(result)
        if not result.ok:
            self._speak(result.summary or "That didn't work.", outcome)
            return False
        # SILENT means silent: read-only actions the user did not ask to hear
        # about are exactly the ones that make an assistant feel chatty.
        if disposition.tier is not RiskTier.SILENT:
            self._speak(result.summary, outcome)
        return ran_for_real


class _AlwaysWake:
    """Stand-in WakeDecision for a loop with no gate wired."""

    wake = True
    end_of_turn = True
    needs_planner = False
    addressed_p = 1.0
    latency_ms = 0.0
    synthetic = True


class _NeverWake(_AlwaysWake):
    wake = False
    addressed_p = 0.0


def _system_prompt() -> str:
    from daa.voice.llm import SYSTEM_PROMPT

    return SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Spoken phrasing
# ---------------------------------------------------------------------------


def _phrase(action: ResolvedAction) -> str:
    """A verb-first fragment that can follow "Should I" or "I would".

    `ResolvedAction.describe()` is verb-first when the resolver supplied a
    verb. When it did not, describe() falls back to the TOOL NAME, which is a
    noun -- "I would screenshots from today" -- so we name the tool explicitly
    instead of pretending it conjugates.
    """
    text = " ".join(str(action.describe() or "").split()).rstrip(". ")
    if str(action.verb or "").strip():
        return text or "that"
    targets = ", ".join(str(t) for t in action.targets)
    name = str(action.tool or "").replace("_", " ").strip() or "that"
    tail = f" on {targets}" if targets else ""
    extra = ""
    if action.consequences:
        extra = ", " + ", ".join(str(v) for v in action.consequences.values())
    return f"use the {name} tool{tail}{extra}"


def _visual_detail(action: ResolvedAction, disposition: Disposition) -> str:
    """The whole action, in full, on screen. Nothing elided and nothing summarised.

    This is the text the CONFIRM_VISUAL tier exists to put in front of someone:
    if a script is about to run, the script is here, not a description of it.
    """
    rule = "─" * 68
    lines = [
        "",
        rule,
        "daa needs your approval ON SCREEN for this action.",
        f"  tool       {action.tool}",
        f"  tier       {disposition.tier.name}",
        f"  reason     {disposition.reason}",
        f"  action     {_phrase(action)}",
        f"  explicit   {'you asked for this' if action.explicit else 'I inferred this'}",
    ]
    targets = [str(t) for t in action.targets]
    lines.append(f"  targets    {len(targets)}")
    lines.extend(f"               - {t}" for t in targets)
    args = dict(action.args)
    lines.append(f"  arguments  {len(args)}")
    for key in sorted(args, key=lambda k: (k not in _SCRIPT_KEYS, str(k))):
        flag = "  <-- this runs" if key in _SCRIPT_KEYS else ""
        lines.append(f"    {key} ={flag}")
        body = str(args[key])
        lines.extend(f"      {ln}" for ln in (body.splitlines() or [""]))
    for name, text in dict(action.consequences).items():
        lines.append(f"  ! {name}: {text}")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def _console_available(console: Any) -> bool:
    try:
        return bool(console.available())
    except Exception:  # noqa: BLE001 -- a console that cannot say is not available
        return False


def _shape(text: str) -> dict[str, Any]:
    """The SHAPE of an utterance, never its content.

    Redaction in the audit sink is a second line of defence, not the first: a
    debug sink, a telemetry hook or a future second sink would receive whatever
    the loop handed it. So raw speech does not leave this module at all -- what
    leaves is a length, a word count and a digest, which is enough to correlate
    records and useless to anyone reading the log.
    """
    raw = str(text or "")
    return {
        "chars": len(raw),
        "words": len(raw.split()),
        "sha256_8": hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8],
    }


_AUDIT_MOD: Any = None
_AUDIT_LOOKED = False


def _audit_module() -> Any:
    """safety/audit.py's event builders, or None in a half-built tree."""
    global _AUDIT_MOD, _AUDIT_LOOKED
    if not _AUDIT_LOOKED:
        _AUDIT_LOOKED = True
        try:
            from daa.safety import audit as module

            _AUDIT_MOD = module
        except ImportError:
            _AUDIT_MOD = None
    return _AUDIT_MOD


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_loop(settings: Any, *, mic: AudioSource | None = None, audit: Any = None) -> VoiceLoop:
    """Assemble a real loop from whatever modules and keys exist.

    Every import here is inside the function and inside try/except. Three
    sibling subsystems are being written in parallel with this one; a missing
    or half-finished module must degrade the loop, not break `import daa`.
    Anything that fails to wire is reported by `daa doctor` rather than raised.
    """
    from daa.voice import llm as llm_mod
    from daa.voice import stt as stt_mod
    from daa.voice import tts as tts_mod

    missing: list[str] = []

    provider = None
    try:
        from daa.jev.client import JevUnavailable, build_provider

        try:
            provider = build_provider(settings)
        except JevUnavailable as exc:
            missing.append(f"jev provider: {exc}")
    except ImportError as exc:
        missing.append(f"daa.jev.client: {exc}")

    gate = router = risk = confirm = None
    if provider is not None:
        try:
            from daa.jev.gate import AddressGate

            gate = AddressGate(provider, settings)
        except ImportError as exc:
            missing.append(f"daa.jev.gate: {exc}")
        try:
            from daa.jev.router import ToolRouter

            router = ToolRouter(provider)
        except ImportError as exc:
            missing.append(f"daa.jev.router: {exc}")
        try:
            from daa.jev.risk import RiskGate

            risk = RiskGate(provider, settings)
        except ImportError as exc:
            missing.append(f"daa.jev.risk: {exc}")
        try:
            from daa.jev.confirm import ConfirmParser

            confirm = ConfirmParser(provider, settings)
        except ImportError as exc:
            missing.append(f"daa.jev.confirm: {exc}")

    registry = None
    try:
        # Bind a FRESH registry to THIS settings object rather than importing the
        # module-global REGISTRY. The global's tools lazily call Settings.load()
        # themselves, so they answer to the environment and not to the caller --
        # a loop built with an explicit dry_run=True would hand work to tools
        # that had independently decided they were live. Today both happen to
        # read the same .env and agree, which is exactly what makes it a trap
        # worth closing before a --live flag or a library embedding finds it.
        from daa.tools import install
        from daa.tools.registry import ToolRegistry

        registry = install(ToolRegistry(), settings)
    except ImportError as exc:
        missing.append(f"daa.tools.registry: {exc}")

    policy_decide = None
    try:
        from daa.safety.policy import decide as policy_decide
    except ImportError as exc:
        missing.append(f"daa.safety.policy: {exc}")

    journal = None
    try:
        from daa.safety.undo import UndoJournal

        journal = UndoJournal()
    except ImportError as exc:
        missing.append(f"daa.safety.undo: {exc}")

    if audit is None:
        try:
            import pathlib

            from daa.safety.audit import JsonlAudit

            path = pathlib.Path.home() / ".daa" / "audit.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            audit = JsonlAudit(path)
        except ImportError as exc:
            missing.append(f"daa.safety.audit: {exc}")

    loop = VoiceLoop(
        settings=settings,
        mic=mic,
        local_stt=stt_mod.build_local(settings),
        cloud_stt=stt_mod.build_cloud(settings),
        speaker=tts_mod.build_speaker(settings),
        gate=gate,
        router=router,
        risk=risk,
        confirm=confirm,
        registry=registry,
        policy_decide=policy_decide,
        journal=journal,
        audit=audit,
        llm=llm_mod.build_llm(settings),
    )
    # Not an error and not a log line: `daa doctor` reads this and prints it,
    # which is the only place a half-wired tree should be visible.
    loop.missing = missing  # type: ignore[attr-defined]
    return loop
