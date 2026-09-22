"""The bridge's moving parts: the writer, the mic seam, the card, the reader.

Every test here is deterministic. Nothing sleeps waiting for a thread to get
round to something: the helpers block on a real `threading.Event` that the
code under test sets, and the one test that is ABOUT a timeout injects the
timeout. A flaky concurrency test trains people to re-run, which is worse than
no test at all -- the same argument `voice/jobs.py` makes for its queue.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from collections.abc import Sequence
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
)
from daa.ui import protocol
from daa.ui.bridge import Bridge, BridgeMic, DockConsole, Writer
from daa.ui.protocol import Method, Request, Response
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import VoiceLoop

TIMEOUT = 5.0  # only ever reached by a FAILING test


# ---------------------------------------------------------------------------
# In-memory pipes
# ---------------------------------------------------------------------------


class PipeOut:
    """A writable that records frames and wakes anyone waiting on them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.raw = bytearray()
        self.flushes = 0
        self.explode: Exception | None = None

    def write(self, data: bytes) -> int:
        with self._cv:
            if self.explode is not None:
                raise self.explode
            self.raw.extend(data)
            self._cv.notify_all()
        return len(data)

    def flush(self) -> None:
        with self._cv:
            self.flushes += 1
            self._cv.notify_all()

    # -- reading it back ------------------------------------------------

    def frames(self) -> list[Any]:
        with self._cv:
            text = bytes(self.raw).decode("utf-8")
        return [protocol.decode(line) for line in text.splitlines() if line.strip()]

    def wait(self, predicate, *, timeout: float = TIMEOUT) -> list[Any]:
        """Block until `predicate(frames)` is true. Fails the test otherwise."""
        deadline = threading.Event()
        with self._cv:
            while True:
                frames = [
                    protocol.decode(line)
                    for line in bytes(self.raw).decode("utf-8").splitlines()
                    if line.strip()
                ]
                if predicate(frames):
                    return frames
                if not self._cv.wait(timeout=timeout):
                    break
        assert deadline is not None
        raise AssertionError(f"the bridge never wrote what was expected; saw {self.frames()}")

    def wait_for_method(self, method: str, **timeout: float) -> Any:
        frames = self.wait(lambda fs: any(getattr(f, "method", None) == method for f in fs))
        return next(f for f in frames if getattr(f, "method", None) == method)

    def methods(self) -> list[str]:
        return [getattr(f, "method", "") for f in self.frames()]


class PipeIn:
    """A readable whose `readline` blocks until the test pushes a line."""

    def __init__(self) -> None:
        self._q: queue.Queue[bytes] = queue.Queue()

    def push(self, frame: Any) -> None:
        self._q.put(protocol.encode(frame) if not isinstance(frame, bytes) else frame)

    def push_raw(self, line: str) -> None:
        self._q.put(line.encode("utf-8") + b"\n")

    def close(self) -> None:
        self._q.put(b"")

    def readline(self) -> bytes:
        return self._q.get()


# ---------------------------------------------------------------------------
# A real VoiceLoop, with everything faked
# ---------------------------------------------------------------------------

SCRIPTED = ToolSpec(
    name="run_applescript",
    description="Run an AppleScript",
    params={"script": {"type": "string"}},
    floor=RiskTier.CONFIRM_VISUAL,
)
SCRIPT_BODY = 'tell application "System Events"\n  keystroke "x"\nend tell'


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
        return ToolResult(ok=True, summary="Ran the script.")


@dataclass(slots=True)
class StubRegistry:
    tools: dict[str, Any] = field(default_factory=dict)

    def register(self, tool: Any) -> None:
        self.tools[tool.spec.name] = tool

    def get(self, name: str) -> Any:
        return self.tools[name]

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]


@dataclass(slots=True)
class StubConfirm:
    verdicts: list[str] = field(default_factory=lambda: ["yes"])
    calls: list[str] = field(default_factory=list)

    def interpret(self, reply: str, action: Any) -> str:
        self.calls.append(reply)
        return self.verdicts.pop(0) if self.verdicts else "unclear"


