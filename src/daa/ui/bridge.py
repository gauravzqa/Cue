"""`daa bridge`: the loop, speaking the dock protocol on stdin/stdout.

Nothing here decides anything. Every rule that matters -- the tier, the
confirmation, the undo record, the audit row -- still lives in `voice/loop.py`,
`safety/` and `jev/`, and this file is the adapter that lets a Swift app be the
loop's microphone, its transcriber and its screen. The three seams it uses are
the three the loop already documents:

    AudioSource      -> BridgeMic, fed by `mic.utterance` frames
    Transcriber      -> BridgeTranscriber, decoding what the dock already heard
    console.present  -> DockConsole, raising the approval card

`VoiceLoop.run()` is untouched, which is the point. `self._segments` still
exists, so `_next_reply()` still works, so a spoken "yes" to a CONFIRM_VOICE
readback arrives through exactly the same path as any other utterance.

THREE THREADS, AND NO ASYNCIO.

    reader  owns stdin. One JSON object per line, decoded and dispatched. An
            unparseable line is logged to stderr and skipped -- never fatal,
            because a bridge that exits is a dock that silently stops updating.
    turn    runs `VoiceLoop.run()` exactly as `daa listen` does, blocking on
            the BridgeMic queue. Single-threaded, unchanged semantics.
    writer  a lock-guarded stdout with a bounded queue, flushing every batch.

fd 1 IS THE PROTOCOL AND NOTHING ELSE. `steal_stdout` takes a private
duplicate of it and points every remaining writer at stderr, so a stray
`print()` anywhere in the tree -- or in a dependency, at import time, on a
machine we have never seen -- lands in the log rather than in the middle of an
approval card.

WHAT THE BRIDGE MAY NOT DO, from `ui/INTEGRATION.md` §6:

    no audio device (Swift owns the microphone, the VAD and the local STT);
    no `setsid`, no double fork, no daemonize, no disclaiming -- the bridge
    stays an ordinary child of the app or every TCC grant moves back onto the
    interpreter, which is the entire reason this architecture exists;
    no policy in the protocol: the dock renders `tier` and `reason` as opaque
    strings and is never given a field it is expected to reason about.
"""

from __future__ import annotations

import collections
import math
import queue
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from daa.contracts import AuditEvent
from daa.ui import protocol, steal_stdout
from daa.ui.protocol import CHATTY_KINDS, Event, FrameError, Method, Request, Response
from daa.voice.mic import AudioChunk, SpeechListener
from daa.voice.stt import Transcription

__all__ = [
    "Bridge",
    "BridgeGate",
    "BridgeMic",
    "BridgeTranscriber",
    "DockConsole",
    "Writer",
    "serve",
    "steal_stdout",
]

DAA_VERSION = "0.1.0"

# The card's fail-closed deadline. `console.ask()` on a terminal blocks
# forever; `present()` must not, because an approval you walked away from is
# not an approval. The dock clamps whatever we send to 5-300s.
CONFIRM_TIMEOUT_S = 90.0

# Frames waiting to be written. Past this the chatty audit kinds are dropped
# (see `Writer.put`): the wire copy is a MIRROR, and `~/.daa/audit.jsonl` is
# the record. Losing a display line is a cosmetic failure; blocking the loop
# thread on a slow pipe is not.
WRITE_QUEUE_LIMIT = 2048


