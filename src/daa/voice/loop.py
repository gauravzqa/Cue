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

Two things were added when execution moved off this thread, and neither of them
weakens the sentence above:

    A WARRANT replaces `confirmed=True` wherever an authorization has to cross
    a thread boundary. A boolean cannot: by the time a worker acts on it the
    grant may have been revoked, the action rebuilt, or the readback two
    minutes stale. A warrant is single-use, expiring, and carries the sha256 of
    the exact action it was issued for.

    A GRANT can supply the ANSWER to a confirmation policy already demanded,
    for steps that fall inside a bounded, expiring, revocable bargain the user
    agreed to out loud. It is consulted strictly AFTER `policy.decide()` and it
    is never an input to the tier. `safety/grant.py` holds the twelve clauses;
    this file holds only the plumbing that calls them and the prompt that
    happens when they do not all hold.

Three inputs to this file are UNTRUSTED and are treated identically:

    the model's tool calls      -- may name tools that do not exist
    the user's raw speech       -- may contain anything, and is never logged
                                   before the address gate has said it was
                                   meant for us
    ~/.daa/undo.jsonl           -- a world-readable file any local process can
                                   append to, so a row claiming to be an undo
                                   is a claim, not a fact. See `_undo_refusal`.

A raised tier from either of the last two is NEVER answered by a grant: the
raise exists to give the user a beat to say no, and a grant that could answer
it would turn "you okayed tidying Downloads" into "you okayed anything that
names move_files". See `_handle_call`.
"""

from __future__ import annotations

import hashlib
import queue
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from daa.contracts import (
    AuditEvent,
    Budget,
    Disposition,
    Grant,
    GrantScope,
    JobStatus,
    ResolvedAction,
    RiskTier,
    ToolResult,
    ToolSpec,
    Warrant,
)
from daa.voice.jobs import (
    InlineChannel,
    JobFinished,
    JobOutcome,
    JobRegistry,
    JobRunner,
    NoticeBoard,
    QueueChannel,
    StepRequest,
    StepVerdict,
    start_thread,
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
# Said out loud when the undo journal hands us a row the registry will not
# vouch for. Deliberately not detailed: the detail goes to the audit log, and
# the user gets a clear refusal plus a way forward.
_UNDO_UNVERIFIED = (
    "That undo record doesn't check out, so I'm not running it. "
    "You'll want to reverse that one yourself."
)
_UNDO_REASON = "That came from the undo journal on disk rather than from you, so I'm checking."
# Said when a rollback is refused or has nothing left it can honestly reverse.
_ROLLBACK_NOTHING = "There's nothing from that stretch I can put back."
_ROLLBACK_REASON = "That's undoing a whole stretch of work, so I'm checking."

# The ONLY thing that authorizes a CONFIRM_VISUAL execution. A spoken "yes"
# cannot produce it, and neither can a forged journal row; only the typed
# approval in `_confirm_visual` returns it.
_VISUAL_OK = object()

# The same trick one tier down, for a checkpoint rollback: it stands for "the
# user already answered ONE question covering this whole window", and only
# `rollback` can produce it. It tops out at CONFIRM_VOICE by construction in
# `_dispatch`, so an aggregate spoken yes can never reach the visual tier.
_ROLLBACK_OK = object()

# "the caller did not say", distinct from an explicit None meaning "do not log".
_UNSET = object()

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
        jobs: JobRegistry | None = None,
        notices: NoticeBoard | None = None,
        grants: Any = None,
        warrants: Any = None,
        now: Callable[[], float] | None = None,
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

        # --- async execution and scoped consent ---------------------------
        # The clock is injected so budgets, grant expiries and warrant TTLs are
        # testable without sleeping. Nothing below calls time.time() directly.
        self.now: Callable[[], float] = now if now is not None else time.time
        self.jobs = jobs
        self.notices = notices if notices is not None else NoticeBoard(now=self.now)
        grant_mod = _grant_module()
        self.grants = grants if grants is not None else grant_mod.GrantBook()
        self.warrants = warrants if warrants is not None else grant_mod.WarrantBook(
            ttl_s=float(getattr(settings, "warrant_ttl_s", grant_mod.DEFAULT_WARRANT_TTL_S))
        )
        # The grant covering FOREGROUND work, if the user has given one. None
        # is the normal state and means every confirmation is asked fresh.
        self.active_grant: Grant | None = None
        # ONE queue. Step requests and job-finished notifications both arrive
        # on it, so `service_jobs` blocks until something really happened and
        # there is no polling interval to tune or sleep to flake on.
        self._job_events: queue.Queue[Any] = queue.Queue()
        self._runners: dict[str, JobRunner] = {}
        self._threads: list[Any] = []
        # Re-entrancy guard for `_next_reply`. Non-zero means a FOREGROUND
        # confirmation is in flight and its answer is spoken for; a background
        # job asking at that moment would steal it. See `_drain_allowed`.
        self._confirming = 0
        # When the room last went quiet, for the silence drain point.
        self._quiet_since: float | None = None

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
            # DRAIN POINT 2: silence. The only room state daa can be SURE it is
            # not interrupting. Held for `notice_quiet_s` first, because one
            # quiet segment is a breath, not a gap in the conversation.
            now = self.now()
            if self._quiet_since is None:
                self._quiet_since = now
            quiet_s = float(getattr(self.settings, "notice_quiet_s", 20.0))
            if now - self._quiet_since >= quiet_s:
                self.drain_notices(outcome)
                self._quiet_since = now
            self.outcomes.append(outcome)
            return outcome
        # Somebody is talking, in this room, right now. Whatever else happens,
        # the silence clock restarts.
        self._quiet_since = None

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
        # DRAIN POINT 1: piggyback. daa has just finished speaking in a turn
        # the user addressed to it, so this is free and it is where 90% of
        # notices should land. Note where it is NOT: the not-addressed return
        # above never reaches here, because speaking into someone else's
        # sentence is the exact failure the address gate exists to prevent.
        self.service_jobs()
        self.drain_notices(outcome)
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
        self.service_jobs()
        self.drain_notices(outcome)
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
            # Say WHICH failure. "Couldn't reach the model" sent me chasing a
            # network problem when the real answer was a 402 with an exact
            # message in the body; an assistant that hides the one useful
            # sentence in the exception is worse than one that says nothing.
            return LLMTurn(text=f"The model call failed: {_llm_reason(exc)}")

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
        grant: Grant | None = None,
        warrant: Warrant | None = None,
        aggregate: object | None = None,
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
        if raise_reason:
            # THE TIER WAS RAISED BECAUSE THE INSTRUCTION DID NOT COME OUT OF
            # THE USER'S MOUTH -- a row from the undo journal on disk, or a
            # tool the router never put on the menu. The confirmation that
            # raise demands exists precisely to give the user a beat to say no,
            # so a grant must not answer it: the grant was about the user's own
            # goal, not about whatever a file claimed next. This is the
            # difference between "you okayed this kind of work" and "you okayed
            # anything that names one of these tools".
            grant = None
            warrant = None
        elif grant is None:
            # Resolved HERE rather than inside `_grant_answer`, so that "no
            # grant" can actually be said. A fallback buried one level down
            # cannot be switched off by the caller, which is how the clause
            # above would have quietly done nothing.
            grant = self.active_grant
        outcome.dispositions.append(disposition)
        self._emit_built(
            "disposition_event",
            action,
            disposition,
            grant_id=grant.id if grant is not None else None,
        )
        return self._dispatch(
            tool, action, disposition, outcome, grant=grant, warrant=warrant, aggregate=aggregate
        )

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
        outcome: Any,
        *,
        grant: Grant | None = None,
        warrant: Warrant | None = None,
        aggregate: object | None = None,
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

        # A grant is consulted AFTER the tier has been computed and never
        # before, and it can only ever supply the ANSWER to a confirmation this
        # disposition already demanded. It does not change `tier`: the tier
        # that was computed is the tier that is logged and the tier the warrant
        # carries. There is no tier here that means "was skipped".
        if warrant is None and tier >= RiskTier.CONFIRM_VOICE:
            warrant = self._grant_answer(tool, action, disposition, grant)

        # ONE consent, N validated executions. A rollback asks "put back the
        # six things?" once; each inverse is still individually checked for
        # forgery, for an inverses claim the registry will vouch for, and for
        # staleness. The consent is aggregated; the VERIFICATION is not. And it
        # tops out at CONFIRM_VOICE, because a spoken aggregate yes cannot
        # answer a question that exists precisely because speech is not enough.
        if warrant is None and aggregate is _ROLLBACK_OK and tier <= RiskTier.CONFIRM_VOICE:
            warrant = self.warrants.issue(action, disposition, via="voice", now=self.now())
            self._emit_built(
                "confirmation_event",
                action,
                tier=tier,
                granted=True,
                via="voice",
                warrant_id=warrant.id,
            )

        if warrant is not None and warrant.tier >= tier:
            return self._execute(
                tool, action, disposition, outcome, confirmed=True, warrant=warrant
            )

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

    def _grant_answer(
        self,
        tool: Any,
        action: ResolvedAction,
        disposition: Disposition,
        grant: Grant | None = None,
    ) -> Warrant | None:
        """Can a live grant answer this confirmation? Fail closed at every step.

        A grant that cannot be SHOWN to cover an action does not cover it, so
        every path out of here that is not a clean twelve-clause pass returns
        None and the user gets asked.
        """
        if grant is None:
            return None
        grant_mod = _grant_module()
        state = self.grants.state(grant.id)
        try:
            ok, why = grant_mod.grant_satisfies(
                grant, state, action, tool.spec, disposition, self.now()
            )
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            # A grant check that throws is a grant that did not pass. This is
            # the one except in this file where the fallback is not "degrade"
            # but "ask the human", which is the same thing said politely.
            self._emit("error", where="grant", error=str(exc))
            return None
        self._emit(
            "grant_checked",
            tool=tool.spec.name,
            grant_id=grant.id,
            satisfied=bool(ok),
            tier=disposition.tier.name,
        )
        if not ok:
            self._emit("grant_miss", tool=tool.spec.name, grant_id=grant.id, reason=why)
            return None
        warrant = self.warrants.issue(
            action, disposition, via="grant", now=self.now(), grant=grant
        )
        if state is not None:
            state.spend_step(action)
        # Logged as a CONFIRM_* step whose confirmation arrived via="grant".
        # That row is what makes habituation countable later.
        self._emit_built(
            "confirmation_event",
            action,
            tier=disposition.tier,
            granted=True,
            via="grant",
            grant_id=grant.id,
            warrant_id=warrant.id,
        )
        return warrant

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
        self._confirming += 1
        try:
            return self._confirm_loop(action, outcome)
        finally:
            self._confirming -= 1

    def _confirm_loop(self, action: ResolvedAction, outcome: Any, *, log: Any = _UNSET) -> bool:
        """The two-attempt yes/no, shared by action confirmations, grant
        readbacks and rollback. `log` is the audit callback; None means the
        caller writes its own row (a grant is not a confirmation of an
        action, and logging it as one would put a fake tool name in the log).
        """
        for attempt in (0, 1):
            reply = self._next_reply()
            if reply is None:
                self._speak("I didn't hear an answer, so I'll leave it.", outcome)
                self._logged(log, action, granted=False)
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
                self._logged(log, action, granted=True)
                return True
            if verdict == "no":
                self._speak("Okay, leaving it.", outcome)
                self._logged(log, action, granted=False)
                return False
            if attempt == 0:
                self._speak("Sorry — yes or no?", outcome)
        self._speak("I'll leave it for now.", outcome)
        self._logged(log, action, granted=False)
        return False

    def _logged(self, log: Any, action: ResolvedAction, *, granted: bool) -> None:
        if log is None:
            return
        self._confirmation_logged(action, granted=granted, via="voice")

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

        # Deliberately NOT spoken here. This line is only reached when the
        # console IS available (the no-console case returned above with
        # _NO_SCREEN), so the card is about to appear in front of the user and
        # the header already says approval is needed on screen. Worse, `daa
        # say` prints spoken lines after the turn completes, so this
        # pre-announcement surfaced AFTER the typed approval and read as a
        # refusal of the thing that had just been approved.
        typed = ""
        self._confirming += 1
        try:
            console.write(_visual_detail(action, disposition))
            typed = console.ask("type yes to approve, anything else to cancel: ")
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="console", error=str(exc))
            typed = ""
        finally:
            self._confirming -= 1
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

    def _record_undo(
        self, tool: Any, result: ToolResult, *, checkpoint_id: str | None = None
    ) -> str | None:
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
                entry = self.journal.record(
                    result.undo, produced_by=name, checkpoint_id=checkpoint_id
                )
            except TypeError:
                # A journal from before checkpoints existed. Still recorded, so
                # per-step undo keeps working; only the window index is lost.
                try:
                    entry = self.journal.record(result.undo, produced_by=name)
                except TypeError:
                    # A journal from before attribution existed. Still
                    # recorded, so `daa undo --list` shows it and the user can
                    # act on it.
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

    # -- scoped consent ----------------------------------------------------

    def request_grant(
        self,
        *,
        goal: str,
        scope: GrantScope,
        budget: Budget,
        ceiling: RiskTier = RiskTier.CONFIRM_VOICE,
        via: str = "voice",
        briefed_consequences: Sequence[str] = (),
        uncertain: str = "",
        ttl_s: float | None = None,
        outcome: TurnOutcome | None = None,
        replies: Sequence[str] = (),
        parent_id: str | None = None,
    ) -> Grant | None:
        """Ask for a bounded go-ahead, and return it only if it was given.

        The readback is composed by `safety/grant.py::readback`, which names
        the MOST DESTRUCTIVE thing in scope rather than the average one. That
        clause is the whole mitigation for consent fatigue: a sentence that
        names the worst case stops sounding the same each time, and a user who
        has learned the shape of the sentence is a user who has stopped
        hearing it.

        A visual ceiling requires the visual channel, and there is no path here
        that lets a spoken yes produce one.
        """
        outcome = outcome if outcome is not None else TurnOutcome(utterance=goal, woke=True)
        if replies:
            self._scripted_replies = list(replies)
        grant_mod = _grant_module()
        specs = list(self.registry.specs()) if self.registry is not None else []
        if via == "visual" or ceiling >= RiskTier.CONFIRM_VISUAL:
            # A grant that could pre-authorize the visual tier must itself have
            # arrived over the visual channel. Anything else is the tier being
            # defeated by going around it.
            via = "visual"
        grant = grant_mod.issue_grant(
            goal=goal,
            plan_summary="",
            ceiling=ceiling,
            granted_via=via,
            scope=scope,
            budget=budget,
            now=self.now(),
            ttl_s=(
                float(getattr(self.settings, "grant_ttl_s", grant_mod.DEFAULT_GRANT_TTL_S))
                if ttl_s is None
                else float(ttl_s)
            ),
            parent_id=parent_id,
            briefed_consequences=briefed_consequences,
        )
        sentence = grant_mod.readback(grant, specs, uncertain=uncertain)
        # The readback is stored VERBATIM on the grant, because "who authorized
        # what" is not answerable from a scope object -- it is answerable from
        # the sentence the user actually heard before they said yes.
        grant = replace(grant, plan_summary=sentence)

        granted = (
            self._grant_typed(grant, specs, outcome)
            if via == "visual"
            else self._grant_spoken(grant, outcome)
        )
        self._emit_built(
            "grant_event", grant, granted=granted, tools=sorted(s.name for s in specs)
        )
        if not granted:
            self._speak("Okay, leaving it.", outcome)
            return None
        self.grants.add(grant)
        self.active_grant = grant
        return grant

    def _grant_spoken(self, grant: Grant, outcome: TurnOutcome) -> bool:
        if self.confirm is None:
            self._speak(_NO_CONFIRM, outcome)
            return False
        self._speak(grant.plan_summary, outcome)
        pending = ResolvedAction(
            tool="grant",
            args={},
            targets=(),
            verb=f"go ahead and {str(grant.goal or 'do that').strip().rstrip('.')}",
        )
        self._confirming += 1
        try:
            return self._confirm_loop(pending, outcome, log=None)
        finally:
            self._confirming -= 1

    def _grant_typed(self, grant: Grant, specs: Sequence[ToolSpec], outcome: TurnOutcome) -> bool:
        console = self.console
        if console is None or not _console_available(console):
            self._speak(_NO_SCREEN, outcome)
            return False
        grant_mod = _grant_module()
        typed = ""
        self._confirming += 1
        try:
            console.write(grant_mod.visual_card(grant, specs))
            typed = console.ask("type yes to approve, anything else to cancel: ")
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="console", error=str(exc))
            typed = ""
        finally:
            self._confirming -= 1
        return str(typed).strip().lower() == "yes"

    def stop(self, reason: str = "you said stop") -> str:
        """Revoke every live grant and cancel every live job. Idempotent.

        Three properties, in priority order: it works without the LLM, it works
        without the tool router, and IT NEVER CLAIMS MORE THAN IT DID.
        Cancellation is cooperative -- a tool already inside a spawned process
        finishes or hits its own timeout -- so the sentence is "I'll finish the
        step I'm on", not "stopped". An assistant that says "stopped" while a
        click is still in flight has taught you not to believe it.
        """
        now = self.now()
        killed = 0
        for grant in list(self.grants.live(now=now)):
            state = self.grants.state(grant.id)
            self.grants.revoke(grant.id, reason, now=now)
            killed = self.warrants.revoke_grant(grant.id)
            self._emit_built(
                "grant_revoked_event",
                grant_id=grant.id,
                reason=reason,
                at=now,
                steps_completed=state.steps_used if state is not None else 0,
                warrants_killed=killed,
            )
        self.active_grant = None
        running = self.jobs.revoke_all(reason) if self.jobs is not None else []
        if running:
            return "Stopping — I'll finish the step I'm on."
        return "Okay, stopped."

    # -- background jobs ---------------------------------------------------

    @property
    def _drain_allowed(self) -> bool:
        """False while a FOREGROUND confirmation is in flight.

        `_next_reply` pops from a single scripted-reply queue and, with a mic,
        from a single segment stream. A background job asking for consent in
        the middle of `_confirm` would consume the answer the user was giving
        to the question in front of them. So notices and job consents drain
        only at explicit drain points, never re-entrantly.
        """
        return self._confirming == 0

    def start_job(
        self,
        tool: Any,
        action: ResolvedAction,
        grant: Grant,
        *,
        threaded: bool = True,
        max_steps: int | None = None,
    ) -> str | None:
        """Hand a long-running tool's generator to a runner.

        The generator is the agent. It yields the action it WANTS and receives
        the result; it has no handle on the registry, the journal or
        `_execute`, so there is no API surface through which it could route
        around the gate. That is the invariant enforced by shape rather than by
        discipline.
        """
        if self.jobs is None:
            self._emit("error", where="jobs", error="no job registry wired")
            return None
        steps = getattr(tool, "steps", None)
        if not callable(steps):
            self._emit("error", where="jobs", error=f"{tool.spec.name} is not long-running")
            return None
        try:
            stream = steps(action)
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="jobs", error=str(exc))
            return None
        try:
            record = self.jobs.create(goal=grant.goal, grant_id=grant.id)
        except RuntimeError:
            # max_jobs. Two agents driving the same GUI is not a concurrency
            # problem, it is a correctness problem, so a second request is a
            # question rather than a queue entry.
            self._emit("job_refused", reason="one at a time")
            return None

        channel: Any = (
            QueueChannel(self._job_events) if threaded else InlineChannel(self._authorize_step)
        )

        def perform(act: ResolvedAction, verdict: StepVerdict, index: int) -> ToolResult:
            return self._perform_step(record.id, act, verdict, index)

        runner = JobRunner(
            job_id=record.id,
            stream=stream,
            ask=channel.ask,
            perform=perform,
            registry=self.jobs,
            cancel=self.jobs.cancel_event(record.id),
            notices=self.notices,
            finished=channel.finish,
            max_steps=int(grant.budget.steps) if max_steps is None else int(max_steps),
        )
        self._runners[record.id] = runner
        self._emit_built(
            "job_event",
            job_id=record.id,
            status=JobStatus.RUNNING,
            goal=grant.goal,
            grant_id=grant.id,
        )
        if threaded:
            self._threads.append(start_thread(runner))
        else:
            # Inline: the generator, the authorization and the execution all
            # happen on this thread, in the same order the threaded path
            # produces. The seam removes the handoff and nothing else.
            runner.drive()
            for event in getattr(channel, "finished", []):
                self._emit_built(
                    "job_event",
                    job_id=event.job_id,
                    status=event.status,
                    summary=event.summary,
                    error=event.error,
                )
        return record.id

    def _authorize_step(self, request: StepRequest) -> StepVerdict:
        """LOOP THREAD ONLY. Everything that decides anything happens here.

        This is the half of the split that does not move: the worker proposes
        and blocks, and the authority stays on the one thread that owns the
        mic, the console and the scripted replies. Serializing it costs
        nothing, because it is pure computation plus, rarely, a prompt.
        """
        action = request.action
        job = self.jobs.get(request.job_id) if self.jobs is not None else None
        grant, state = self.grants.get(job.grant_id if job is not None else None)
        outcome = TurnOutcome(utterance=job.goal if job is not None else "", woke=True)

        def refuse(reason: str, status: JobStatus = JobStatus.CANCELLED) -> StepVerdict:
            self._emit_built(
                "job_step_event",
                job_id=request.job_id,
                step_index=request.step_index,
                tool=str(action.tool),
                grant_id=grant.id if grant is not None else None,
                authorized=False,
                reason=reason,
            )
            return StepVerdict(reason=reason, status=status)

        if self.jobs is not None and self.jobs.cancelled(request.job_id):
            return refuse("Stopped.", JobStatus.CANCELLED)
        if state is not None and state.revoked:
            return refuse("Stopped.", JobStatus.CANCELLED)

        tool = self._lookup_quiet(str(action.tool))
        if tool is None:
            return refuse("I don't have a tool for that.", JobStatus.FAILED)

        assessment = self._assess(action, job.goal if job is not None else "")
        self._emit_built(
            "judgment_event",
            action,
            assessment,
            job_id=request.job_id,
            step_index=request.step_index,
            grant_id=grant.id if grant is not None else None,
        )
        disposition = self._decide(action, tool.spec, assessment)
        self._emit_built(
            "disposition_event",
            action,
            disposition,
            job_id=request.job_id,
            step_index=request.step_index,
            grant_id=grant.id if grant is not None else None,
        )
        if disposition.tier is RiskTier.REFUSE:
            return refuse(f"I won't do that. {disposition.reason}", JobStatus.CANCELLED)

        via = "implicit"
        warrant: Warrant | None = None
        if disposition.tier >= RiskTier.CONFIRM_VOICE:
            warrant = self._grant_answer(tool, action, disposition, grant)
            via = "grant"
        if warrant is None and disposition.tier >= RiskTier.CONFIRM_VOICE:
            # The grant does not cover this step, so the user is asked about
            # THIS STEP -- not asked to widen the grant. A mid-run request for
            # more scope is the weakest possible moment to ask for it: the user
            # is not looking at the whole bargain any more, and "granting
            # another grant" is on the never-grantable list for that reason.
            if disposition.tier is RiskTier.CONFIRM_VISUAL:
                if not self._confirm_visual(tool, action, disposition, outcome):
                    return refuse("Okay, leaving it.", JobStatus.CANCELLED)
                via = "visual"
            else:
                if self.confirm is None or not self._confirm(action, outcome):
                    return refuse("Okay, leaving it.", JobStatus.CANCELLED)
                via = "voice"
            warrant = self.warrants.issue(
                action,
                disposition,
                via=via,
                now=self.now(),
                job_id=request.job_id,
                step_index=request.step_index,
            )
        elif warrant is None:
            # Below the confirmation tiers nothing was demanded, so nothing has
            # to be answered. The warrant is still issued: it binds the action
            # that was authorized to the action that runs, across the thread
            # boundary, at every tier.
            warrant = self.warrants.issue(
                action,
                disposition,
                via="implicit",
                now=self.now(),
                job_id=request.job_id,
                step_index=request.step_index,
            )
        self._emit_built(
            "job_step_event",
            job_id=request.job_id,
            step_index=request.step_index,
            tool=str(action.tool),
            grant_id=grant.id if grant is not None else None,
            warrant_id=warrant.id,
            authorized=True,
            via=via,
            tier=disposition.tier,
        )
        checkpoint_id = job.checkpoint_ids[-1] if job and job.checkpoint_ids else None
        return StepVerdict(
            warrant=warrant,
            tool=tool,
            disposition=disposition,
            via=via,
            checkpoint_id=checkpoint_id,
        )

    def _perform_step(
        self, job_id: str, action: ResolvedAction, verdict: StepVerdict, index: int
    ) -> ToolResult:
        """WORKER THREAD. The one thing that happens off the loop thread.

        It goes through `_execute` -- the same asserts, the same undo record,
        the same audit row -- because "the only caller of tool.run" was never a
        statement about which thread makes the call.
        """
        runner = self._runners.get(job_id)
        outcome = runner.outcome if runner is not None else JobOutcome(job_id=job_id)
        before = len(outcome.results)
        self._execute(
            verdict.tool,
            action,
            verdict.disposition,
            outcome,
            confirmed=True,
            warrant=verdict.warrant,
            quiet=True,
            checkpoint_id=verdict.checkpoint_id,
            job_id=job_id,
            step_index=index,
        )
        if len(outcome.results) > before:
            return outcome.results[-1]
        return ToolResult(ok=False, summary="That didn't work.", error="step failed", job_id=job_id)

    def service_jobs(self, *, timeout: float = 0.0) -> bool:
        """Answer ONE pending job event. Returns False when there was none.

        A drain point, and only ever called from one. Never re-entrant inside
        `_confirm` or `_confirm_visual`: see `_drain_allowed`.
        """
        if not self._drain_allowed:
            return False
        try:
            event = self._job_events.get(timeout=timeout) if timeout else self._job_events.get_nowait()
        except queue.Empty:
            return False
        if isinstance(event, StepRequest):
            verdict = self._authorize_step(event)
            event.reply.put(verdict)
            return True
        if isinstance(event, JobFinished):
            self._emit_built(
                "job_event",
                job_id=event.job_id,
                status=event.status,
                summary=event.summary,
                error=event.error,
            )
            return True
        return False

    def drain_jobs(self, *, timeout: float = 5.0) -> None:
        """Service job events until every runner has finished.

        Bounded by the per-get timeout rather than by a sleep loop: every
        handoff in this design is a queue put, including the one that says the
        job ended, so this blocks until something really happened and returns
        the moment nothing is left. That is what makes the threaded tests
        deterministic instead of usually-green.
        """
        while not self.jobs_idle():
            if not self.service_jobs(timeout=timeout):
                break

    def jobs_idle(self) -> bool:
        return self._job_events.empty() and all(
            r.done.is_set() for r in self._runners.values()
        )

    def _lookup_quiet(self, name: str) -> Any:
        """`_lookup` without the speech. A job step that names a missing tool
        is reported through a notice, not shouted over whoever is talking."""
        if self.registry is None:
            return None
        try:
            tool = self.registry.get(name)
        except Exception:  # noqa: BLE001 -- unknown tool is routine, see _lookup
            return None
        return tool if isinstance(getattr(tool, "spec", None), ToolSpec) else None

    # -- notices -----------------------------------------------------------

    def drain_notices(self, outcome: TurnOutcome | None = None) -> str:
        """Say what background work has been waiting to report. ONE line.

        Called at safe moments only: right after daa has finished speaking in a
        turn the user addressed to it, or after a stretch of silence. Never on
        an utterance the gate judged not-addressed -- that is another person
        talking, and speaking into it is exactly the failure the address gate
        exists to prevent. Notices consume the gate's output; they never bypass
        it.
        """
        if not self._drain_allowed:
            return ""
        text, spoken, expired = self.notices.drain()
        for notice in spoken:
            self._emit_built("notice_event", job_id=notice.job_id, spoken=True,
                             urgency=notice.urgency)
        if expired:
            self._emit_built(
                "notice_event", job_id="", spoken=False, reason="stale", pending=expired
            )
        if text:
            self._speak_aside(text, outcome)
        return text

    def _speak_aside(self, text: str, outcome: TurnOutcome | None = None) -> None:
        """Speak something that is NOT part of the conversation.

        Deliberately not `_speak`: a notice must never enter the transcript,
        because the transcript is what the LLM sees next turn. A notice is not
        an utterance -- it may not re-enter the address gate, may not reach the
        model, and may not start a turn or a tool call. It is TTS, from a
        string a job produced, and nothing else.
        """
        if not text:
            return
        if outcome is not None:
            outcome.spoken.append(text)
        self._emit("spoke_aside", chars=len(text))
        if self.speaker is not None:
            try:
                self.speaker.say(text)
            except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
                self._emit("error", where="speak", error=str(exc))

    # -- checkpoints -------------------------------------------------------

    def rollback(self, checkpoint_id: str, *, replies: Sequence[str] = ()) -> TurnOutcome:
        """Reverse a whole window of work: one consent, N validated executions.

        Three things make this different from stacking inverses:

        1. The user hears ONE question. Each inverse is still individually
           validated -- the forgery check, the inverses check, the staleness
           check -- because the consent is what is aggregated, not the
           verification.
        2. It STOPS at the first problem and says so. A rollback that is
           all-or-nothing is nothing, and one that ploughs through stale rows
           is a second mutation wearing an undo's clothes.
        3. Sealing changes the sentence rather than disabling the rollback.
           Checkpoints do not make irreversible things reversible; they make
           the boundary speakable.
        """
        outcome = TurnOutcome(utterance="rollback", woke=True)
        self._scripted_replies = list(replies)
        if self.journal is None or not hasattr(self.journal, "window"):
            self._speak("I don't have a record of that stretch.", outcome)
            self.outcomes.append(outcome)
            return outcome
        entries = list(self.journal.window(checkpoint_id))
        sealed_reason = self.journal.seal_reason(checkpoint_id) or ""
        if not entries:
            self._speak(_ROLLBACK_NOTHING, outcome)
            self.outcomes.append(outcome)
            return outcome

        question = f"Should I put back the {_count(len(entries))} I did in that stretch?"
        if sealed_reason:
            # The sealed sentence, and the reason this whole section is worth
            # having: the boundary is stated BEFORE the yes, not after it.
            question = (
                f"I can put back the {_count(len(entries))} I did, "
                f"but {sealed_reason.rstrip('.')}. Should I?"
            )
        self._speak(question, outcome)
        pending = ResolvedAction(
            tool="rollback", args={}, targets=(), verb="put that stretch back"
        )
        self._confirming += 1
        try:
            agreed = self.confirm is not None and self._confirm_loop(pending, outcome, log=None)
        finally:
            self._confirming -= 1
        if not agreed:
            self._speak("Okay, leaving it.", outcome)
            self._emit_built(
                "rollback_event",
                checkpoint_id=checkpoint_id,
                attempted=len(entries),
                reversed_count=0,
                stopped_reason="not confirmed",
                sealed=bool(sealed_reason),
            )
            self.outcomes.append(outcome)
            return outcome

        done = 0
        stopped = ""
        for entry in entries:
            refusal = self._undo_refusal(entry)
            if refusal is not None:
                stopped = refusal
                break
            if entry.stale:
                stopped = str(entry.stale)
                break
            ran = self._handle_call(
                ToolCall(name=str(entry.tool), args=dict(entry.args or {})),
                str(entry.description or "rollback"),
                outcome,
                min_tier=RiskTier.CONFIRM_VOICE,
                raise_reason=_ROLLBACK_REASON,
                aggregate=_ROLLBACK_OK,
            )
            if not ran:
                stopped = "one of them wouldn't go back"
                break
            self._commit_undo(entry)
            done += 1

        self._speak(_rollback_summary(done, len(entries), stopped, sealed_reason), outcome)
        self._emit_built(
            "rollback_event",
            checkpoint_id=checkpoint_id,
            attempted=len(entries),
            reversed_count=done,
            stopped_reason=stopped,
            sealed=bool(sealed_reason),
        )
        self.outcomes.append(outcome)
        return outcome

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
        outcome: Any,
        *,
        confirmed: bool,
        visual: object | None = None,
        warrant: Warrant | None = None,
        quiet: bool = False,
        checkpoint_id: str | None = None,
        job_id: str | None = None,
        step_index: int | None = None,
    ) -> bool:
        """THE ONLY CALLER OF THE TOOL IN THIS CODEBASE.

        The asserts are not defensive noise -- they are the enforcement point
        for the one rule that makes the rest of the design mean anything. If a
        future refactor routes around policy, the process dies here rather than
        quietly deleting something.

        Returns True only when the tool really ran and really succeeded; a dry
        run returns False, because nothing happened.

        IT MAY NOW BE CALLED FROM ANOTHER THREAD. That was never the stated
        invariant -- the invariant is that this is the only function that
        invokes a tool -- and the assertion block below runs on every call
        whatever thread makes it. What crosses the boundary is a `Warrant`
        rather than `confirmed=True`, because a boolean cannot survive a queue
        hop and a clock: by the time a worker acts on it the grant may have
        been revoked, the action rebuilt, or the readback gone two minutes
        stale. A warrant carries the digest of the action it was issued for, so
        it cannot be reused for a different one or replayed after it expires.
        """
        assert isinstance(disposition, Disposition), "tool.run requires a Disposition"
        assert disposition.tier is not RiskTier.REFUSE, "refused actions must never run"
        visual_ok = False
        if warrant is not None:
            grant_mod = _grant_module()
            assert warrant.action_digest == grant_mod.action_digest(action), (
                "the warrant was issued for a different action"
            )
            assert self.now() < warrant.expires_at, "the warrant has expired"
            spent = self.warrants.spend(warrant.id, now=self.now())
            # Single use. A warrant that has already paid for a step, or that
            # was invalidated when its grant was revoked, is not a warrant.
            assert spent is not None, "the warrant was already spent or is unknown"
            visual_ok = spent.visual_ok
        assert (
            disposition.tier is not RiskTier.CONFIRM_VISUAL or visual is _VISUAL_OK or visual_ok
        ), "voice cannot authorize visual tier"
        if disposition.tier is RiskTier.CONFIRM_VOICE:
            assert confirmed or warrant is not None, (
                "confirm-tier actions require an affirmative confirmation"
            )
        grant_id = warrant.grant_id if warrant is not None else None
        warrant_id = warrant.id if warrant is not None else None
        link = {
            "grant_id": grant_id,
            "warrant_id": warrant_id,
            "job_id": job_id,
            "step_index": step_index,
        }

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
                **link,
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
                    checkpoint_id=checkpoint_id,
                    **link,
                )
                if not quiet:
                    self._speak("That didn't work.", outcome)
                return False
            ran_for_real = bool(result.ok)
            # Record BEFORE speaking: if TTS hangs, the undo must still exist.
            entry_id = self._record_undo(
                tool, result, checkpoint_id=checkpoint_id or result.checkpoint_id
            )
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
                checkpoint_id=checkpoint_id or result.checkpoint_id,
                **link,
            )

        outcome.results.append(result)
        if not result.ok:
            if not quiet:
                self._speak(result.summary or "That didn't work.", outcome)
            return False
        # SILENT means silent: read-only actions the user did not ask to hear
        # about are exactly the ones that make an assistant feel chatty. `quiet`
        # is the same rule one level up: a background job narrating every step
        # is the loudest possible way to be in the background, so its steps say
        # nothing and the job posts ONE notice when it is done.
        if disposition.tier is not RiskTier.SILENT and not quiet:
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


_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count(n: int) -> str:
    """Spoken counts. "six things", not "6 things": this is read aloud."""
    word = _WORDS[n] if 0 <= n < len(_WORDS) else str(n)
    return f"{word} thing" if n == 1 else f"{word} things"


def _rollback_summary(done: int, total: int, stopped: str, sealed: str) -> str:
    """The honest sentence about a partial rollback.

    "I put back four of the six. The other two have changed since, so I've
    left them alone." A rollback that reports success when it stopped half way
    is worse than one that fails outright, because the user stops checking.
    """
    if done == 0:
        head = "I couldn't put any of it back."
    elif done == total:
        head = f"Put back {_count(done)}." if total > 1 else "Put that back."
    else:
        left = total - done
        head = (
            f"I put back {_WORDS[done] if done < len(_WORDS) else done} of "
            f"{_WORDS[total] if total < len(_WORDS) else total}."
        )
        head += f" The other {_WORDS[left] if left < len(_WORDS) else left} I've left alone."
    if stopped and done < total:
        head += f" {stopped[0].upper()}{stopped[1:].rstrip('.')}."
    if sealed:
        head += f" {sealed[0].upper()}{sealed[1:].rstrip('.')}, so that part stands."
    return head


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


_GRANT_MOD: Any = None


def _grant_module() -> Any:
    """safety/grant.py, imported lazily like every other sibling in this file.

    Not a module-scope import: this file's whole discipline is that the four
    subsystems arrive through the constructor, so a half-built tree still
    imports and still tests.
    """
    global _GRANT_MOD
    if _GRANT_MOD is None:
        from daa.safety import grant as module

        _GRANT_MOD = module
    return _GRANT_MOD


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

    jobs = None
    try:
        from daa.voice.jobs import DEFAULT_JOBS_PATH
        from daa.voice.jobs import JobRegistry as _JobRegistry

        # Constructing it is also what rewrites any row still claiming to be
        # RUNNING as INTERRUPTED, so the "I was part way through..." offer is
        # available from the first moment the loop exists.
        jobs = _JobRegistry(DEFAULT_JOBS_PATH)
    except (ImportError, OSError) as exc:
        missing.append(f"daa.voice.jobs: {exc}")

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
        jobs=jobs,
    )
    # Not an error and not a log line: `daa doctor` reads this and prints it,
    # which is the only place a half-wired tree should be visible.
    loop.missing = missing  # type: ignore[attr-defined]
    return loop


def _llm_reason(exc: BaseException) -> str:
    """One short spoken clause naming the actual cause.

    Providers put the useful sentence in the response body, not in the
    exception class, so prefer the body when we can reach it.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
        if msg:
            return str(msg).rstrip(".").lower()
    status = getattr(exc, "status_code", None)
    if status == 402:
        return "the account is out of credit"
    if status == 401:
        return "the API key was rejected"
    if status == 404:
        return "that model name does not exist"
    if status:
        return f"the provider returned {status}"
    return type(exc).__name__