@dataclass(slots=True)
class StubJournal:
    entries: list[Any] = field(default_factory=list)

    def peek(self) -> Any:
        return self.entries[-1] if self.entries else None

    def record(self, *a: Any, **kw: Any) -> str:
        return "u1"

    def history(self) -> list[Any]:
        return list(self.entries)


ASSESSMENT = RiskAssessment(
    blast_radius=2.4,
    unrecoverable=0.81,
    explicitly_requested=0.19,
    target_confidence="probable",
    confidence=0.77,
    synthetic=True,
)


def stub_policy(tier: RiskTier):
    def decide(action: Any, spec: Any, assessment: Any, settings: Any) -> Disposition:
        return Disposition(tier=tier, reason="because the stub said so", assessment=ASSESSMENT)

    return decide


@dataclass(slots=True)
class Harness:
    bridge: Bridge
    loop: VoiceLoop
    out: PipeOut
    stdin: PipeIn
    tool: ScriptTool
    events: list[AuditEvent]
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.bridge.serve, name="serve", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stdin.close()
        if self.thread is not None:
            self.thread.join(timeout=TIMEOUT)
            assert not self.thread.is_alive(), "the reader did not stop when stdin closed"

    def hello(self) -> None:
        self.stdin.push(protocol.request("h1", Method.HELLO, proto=1, app="0.1.0"))
        self.out.wait_for_method(Method.READY)


def harness(
    *,
    tier: RiskTier = RiskTier.CONFIRM_VISUAL,
    dry_run: bool = False,
    calls: Sequence[ToolCall] = (ToolCall("run_applescript", {"script": SCRIPT_BODY}),),
    journal: Any = None,
    confirm_timeout_s: float = 30.0,
) -> Harness:
    tool = ScriptTool()
    registry = StubRegistry()
    registry.register(tool)
    events: list[AuditEvent] = []
    loop = VoiceLoop(
        settings=Settings(dry_run=dry_run),
        local_stt=None,
        gate=None,
        router=None,
        risk=None,
        confirm=StubConfirm(),
        registry=registry,
        policy_decide=stub_policy(tier),
        journal=journal,
        audit=events.append,
        llm=FakeLLM(turns=[LLMTurn(tool_calls=tuple(calls))]),
    )
    out, stdin = PipeOut(), PipeIn()
    bridge = Bridge(
        loop=loop,
        stdin=stdin,
        stdout=out,
        missing=["nothing is real here"],
        confirm_timeout_s=confirm_timeout_s,
    )
    return Harness(bridge=bridge, loop=loop, out=out, stdin=stdin, tool=tool, events=events)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_the_writer_flushes_every_batch():
    """Python's stdout is FULLY BUFFERED on a pipe. `-u` is passed and
    PYTHONUNBUFFERED is set and we flush anyway: forgetting this produces a
    dock that hangs forever waiting for `ready`."""
    out = PipeOut()
    writer = Writer(out)
    writer.start()
    try:
        assert writer.put(protocol.event("ready", daa="0.1.0"))
        out.wait(lambda fs: len(fs) == 1)
        assert out.flushes >= 1
    finally:
        writer.close()


def test_the_writer_drops_the_chatty_kinds_first_and_never_the_decisions():
    out = PipeOut()
    writer = Writer(out, limit=4)  # not started: nothing drains
    for kind in ("heard", "spoke", "execution", "refused"):
        assert writer.put(protocol.event(Method.AUDIT, kind=kind), kind=kind)
    # Full. The next two decisions evict the two chatty rows, in order.
    assert writer.put(protocol.event(Method.AUDIT, kind="undo"), kind="undo")
    assert writer.put(protocol.event(Method.AUDIT, kind="judgment"), kind="judgment")
    writer.start()
    out.wait(lambda fs: len(fs) == 4)
    kinds = [f.params["kind"] for f in out.frames()]
    assert kinds == ["execution", "refused", "undo", "judgment"]
    assert writer.dropped == 2
    writer.close()