def log(message: str) -> None:
    """stderr, which the dock tees to ~/Library/Logs/daa/python.log."""
    try:
        sys.stderr.write(f"daa bridge: {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001, S110 -- logging must never be the failure
        pass


# `steal_stdout` lives in `daa/ui/__init__.py`, which imports nothing, because
# it has to run before this module does. Re-exported here so the bridge's own
# surface is one import.
_ = steal_stdout


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------


class Writer:
    """The only thing that writes to the dock.

    A bounded deque plus one thread. `put` NEVER blocks: the loop thread
    calling it may be holding an approval open or half way through an
    execution, and a slow reader on the other end of a pipe must not be able
    to stall that.
    """

    def __init__(self, stream: Any, *, limit: int = WRITE_QUEUE_LIMIT) -> None:
        self._stream = stream
        self._limit = limit
        self._cv = threading.Condition()
        self._queue: collections.deque[tuple[str, bytes]] = collections.deque()
        self._closed = False
        self._thread: threading.Thread | None = None
        # Counted, not raised. Reported by `doctor` so a dock that looks stale
        # can be told apart from one that is merely quiet.
        self.dropped = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._pump, name="daa-writer", daemon=True)
        self._thread.start()

    def close(self, *, drain: bool = True) -> None:
        with self._cv:
            self._closed = True
            if not drain:
                self._queue.clear()
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    @property
    def alive(self) -> bool:
        with self._cv:
            return not self._closed

    # -- the queue ------------------------------------------------------

    def put(self, frame: Any, *, kind: str = "") -> bool:
        """Queue one frame. False means it will never be sent.

        `kind` is the audit kind for an `audit` event and "" for everything
        else. On overflow the OLDEST chatty record is evicted to make room;
        a decision record is never evicted to make room for anything. If the
        queue is full of decisions, the newcomer is dropped and counted --
        because the alternative is blocking the loop, and the disk log has the
        row either way.
        """
        try:
            payload = protocol.encode(frame)
        except Exception as exc:  # noqa: BLE001 -- an unencodable frame is ours to eat
            log(f"could not encode a {kind or 'frame'}: {exc}")
            return False
        with self._cv:
            if self._closed:
                return False
            if len(self._queue) >= self._limit and not self._evict_chatty():
                self.dropped += 1
                return False
            self._queue.append((kind, payload))
            self._cv.notify()
        return True

    def _evict_chatty(self) -> bool:
        """Drop the oldest display-only record. Caller holds the lock."""
        for index, (kind, _payload) in enumerate(self._queue):
            if kind in CHATTY_KINDS:
                del self._queue[index]
                self.dropped += 1
                return True
        return False

    # -- the thread -----------------------------------------------------

    def _pump(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if not self._queue:
                    break
                batch = [payload for _kind, payload in self._queue]
                self._queue.clear()
            try:
                self._stream.write(b"".join(batch))
                # Python's stdout is FULLY BUFFERED on a pipe. `-u` is passed
                # and PYTHONUNBUFFERED is set, and this flush is here anyway:
                # forgetting it produces a dock that hangs forever waiting for
                # a `ready` frame sitting in an 8 KiB buffer.
                self._stream.flush()
            except Exception as exc:  # noqa: BLE001 -- a dead pipe is a dead dock
                log(f"the dock stopped reading ({exc})")
                with self._cv:
                    self._closed = True
                    self._queue.clear()
                return


# ---------------------------------------------------------------------------
# the mic seam
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Turn:
    """What the dock said ABOUT the utterance the transcriber is decoding.

    Written by `BridgeTranscriber.transcribe` and read by `BridgeGate` a few
    lines later, both on the turn thread, in that order, every time -- see
    `VoiceLoop.handle_chunk`. It exists because the gate's signature takes an
    utterance and a context, and `addressed` is a property of neither.
    """

    addressed: bool = True
    complete: bool = True
    confidence: float = 1.0


class _Work:
    """A callable to run ON THE TURN THREAD, between turns.

    The dock's undo button has to reach `VoiceLoop.undo_last`, which speaks,
    asks and may execute. Running that on the reader thread would put two
    turns through one VoiceLoop at once. So it is queued with the audio and
    performed at the one instant the loop is provably idle: inside
    `BridgeMic.segments`, blocked, waiting for the next chunk.
    """

    __slots__ = ("fn", "name")

    def __init__(self, fn: Callable[[], None], name: str = "work") -> None:
        self.fn = fn
        self.name = name

    def perform(self) -> None:
        try:
            self.fn()
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            log(f"{self.name} blew up: {exc}")


_CLOSED = object()


class BridgeMic:
    """An `AudioSource` fed by the dock instead of by a sound card.

    `AudioChunk.pcm` already documents carrying a non-PCM payload for the
    fakes -- "the mic's payload is opaque bytes" is the contract, and the
    reason it is a contract is so the loop can never read the mic without
    going through a Transcriber. The bridge uses the same door: the payload is
    the UTF-8 text the dock's on-device recogniser already produced, and only
    `BridgeTranscriber` may decode it.
    """

    def __init__(self, *, work_allowed: Callable[[], bool] | None = None) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._held: collections.deque[_Work] = collections.deque()
        self._listener: SpeechListener | None = None
        self._closed = False
        # False while a FOREGROUND confirmation is in flight: `_next_reply`
        # also pumps this generator, and running the undo button's work there
        # would steal the answer the user is giving to the question in front
        # of them. `VoiceLoop._drain_allowed` is the loop's own name for this.
        self._work_allowed = work_allowed if work_allowed is not None else (lambda: True)
        # Called when the generator is about to block. The argument is False
        # when a foreground confirmation is in flight.
        self.on_idle: Callable[[bool], None] | None = None
        self.on_chunk: Callable[[], None] | None = None

    # -- AudioSource ----------------------------------------------------

    def segments(self) -> Iterator[AudioChunk]:
        while not self._closed:
            if self._held and self._work_allowed():
                self._held.popleft().perform()
                continue
            if self.on_idle is not None:
                # "Blocked here" means two different things. With no
                # confirmation in flight the loop is genuinely idle; inside
                # `_next_reply` it is waiting for the user to answer a
                # question it just asked, which is not the same state and must
                # not render as a calm dock.
                self.on_idle(self._work_allowed())
            item = self._queue.get()
            if item is _CLOSED or self._closed:
                return
            if isinstance(item, _Work):
                if self._work_allowed():
                    item.perform()
                else:
                    # Held rather than run, and rather than dropped: it drains
                    # at the top of this loop the moment the confirmation in
                    # front of the user has been answered.
                    self._held.append(item)
                continue
            if self.on_chunk is not None:
                self.on_chunk()
            yield item

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        self._listener = listener

    def close(self) -> None:
        self._closed = True
        self._queue.put(_CLOSED)

    # -- fed by the reader ----------------------------------------------

    def push(self, text: str, *, complete: bool = True, started_at: float = 0.0) -> None:
        self._queue.put(
            AudioChunk(
                pcm=str(text).encode("utf-8"),
                started_at=float(started_at),
                complete=bool(complete),
            )
        )

    def defer(self, fn: Callable[[], None], *, name: str = "work") -> None:
        self._queue.put(_Work(fn, name))

    def onset(self) -> None:
        """`mic.onset`. Cheap and non-blocking by contract: in practice it is
        exactly `speaker.stop`, and it is what keeps barge-in working."""
        listener = self._listener
        if listener is None:
            return
        try:
            listener()
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            log(f"barge-in listener raised: {exc}")

    def drop_pending(self) -> int:
        """Esc. Discard queued audio; keep any deferred work."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _Work):
                self._held.append(item)
            elif item is _CLOSED:
                self._queue.put(_CLOSED)
                break
            else:
                dropped += 1
        return dropped


@dataclass(slots=True)
class BridgeTranscriber:
    """A `Transcriber` over text the dock already recognised.

    There is no model here and there must not be one: the dock holds the
    microphone grant and runs `SpeechTranscriber` on device, and a second
    recogniser in Python would be a second audio path to reason about.
    """

    turn: _Turn
    source: str = "local"

    @property
    def name(self) -> str:
        return "bridge"

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        text = chunk.pcm.decode("utf-8", errors="replace")
        self.turn.complete = bool(chunk.complete)
        return Transcription(
            text=text,
            confidence=float(self.turn.confidence),
            source=self.source,
            partial=not chunk.complete,
        )


@dataclass(slots=True)
class _Wake:
    """A WakeDecision. Same duck type `jev/gate.py` returns."""

    wake: bool = True
    end_of_turn: bool = True
    needs_planner: bool = False
    addressed_p: float = 1.0
    latency_ms: float = 0.0
    synthetic: bool = True


class BridgeGate:
    """`AddressGate`, with push-to-talk allowed past it.

    Holding a key is an unambiguous act of address -- the same argument
    `handle_text`'s docstring already makes for typing into the CLI -- so an
    utterance the dock marks `addressed` skips the gate. An utterance from an
    open microphone does not: it goes through the real gate unchanged, which
    is the line that makes always-on viable at all.

    `end_of_turn` is additionally ANDed with the dock's VAD. `complete` is
    false when the segment was cut by the user releasing the key or by the
    max-length timer rather than by silence, i.e. the user is probably still
    talking, and buffering a half sentence is always the safer read.
    """

    def __init__(self, inner: Any, turn: _Turn) -> None:
        self.inner = inner
        self.turn = turn

    def should_wake(self, utterance: str, context: Any) -> Any:
        if self.turn.addressed or self.inner is None:
            # `inner is None` is a tree with no Jev wired, where the loop's own
            # fallback is also "always awake". Parity, deliberately.
            return _Wake(wake=True, end_of_turn=self.turn.complete)
        decision = self.inner.should_wake(utterance, context)
        if self.turn.complete:
            return decision
        return _Wake(
            wake=bool(getattr(decision, "wake", True)),
            end_of_turn=False,
            needs_planner=bool(getattr(decision, "needs_planner", False)),
            addressed_p=float(getattr(decision, "addressed_p", 1.0)),
            latency_ms=float(getattr(decision, "latency_ms", 0.0)),
            synthetic=bool(getattr(decision, "synthetic", False)),
        )


# ---------------------------------------------------------------------------
# the console seam
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Card:
    reply: queue.Queue[tuple[bool, str]] = field(default_factory=lambda: queue.Queue(maxsize=1))


class DockConsole:
    """The screen, when the screen is an app.

    Implements `available()` and `present(action, disposition)` and NOTHING
    else. It deliberately has no `write` and no `ask`: `_confirm_visual` picks
    the `present` branch by the presence of the attribute, and a console that
    had both would be a console whose branch depended on the weather.

    `present` returns a bool and only a bool. It never sees `_VISUAL_OK` and
    neither does the protocol; `_confirm_visual` remains the only function in
    the codebase that can produce that token.
    """

    name = "dock"

    def __init__(
        self,
        *,
        send: Callable[[str, dict[str, Any]], bool],
        emit: Callable[[str, dict[str, Any]], None],
        audit: Callable[..., None],
        alive: Callable[[], bool],
        phrase: Callable[[Any], str],
        script_keys: tuple[str, ...],
        dry_run: Callable[[], bool],
        on_awaiting: Callable[[bool], None] | None = None,
        timeout_s: float = CONFIRM_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._emit = emit
        self._audit = audit
        self._alive = alive
        self._phrase = phrase
        self._script_keys = script_keys
        self._dry_run = dry_run
        self._on_awaiting = on_awaiting
        self._timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._cards: dict[str, _Card] = {}

    # -- the console protocol -------------------------------------------

    def available(self) -> bool:
        return bool(self._alive())

    def present(self, action: Any, disposition: Any) -> bool:
        """Raise the card and block this turn until it is answered.

        Blocks on a queue keyed by THIS card's token, never on the segment
        stream, so it cannot consume a scripted reply or a spoken answer meant
        for the foreground turn (`INTEGRATION.md` §4, requirement 4).

        Returns True only for a completed, deliberate approval. A timeout, a
        cancel, an Esc, a dock that died and a frame the dock could not render
        are all False, and each is audited under its own reason: a card that
        timed out is not the same event as a user who said no.
        """
        # A fresh random token, minted per card and single use. The model
        # cannot mint one; neither can a forged row in ~/.daa/undo.jsonl.
        token = "cf_" + secrets.token_hex(8)
        card = _Card()
        with self._lock:
            self._cards[token] = card

        try:
            payload = protocol.card_payload(
                action,
                disposition,
                dry_run=self._dry_run(),
                phrase=self._phrase(action),
                script_keys=self._script_keys,
                expires_in_ms=int(self._timeout_s * 1000),
            )
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            self._forget(token)
            self._audit("error", where="confirm", error=f"could not build the card: {exc}")
            return False

        if self._on_awaiting is not None:
            self._on_awaiting(True)
        try:
            if not self._send(token, payload):
                # A dead or unresponsive dock is a refusal, immediately.
                self._audit(
                    "confirm_card",
                    tool=payload["tool"],
                    card_id=token,
                    granted=False,
                    reason="brain stopped",
                )
                return False
            try:
                granted, reason = card.reply.get(timeout=self._timeout_s)
            except queue.Empty:
                granted, reason = False, "timeout"
                # Withdraw it, so the card does not sit on screen counting
                # down against a Python side that has already given up.
                self._emit(Method.CONFIRM_CANCEL, {"id": token, "reason": "timeout"})
            self._audit(
                "confirm_card",
                tool=payload["tool"],
                card_id=token,
                granted=bool(granted),
                reason=reason,
            )
            return granted is True
        finally:
            self._forget(token)
            if self._on_awaiting is not None:
                self._on_awaiting(False)

    # -- fed by the reader ----------------------------------------------

    def resolve(self, token: str, *, granted: bool, reason: str) -> bool:
        """Deliver one answer. False when the token is unknown or spent."""
        with self._lock:
            card = self._cards.pop(token, None)
        if card is None:
            return False
        try:
            card.reply.put_nowait((bool(granted), str(reason)))
        except queue.Full:  # pragma: no cover - the token is single use
            return False
        return True

    def withdraw_all(self, reason: str) -> None:
        """Every open card resolves as a refusal. Used when stdin closes."""
        with self._lock:
            tokens = list(self._cards)
        for token in tokens:
            self.resolve(token, granted=False, reason=reason)

    def _forget(self, token: str) -> None:
        with self._lock:
            self._cards.pop(token, None)


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


class Bridge:
    """Frames in, frames out, one VoiceLoop in the middle."""

    def __init__(
        self,
        *,
        loop: Any,
        stdin: Any,
        stdout: Any,
        missing: Any = (),
        version: str = DAA_VERSION,
        confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.loop = loop
        self._stdin = stdin
        self.writer = Writer(stdout)
        self.missing = list(missing)
        self.version = version
        self.now = now if now is not None else time.time
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._phase = ""
        self._detail = ""
        self._turn_thread: threading.Thread | None = None

        from daa.voice.loop import _SCRIPT_KEYS, _phrase

        self.turn = _Turn()
        self.mic = BridgeMic(work_allowed=lambda: bool(getattr(loop, "_drain_allowed", True)))
        self.mic.on_idle = self._idle
        self.mic.on_chunk = lambda: self.state("thinking", "")
        self.console = DockConsole(
            send=self._send_card,
            emit=self.emit,
            audit=self.audit,
            alive=lambda: self.writer.alive and not self._stop.is_set(),
            phrase=_phrase,
            script_keys=_SCRIPT_KEYS,
            dry_run=lambda: bool(getattr(self.loop.settings, "dry_run", True)),
            on_awaiting=self._awaiting,
            timeout_s=confirm_timeout_s,
        )

        # --- the three seams, wired into the loop -----------------------
        loop.mic = self.mic
        loop.local_stt = BridgeTranscriber(turn=self.turn)
        # Swift already did the recognition, so there is no audio left to
        # rescore -- and a cloud transcriber handed our UTF-8 payload would be
        # asked to decode text as if it were PCM.
        loop.cloud_stt = None
        loop.gate = BridgeGate(loop.gate, self.turn)
        loop.console = self.console
        loop.audit = self._tee(loop.audit)

    # -- outbound --------------------------------------------------------

    def emit(self, method: str, params: dict[str, Any], *, kind: str = "") -> bool:
        return self.writer.put(protocol.event(method, **params), kind=kind)

    def reply(self, fid: str, **params: Any) -> bool:
        return self.writer.put(protocol.response(fid, True, params))

    def refuse(self, fid: str, code: str, message: str) -> bool:
        return self.writer.put(protocol.error(fid, code, message))

    def state(self, phase: str, detail: str = "", **extra: Any) -> None:
        """Dedup'd, because a dock redrawing on every identical frame is a
        dock that looks busy when nothing is happening.

        The write happens UNDER the lock. Two threads deciding they had
        something new to say and then racing to say it would leave the dock
        showing whichever one lost, which for a phase is the difference
        between a dock that looks calm and a dock that is waiting for you.
        """
        with self._state_lock:
            if phase == self._phase and detail == self._detail and not extra:
                return
            self._phase, self._detail = phase, detail
            self.emit(
                Method.STATE, {"phase": phase, "detail": detail, "since": self.now(), **extra}
            )

    def audit(self, kind: str, **payload: Any) -> None:
        """Write one of the bridge's own rows through the loop's sink.

        Through the sink, not straight onto the wire: a row about a
        confirmation token belongs in `~/.daa/audit.jsonl` as much as any
        other, and going the long way round means it is redacted by the same
        code as everything else.
        """
        sink = getattr(self.loop, "audit", None)
        if sink is None:
            return
        try:
            sink(AuditEvent(kind=kind, payload=payload))
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            log(f"could not audit {kind}: {exc}")

    def _send_card(self, token: str, payload: dict[str, Any]) -> bool:
        return self.writer.put(protocol.request(token, Method.CONFIRM_REQUEST, **payload))

    def _awaiting(self, open_: bool) -> None:
        if open_:
            self.state("awaiting", "waiting for your approval")
        else:
            self.state("thinking", "")

    def _idle(self, quiet: bool) -> None:
        if quiet:
            self.state("idle")
        else:
            self.state("awaiting", "waiting for your answer")

    # -- the audit tee ---------------------------------------------------

    def _tee(self, inner: Any) -> Callable[[AuditEvent], None]:
        """`~/.daa/audit.jsonl` exactly as today, plus a copy on the wire.

        The read path needs no new instrumentation: `VoiceLoop.audit` is
        already an injected callable taking a structured, redacted event. The
        `finally` is load bearing -- a disk sink that throws must not cost the
        dock its transcript, and `VoiceLoop._send` already swallows whatever
        comes back out of here.
        """
        from daa.safety.audit import record, scrub

        def sink(event: AuditEvent) -> None:
            try:
                if inner is not None:
                    inner(event)
            finally:
                try:
                    self._project(event, record, scrub)
                except Exception as exc:  # noqa: BLE001 -- never take the loop down
                    log(f"audit tee: {exc}")

        return sink

    def _project(self, event: AuditEvent, record: Any, scrub: Any) -> None:
        kind = str(event.kind)
        payload = protocol.audit_payload(event, record=record, scrub=scrub)
        self.emit(Method.AUDIT, payload, kind=kind)
        # A couple of kinds are also a state change the dock should show.
        if kind == "spoke":
            text = str((event.payload or {}).get("text") or "")
            if text:
                self.emit(Method.SPEAK, {"text": scrub(text)})
                self.state("speaking", scrub(text))
        elif kind == "woke":
            self.state("thinking", "working out what to do")

    # -- inbound ---------------------------------------------------------

    def handle(self, frame: Any) -> None:
        if isinstance(frame, Request):
            self._request(frame)
        elif isinstance(frame, Response):
            self._response(frame)
        elif isinstance(frame, Event):
            self._event(frame)

    def _request(self, frame: Request) -> None:
        method, fid, p = frame.method, frame.id, frame.params

        if method == Method.HELLO:
            proto = p.get("proto")
            if proto not in (None, protocol.PROTOCOL_VERSION):
                self.refuse(
                    fid,
                    "proto",
                    f"this daa speaks protocol {protocol.PROTOCOL_VERSION}, the dock speaks {proto}",
                )
                log(f"refusing a protocol {proto} dock; expected {protocol.PROTOCOL_VERSION}")
                return
            self.reply(fid, proto=protocol.PROTOCOL_VERSION, daa=self.version)
            self.ready()
            # One line in ~/Library/Logs/daa/python.log saying the handshake
            # completed. A dock stuck before `ready` and a dock stuck after it
            # look identical from the outside, and this is the difference.
            log(f"hello from {p.get('app') or 'the dock'}; ready sent")
            return

        if method == Method.CONTROL_ALWAYS_ON:
            self.reply(fid, on=self._set_always_on(bool(p.get("on"))))
            return

        if method == Method.UNDO_LAST:
            ok, summary = self._begin_undo()
            self.reply(fid, ok=ok, summary=summary)
            return

        if method == Method.DOCTOR:
            self.reply(fid, **self.doctor())
            return

        if method == Method.SHUTDOWN:
            self.reply(fid)
            self._stop.set()
            return

        # Unimplemented methods answer rather than staying silent: the dock
        # times a request out at 30s and shows the failure, and a visible
        # "daa does not do that" beats half a minute of nothing.
        self.refuse(fid, "unknown-method", f"daa does not implement {method}")

    def _response(self, frame: Response) -> None:
        """The answer to a `confirm.request`. The token is single use."""
        if frame.ok:
            granted = frame.params.get("granted") is True
            reason = str(frame.params.get("reason") or ("approved" if granted else "cancelled"))
        else:
            # `ok:false`, or a `res` with no `ok` at all. Either way it is not
            # an approval -- the only thing a response ever authorises is an
            # action, and an unreadable answer is not one.
            granted, reason = False, frame.message or "unreadable request"
        if self.console.resolve(frame.id, granted=granted, reason=reason):
            return
        # An unknown, already-consumed or stale id is DROPPED and audited.
        self.audit("error", where="confirm", error=f"unknown confirmation token {frame.id}")
        log(f"dropped a stale confirm result {frame.id}")

    def _event(self, frame: Event) -> None:
        method, p = frame.method, frame.params

        if method == Method.MIC_ONSET:
            self.mic.onset()
            return

        if method in (Method.MIC_UTTERANCE, Method.CONTROL_TEXT):
            text = str(p.get("text") or "")
            if not text.strip():
                return
            # Push-to-talk and typed text are both unambiguous acts of
            # address; an open microphone is not. See BridgeGate.
            self.turn.addressed = bool(p.get("addressed", method == Method.CONTROL_TEXT))
            self.turn.confidence = _float(p.get("confidence"), 1.0)
            self.mic.push(
                text,
                complete=bool(p.get("complete", True)),
                started_at=_float(p.get("startedAt"), 0.0),
            )
            self.state("thinking", "")
            return

        if method == Method.CONTROL_CANCEL:
            self._cancel()
            return

        log(f"unhandled event {method}")

    # -- the things the dock asks for -------------------------------------

    def ready(self) -> None:
        """The dock's whole boot state in one frame, sent straight after the
        `session.hello` response."""
        settings = self.loop.settings
        speaker = getattr(self.loop, "speaker", None)
        tools = []
        registry = getattr(self.loop, "registry", None)
        if registry is not None:
            try:
                tools = [
                    {"name": str(s.name), "floor": getattr(s.floor, "name", str(s.floor))}
                    for s in registry.specs()
                ]
            except Exception as exc:  # noqa: BLE001 -- a broken registry is a report
                log(f"could not list tools: {exc}")
        self.emit(
            Method.READY,
            {
                "daa": self.version,
                # Never omitted. The dock defaults a missing dryRun to true and
                # shows the pill, and relying on that default is how a live
                # build ends up looking safe.
                "dryRun": bool(getattr(settings, "dry_run", True)),
                "alwaysOn": bool(getattr(settings, "always_on", False)),
                "jevLive": bool(getattr(settings, "jev_live", False)),
                "providers": {
                    # What the BRIDGE would use. The dock owns the microphone,
                    # the VAD and the recogniser; Python opens no audio device.
                    "mic": "bridge",
                    "stt": "bridge",
                    "tts": "live" if getattr(speaker, "name", "fake") != "fake" else "fake",
                    "llm": "live" if getattr(settings, "deepseek_api_key", None) else "fake",
                    "jev": "live" if getattr(settings, "jev_live", False) else "fake",
                },
                "tools": tools,
                "missing": [str(m) for m in self.missing],
            },
        )
        self.state("idle")

    def doctor(self) -> dict[str, Any]:
        settings = self.loop.settings
        speaker = getattr(self.loop, "speaker", None)
        return {
            "daa": self.version,
            "dryRun": bool(getattr(settings, "dry_run", True)),
            "alwaysOn": bool(getattr(settings, "always_on", False)),
            "jevLive": bool(getattr(settings, "jev_live", False)),
            "providers": {
                "mic": "bridge",
                "stt": "bridge",
                "tts": str(getattr(speaker, "name", "fake")),
                "llm": "live" if getattr(settings, "deepseek_api_key", None) else "fake",
                "jev": "live" if getattr(settings, "jev_live", False) else "fake",
            },
            "modules": {
                name: getattr(self.loop, attr, None) is not None
                for name, attr in (
                    ("jev.gate", "gate"),
                    ("jev.router", "router"),
                    ("jev.risk", "risk"),
                    ("jev.confirm", "confirm"),
                    ("tools.registry", "registry"),
                    ("safety.policy", "policy_decide"),
                    ("safety.undo", "journal"),
                    ("safety.audit", "audit"),
                )
            },
            "missing": [str(m) for m in self.missing],
            "droppedFrames": self.writer.dropped,
        }

    def _set_always_on(self, on: bool) -> bool:
        """Returns the ACTUAL state. The dock reverts its switch if this
        disagrees, because a control that lies about a hot microphone is the
        worst control on the panel."""
        settings = self.loop.settings
        try:
            self.loop.settings = replace(settings, always_on=bool(on))
        except Exception as exc:  # noqa: BLE001 -- a settings object we cannot copy
            log(f"could not change always-on: {exc}")
            return bool(getattr(settings, "always_on", False))
        return bool(getattr(self.loop.settings, "always_on", False))

    def _begin_undo(self) -> tuple[bool, str]:
        """Answer now, undo on the turn thread.

        Undo never runs below CONFIRM_VOICE -- the journal is a file any local
        process can append to, so every row is a claim -- which means the
        honest immediate answer is that daa is ASKING, not that it reversed
        anything. `peek()` is contractually side-effect free, so checking
        whether there is anything to ask about costs nothing.
        """
        journal = getattr(self.loop, "journal", None)
        if journal is None:
            return False, "I don't have an undo journal."
        try:
            entry = journal.peek()
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            self.audit("error", where="journal", error=str(exc))
            return False, "I couldn't read the undo journal."
        if entry is None:
            return False, "There's nothing to undo."
        loop = self.loop
        self.mic.defer(lambda: loop.undo_last(), name="undo")
        return False, "Asking out loud before I reverse that."

    def _cancel(self) -> None:
        """Esc: abandon the current turn and say nothing.

        Nothing here interrupts work already in flight -- an execution that
        has started is not something a keystroke gets to half-finish. What it
        does is stop the speaking, drop audio that has not been looked at yet,
        and forget the half sentence the gate was holding, so the abandoned
        fragment does not join the next thing the user says.
        """
        speaker = getattr(self.loop, "speaker", None)
        if speaker is not None:
            try:
                speaker.stop()
            except Exception as exc:  # noqa: BLE001 -- isolation boundary
                log(f"could not stop the speaker: {exc}")
        dropped = self.mic.drop_pending()
        try:
            self.loop._pending = ""
        except Exception as exc:  # noqa: BLE001 -- isolation boundary
            log(f"could not clear the buffered utterance: {exc}")
        self.audit("abandoned", tool="", reason="you pressed escape", dropped=dropped)
        self.state("idle")

    # -- running ---------------------------------------------------------

    def serve(self) -> int:
        """Reader loop. Returns when stdin closes or the dock says shutdown."""
        self.writer.start()
        loop = self.loop
        self._turn_thread = threading.Thread(
            target=lambda: self._drive(loop), name="daa-turn", daemon=True
        )
        self._turn_thread.start()
        log("up")
        try:
            while not self._stop.is_set():
                # `readline`, not `for line in stdin`: explicit about blocking
                # until a newline or EOF, with no read-ahead to wonder about.
                raw = self._stdin.readline()
                if not raw:
                    break
                try:
                    frame = protocol.decode(raw)
                except FrameError as exc:
                    # Logged and skipped. One bad line is one bad line.
                    log(f"bad frame ({exc})")
                    continue
                try:
                    self.handle(frame)
                except Exception as exc:  # noqa: BLE001 -- isolation boundary
                    log(f"handler blew up on {getattr(frame, 'method', '?')}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- a broken pipe is a closed dock
            log(f"stdin stopped ({exc})")
        finally:
            self.shutdown()
        return 0

    def _drive(self, loop: Any) -> None:
        try:
            loop.run()
        except Exception as exc:  # noqa: BLE001 -- the backstop `daa listen` has
            log(f"the loop stopped on an unexpected error: {exc}")
            self.audit("error", where="loop", error=str(exc))
            self.state("degraded", "daa's loop stopped")

    def shutdown(self) -> None:
        self._stop.set()
        # Every card on screen resolves as a refusal. There is no consent
        # recorded for a question nobody is left to answer.
        self.console.withdraw_all("brain stopped")
        self.mic.close()
        thread = self._turn_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        speaker = getattr(self.loop, "speaker", None)
        if speaker is not None:
            try:
                speaker.stop()
            except Exception:  # noqa: BLE001, S110 -- we are on the way out
                pass
        self.writer.close()
        log("down")


def _float(value: Any, fallback: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if math.isnan(out) else out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def serve(stdout: Any, *, stdin: Any = None, settings: Any = None) -> int:
    """Build the real loop and speak the protocol until stdin closes.

    `stdout` is the PRIVATE duplicate of fd 1 that `steal_stdout` returned;
    nothing else in the process can reach it.
    """
    from daa.config import Settings
    from daa.voice.loop import build_loop

    settings = settings if settings is not None else Settings.load()
    # No mic: `build_loop` would otherwise wire a real one, and the bridge
    # must never open an audio device. `Bridge.__init__` installs the
    # BridgeMic before anything is pulled from it.
    loop = build_loop(settings, mic=None)
    bridge = Bridge(
        loop=loop,
        stdin=stdin if stdin is not None else sys.stdin.buffer,
        stdout=stdout,
        missing=getattr(loop, "missing", []),
    )
    return bridge.serve()