def test_a_queue_full_of_decisions_drops_the_newcomer_rather_than_blocking():
    """Never block the loop thread. The disk log has the row either way; the
    wire copy is a mirror."""
    out = PipeOut()
    writer = Writer(out, limit=2)
    assert writer.put(protocol.event(Method.AUDIT, kind="execution"), kind="execution")
    assert writer.put(protocol.event(Method.AUDIT, kind="execution"), kind="execution")
    done = threading.Event()

    def push() -> None:
        assert writer.put(protocol.event(Method.AUDIT, kind="undo"), kind="undo") is False
        done.set()

    threading.Thread(target=push, daemon=True).start()
    assert done.wait(TIMEOUT), "put() blocked the calling thread"
    assert writer.dropped == 1


def test_a_dead_pipe_closes_the_writer_rather_than_raising_into_the_loop():
    out = PipeOut()
    out.explode = BrokenPipeError("the dock quit")
    writer = Writer(out)
    writer.start()
    writer.put(protocol.event("ready"))
    for _ in range(200):
        if not writer.alive:
            break
        threading.Event().wait(0.01)
    assert not writer.alive
    assert writer.put(protocol.event("ready")) is False


# ---------------------------------------------------------------------------
# The mic seam
# ---------------------------------------------------------------------------


def test_the_mic_hands_the_loop_opaque_bytes_not_a_string():
    """`AudioChunk.pcm` is opaque by contract, so the loop can never read the
    mic without going through a Transcriber."""
    mic = BridgeMic()
    mic.push("move my screenshots", complete=False, started_at=12.5)
    chunk = next(mic.segments())
    assert chunk.pcm == b"move my screenshots"
    assert chunk.complete is False
    assert chunk.started_at == 12.5


def test_closing_the_mic_stops_the_loop_rather_than_draining_a_backlog():
    """The dock has quit. Working through the speech it queued before it went
    is not catching up, it is acting on instructions nobody is watching."""
    mic = BridgeMic()
    mic.push("one")
    mic.push("two")
    mic.close()
    assert list(mic.segments()) == []


def test_deferred_work_runs_on_the_turn_thread_between_turns():
    mic = BridgeMic()
    ran: list[str] = []
    mic.defer(lambda: ran.append("undo"))
    mic.push("hello")
    assert next(mic.segments()).pcm == b"hello"
    assert ran == ["undo"]


def test_deferred_work_is_held_while_a_confirmation_is_in_flight():
    """`_next_reply` pumps this same generator. Running the undo button's work
    there would consume the answer the user is giving to the question in front
    of them -- `VoiceLoop._drain_allowed` is the loop's own name for this."""
    confirming = {"yes": True}
    mic = BridgeMic(work_allowed=lambda: not confirming["yes"])
    ran: list[str] = []
    mic.defer(lambda: ran.append("undo"))
    mic.push("yes")
    segments = mic.segments()

    assert next(segments).pcm == b"yes"
    assert ran == [], "the undo ran while a confirmation was open"

    confirming["yes"] = False
    mic.push("and now this")
    assert next(segments).pcm == b"and now this"
    assert ran == ["undo"], "the held work never drained"


def test_work_that_blows_up_does_not_end_the_mic():
    mic = BridgeMic()

    def boom() -> None:
        raise RuntimeError("undo exploded")

    mic.defer(boom)
    mic.push("still here")
    assert next(mic.segments()).pcm == b"still here"


def test_onset_reaches_the_listener_and_survives_one_that_raises():
    mic = BridgeMic()
    hits: list[int] = []
    mic.set_speech_listener(lambda: hits.append(1))
    mic.onset()
    assert hits == [1]

    def boom() -> None:
        raise OSError("the listener is gone")

    mic.set_speech_listener(boom)
    mic.onset()  # must not raise


def test_escape_drops_queued_audio_and_keeps_deferred_work():
    mic = BridgeMic()
    ran: list[str] = []
    mic.push("one")
    mic.defer(lambda: ran.append("undo"))
    mic.push("two")
    assert mic.drop_pending() == 2
    mic.push("after")
    assert next(mic.segments()).pcm == b"after"
    assert ran == ["undo"]


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def a_console(**over: Any) -> tuple[DockConsole, list[Any], list[tuple[str, dict]]]:
    sent: list[Any] = []
    audited: list[tuple[str, dict]] = []
    kwargs: dict[str, Any] = {
        "send": lambda token, payload: (sent.append((token, payload)), True)[1],
        "emit": lambda method, params: sent.append((method, params)),
        "audit": lambda kind, **p: audited.append((kind, p)),
        "alive": lambda: True,
        "phrase": lambda action: "run this script",
        "script_keys": ("script",),
        "dry_run": lambda: True,
        "timeout_s": 30.0,
    }
    kwargs.update(over)
    return DockConsole(**kwargs), sent, audited


ACTION = ResolvedAction(tool="run_applescript", args={"script": SCRIPT_BODY}, verb="run")
DISPOSITION = Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x", assessment=ASSESSMENT)


def answer_in_background(console: DockConsole, sent: list[Any], **kw: Any) -> threading.Thread:
    """Answer the card as soon as it appears. Blocks on the card, not a clock."""
    ready = threading.Event()

    def reply() -> None:
        while not ready.is_set():
            if sent and isinstance(sent[-1][0], str) and sent[-1][0].startswith("cf_"):
                break
            threading.Event().wait(0.001)
        console.resolve(sent[-1][0], **kw)

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    return thread


def test_a_held_approval_returns_true_and_the_token_is_single_use():
    console, sent, audited = a_console()
    answer_in_background(console, sent, granted=True, reason="approved")
    assert console.present(ACTION, DISPOSITION) is True
    token = sent[0][0]
    assert token.startswith("cf_") and len(token) > 8
    # Spent. A replayed answer is dropped.
    assert console.resolve(token, granted=True, reason="approved") is False
    assert ("confirm_card", {"tool": "run_applescript", "card_id": token,
                             "granted": True, "reason": "approved"}) in audited


def test_a_refusal_is_a_refusal_and_says_which_kind():
    """A card that timed out is NOT the same event as a user who said no, and
    `daa doctor` and the History window should be able to tell them apart."""
    for reason in ("cancelled", "escaped", "brain stopped", "unreadable request"):
        console, sent, audited = a_console()
        answer_in_background(console, sent, granted=False, reason=reason)
        assert console.present(ACTION, DISPOSITION) is False
        assert audited[-1][1]["reason"] == reason
        assert audited[-1][1]["granted"] is False


def test_a_card_nobody_answers_expires_closed_and_is_withdrawn():
    """`console.ask()` on a terminal blocks forever; `present()` must not. An
    approval you walked away from is not an approval."""
    console, sent, audited = a_console(timeout_s=0.05)
    assert console.present(ACTION, DISPOSITION) is False
    assert audited[-1][1]["reason"] == "timeout"
    # And the card is taken off the screen rather than left counting down.
    assert (Method.CONFIRM_CANCEL, {"id": sent[0][0], "reason": "timeout"}) in sent


def test_a_dead_dock_is_an_immediate_refusal():
    console, _sent, audited = a_console(send=lambda token, payload: False)
    assert console.present(ACTION, DISPOSITION) is False
    assert audited[-1][1]["reason"] == "brain stopped"


def test_available_follows_the_peer():
    console, _sent, _audited = a_console(alive=lambda: False)
    assert console.available() is False


def test_two_cards_in_flight_get_their_own_tokens_and_their_own_answers():
    """`present()` blocks on a queue keyed by ITS token, never on the segment
    stream, so it cannot steal a reply meant for the foreground turn."""
    console, sent, _audited = a_console()
    results: dict[int, bool] = {}
    started = threading.Barrier(3, timeout=TIMEOUT)

    def ask(n: int) -> None:
        started.wait()
        results[n] = console.present(ACTION, DISPOSITION)

    threads = [threading.Thread(target=ask, args=(n,), daemon=True) for n in (0, 1)]
    for t in threads:
        t.start()
    started.wait()
    tokens: set[str] = set()
    while len(tokens) < 2:
        tokens = {t for t, _p in sent if isinstance(t, str) and t.startswith("cf_")}
    first, second = sorted(tokens)
    console.resolve(first, granted=True, reason="approved")
    console.resolve(second, granted=False, reason="cancelled")
    for t in threads:
        t.join(timeout=TIMEOUT)
    assert sorted(results.values()) == [False, True]


def test_the_console_has_no_write_or_ask():
    """`_confirm_visual` picks its branch on the presence of `present`. A
    console with all three would be a console whose behaviour depended on the
    order of two getattrs."""
    console, _sent, _audited = a_console()
    assert hasattr(console, "present")
    assert not hasattr(console, "write")
    assert not hasattr(console, "ask")


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_hello_is_answered_and_ready_follows_immediately():
    h = harness()
    h.start()
    try:
        h.stdin.push(protocol.request("h1", Method.HELLO, proto=1, app="0.1.0"))
        frames = h.out.wait(lambda fs: any(getattr(f, "method", "") == Method.READY for f in fs))
        res = next(f for f in frames if isinstance(f, Response))
        assert res.id == "h1" and res.ok and res.params["proto"] == 1
        ready = next(f for f in frames if getattr(f, "method", "") == Method.READY)
        # `dryRun` MUST be present: the dock defaults a missing one to true
        # and shows the pill, and relying on that is how a live build looks safe.
        assert ready.params["dryRun"] is False
        assert ready.params["providers"]["mic"] == "bridge"
        assert ready.params["providers"]["jev"] == "fake"
        assert {"name": "run_applescript", "floor": "CONFIRM_VISUAL"} in ready.params["tools"]
        assert ready.params["missing"] == ["nothing is real here"]
        assert h.out.wait_for_method(Method.STATE).params["phase"] == "idle"
    finally:
        h.stop()


def test_a_dock_speaking_another_protocol_is_refused_rather_than_guessed_at():
    h = harness()
    h.start()
    try:
        h.stdin.push(protocol.request("h1", Method.HELLO, proto=9))
        frames = h.out.wait(lambda fs: any(isinstance(f, Response) for f in fs))
        res = next(f for f in frames if isinstance(f, Response))
        assert res.ok is False and res.code == "proto"
        assert Method.READY not in h.out.methods()
    finally:
        h.stop()


def test_an_unimplemented_method_is_answered_rather_than_ignored():
    """The dock times a request out at 30s and shows the failure; a visible
    "daa does not do that" beats half a minute of nothing."""
    h = harness()
    h.start()
    try:
        h.stdin.push(protocol.request("r1", "something.new"))
        frames = h.out.wait(lambda fs: any(isinstance(f, Response) for f in fs))
        res = next(f for f in frames if isinstance(f, Response))
        assert res.ok is False and res.code == "unknown-method"
    finally:
        h.stop()


@pytest.mark.parametrize(
    "junk",
    ["", "   ", "not json", "[1,2]", '{"t":"weird"}', '{"t":"req","m":"x"}', "\x00\x01"],
)
def test_an_unparseable_line_is_skipped_and_the_reader_lives(junk: str):
    h = harness()
    h.start()
    try:
        h.stdin.push_raw(junk)
        h.stdin.push(protocol.request("h1", Method.HELLO, proto=1))
        h.out.wait_for_method(Method.READY)  # the reader survived the junk
    finally:
        h.stop()


def test_a_handler_that_blows_up_does_not_end_the_reader():
    h = harness()
    h.bridge.ready = lambda: (_ for _ in ()).throw(RuntimeError("ready exploded"))
    h.start()
    try:
        h.stdin.push(protocol.request("h1", Method.HELLO, proto=1))
        h.stdin.push(protocol.request("r2", "nope"))
        frames = h.out.wait(lambda fs: sum(isinstance(f, Response) for f in fs) >= 2)
        assert [f.id for f in frames if isinstance(f, Response)] == ["h1", "r2"]
    finally:
        h.stop()


def test_a_response_with_an_unknown_token_is_dropped_and_audited():
    """The model cannot mint a token; neither can a forged undo.jsonl row."""
    h = harness()
    h.start()
    try:
        h.hello()
        h.stdin.push(protocol.response("cf_forged", True, {"granted": True, "reason": "approved"}))
        deadline = threading.Event()
        while not deadline.wait(0.005):
            errors = [
                e
                for e in h.events
                if e.kind == "error" and e.payload.get("where") == "confirm"
            ]
            if errors:
                break
        assert "cf_forged" in errors[0].payload["error"]
        assert h.tool.runs == [], "a forged token ran something"
    finally:
        h.stop()


def test_always_on_answers_with_the_state_that_actually_took_effect():
    """The dock reverts its switch if this disagrees, because a control that
    lies about a hot microphone is the worst control on the panel."""
    h = harness()
    h.start()
    try:
        h.hello()
        h.stdin.push(protocol.request("a1", Method.CONTROL_ALWAYS_ON, on=True))
        frames = h.out.wait(
            lambda fs: any(isinstance(f, Response) and f.id == "a1" for f in fs)
        )
        res = next(f for f in frames if isinstance(f, Response) and f.id == "a1")
        assert res.params["on"] is True
        assert h.loop.settings.always_on is True
    finally:
        h.stop()


def test_undo_with_an_empty_journal_says_so_rather_than_asking():
    h = harness(journal=StubJournal())
    h.start()
    try:
        h.hello()
        h.stdin.push(protocol.request("u1", Method.UNDO_LAST))
        frames = h.out.wait(
            lambda fs: any(isinstance(f, Response) and f.id == "u1" for f in fs)
        )
        res = next(f for f in frames if isinstance(f, Response) and f.id == "u1")
        assert res.ok and res.params["ok"] is False
        assert "nothing to undo" in res.params["summary"].lower()
    finally:
        h.stop()


def test_undo_with_a_row_answers_that_daa_is_asking_not_that_it_reversed():
    """Undo never runs below CONFIRM_VOICE -- the journal is a file any local
    process can append to -- so the honest immediate answer is that daa is
    ASKING. Say so, or the click looks broken."""
    journal = StubJournal(entries=[object()])
    h = harness(journal=journal)
    h.start()
    try:
        h.hello()
        h.stdin.push(protocol.request("u1", Method.UNDO_LAST))
        frames = h.out.wait(
            lambda fs: any(isinstance(f, Response) and f.id == "u1" for f in fs)
        )
        res = next(f for f in frames if isinstance(f, Response) and f.id == "u1")
        assert res.params["ok"] is False
        assert res.params["summary"] == "Asking out loud before I reverse that."
    finally:
        h.stop()


def test_escape_forgets_the_half_sentence():
    h = harness()
    h.start()
    try:
        h.hello()
        h.loop._pending = "move the screen"
        h.stdin.push(protocol.event(Method.CONTROL_CANCEL))
        h.out.wait(
            lambda fs: any(
                getattr(f, "method", "") == Method.AUDIT
                and f.params.get("kind") == "abandoned"
                for f in fs
            )
        )
        assert h.loop._pending == ""
    finally:
        h.stop()


def test_shutdown_is_answered_and_then_the_bridge_stops():
    h = harness()
    h.start()
    try:
        h.hello()
        h.stdin.push(protocol.request("s1", Method.SHUTDOWN))
        h.out.wait(lambda fs: any(isinstance(f, Response) and f.id == "s1" for f in fs))
        assert h.thread is not None
        h.thread.join(timeout=TIMEOUT)
        assert not h.thread.is_alive()
    finally:
        h.stdin.close()


def test_closing_stdin_refuses_every_card_on_screen():
    """Child death, dock quit and `confirm.cancel` all resolve to
    granted:false. There is no consent recorded for a question nobody is left
    to answer."""
    h = harness()
    h.start()
    h.hello()
    result: dict[str, Any] = {}
    raised = threading.Event()

    def present() -> None:
        raised.set()
        result["granted"] = h.bridge.console.present(ACTION, DISPOSITION)

    thread = threading.Thread(target=present, daemon=True)
    thread.start()
    assert raised.wait(TIMEOUT)
    h.out.wait(lambda fs: any(isinstance(f, Request) for f in fs))
    h.stop()
    thread.join(timeout=TIMEOUT)
    assert result["granted"] is False


# ---------------------------------------------------------------------------
# fd 1
# ---------------------------------------------------------------------------


STDOUT_THEFT = r"""
import sys
from daa.ui import steal_stdout

real = steal_stdout()

# Everything from here on is noise on fd 1 unless the theft worked. Import the
# WHOLE tree: an import-time print in a sibling, or in a dependency of one, is
# exactly the failure this test exists for.
print("a stray print before the imports")
import importlib, pkgutil
import daa
for mod in pkgutil.walk_packages(daa.__path__, "daa."):
    try:
        importlib.import_module(mod.name)
    except Exception as exc:
        print(f"could not import {mod.name}: {exc}")
print("a stray print after the imports")
sys.stdout.write("and one straight to the object\n")
sys.stdout.flush()

from daa.ui import protocol
real.write(protocol.encode(protocol.event("ready", daa="0.1.0")))
real.write(protocol.encode(protocol.event("state", phase="idle")))
real.flush()
"""


def test_fd_1_sees_nothing_but_frames_even_with_the_whole_tree_imported():
    """fd 1 is the protocol and nothing else. One stray `print()` corrupts the
    stream and the symptom is a dock that silently stops updating."""
    proc = subprocess.run(
        [sys.executable, "-c", STDOUT_THEFT],
        capture_output=True,
        timeout=180,
        check=False,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr.decode()
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    frames = [protocol.decode(ln) for ln in lines]  # raises if fd 1 was corrupted
    assert [f.method for f in frames] == ["ready", "state"]
    # And the noise went somewhere it is not lost.
    assert b"a stray print before the imports" in proc.stderr
    assert b"a stray print after the imports" in proc.stderr
    assert b"and one straight to the object" in proc.stderr


def test_the_bridge_never_spawns_and_never_opens_an_audio_device():
    """No `setsid`, no double fork, no daemonize, no disclaiming: the bridge
    stays an ordinary child of the app or every TCC grant moves back onto the
    interpreter, which is the entire reason this architecture exists. And
    Swift owns the microphone, the VAD and the recogniser, so Python opens no
    audio device at all.

    Read off the AST rather than grepped, so the prose above can name the
    things it forbids without tripping its own rule.
    """
    import ast
    import pathlib

    forbidden_modules = {"subprocess", "multiprocessing", "sounddevice", "pty", "asyncio"}
    forbidden_calls = {"fork", "forkpty", "setsid", "execv", "execvp", "spawnv", "posix_spawn"}
    src = pathlib.Path(__file__).resolve().parent.parent / "src/daa/ui"

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden_modules, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden_modules, f"{path.name} imports from {node.module}"
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in forbidden_calls, f"{path.name} calls {name}()"


def test_the_bridge_leaves_the_loops_own_cli_paths_alone():
    """`daa listen`, `daa say`, `daa undo` and `daa doctor` must be
    bit-for-bit unchanged: the bridge is a fifth subcommand, not a rewiring."""
    from daa.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["bridge"])
    assert args.command == "bridge"
    for command in ("listen", "say", "doctor", "undo"):
        assert parser.parse_args([command, *(["x"] if command == "say" else [])])


def test_a_scoped_grant_asked_for_on_the_dock_is_refused_not_assumed():
    """`_grant_typed` renders its card with `console.write` / `console.ask`,
    which `DockConsole` deliberately does not have -- and the grant card is
    not part of the dock protocol yet, so there is no card for it to raise.

    The behaviour that matters is which way it fails. An AttributeError on
    this path must mean "no grant", never "grant assumed": a scoped bargain
    nobody was shown is the one thing worse than being asked twice.
    """
    from daa.contracts import Budget, GrantScope

    h = harness()
    h.start()
    try:
        h.hello()
        grant = h.loop.request_grant(
            goal="tidy the Downloads folder",
            scope=GrantScope(tools=("run_applescript",)),
            budget=Budget(steps=3),
            ceiling=RiskTier.CONFIRM_VISUAL,
            via="visual",
        )
        assert grant is None, "the dock was handed a grant it never displayed"
        assert h.loop.active_grant is None
        assert any(
            e.kind == "error" and e.payload.get("where") == "console" for e in h.events
        )
    finally:
        h.stop()
