"""The local STT tier: faster-whisper, on device, with the network refused.

Three groups:

1. Contract tests that need no model: model resolution never downloads, the
   offline guard refuses sockets and DNS on the decoding thread, `partial` is
   read off the punctuation, and heavy imports stay lazy.
2. Real-model tests, skipped when no whisper model is on disk. Speech comes
   from `say` rendered straight to 16 kHz WAV -- studio-clean, so these prove
   wiring, not accuracy in a real room.
3. End to end: a FILE-backed AudioSource pushed through the real EnergyVAD,
   the real LocalTranscriber, the real AddressGate and the rest of the loop
   with fake providers -- and once more through `daa listen` itself.

The microphone is never opened in this file. The one thing left unexercised
is `SoundDeviceMic._blocks`, the PortAudio read, because opening it raises a
TCC prompt.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import wave
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from daa.config import Settings
from daa.contracts import (
    Disposition,
    ResolvedAction,
    RiskAssessment,
    RiskTier,
    ToolResult,
    ToolSpec,
)
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.mic import AudioChunk, EnergyVAD, FakeMic, SpeechListener
from daa.voice.stt import (
    DEFAULT_LOCAL_MODEL,
    FakeTranscriber,
    LocalTranscriber,
    NetworkForbidden,
    STTUnavailable,
    build_local,
    is_sentence_final,
    no_network,
    resolve_local_model,
)

# Resolved at import, before any test repoints HOME or the HF cache.
MODEL_DIR = resolve_local_model()
HAVE_MODEL = LocalTranscriber.available()
HAVE_SAY = sys.platform == "darwin" and shutil.which("say") is not None
needs_model = pytest.mark.skipif(not HAVE_MODEL, reason="no local whisper model on disk")
needs_speech = pytest.mark.skipif(
    not (HAVE_MODEL and HAVE_SAY), reason="needs a local whisper model and macOS `say`"
)


# ---------------------------------------------------------------------------
# 1. contract, no model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "final"),
    [
        ("Open Safari.", True),
        ("What's on my calendar tomorrow?", True),
        ("Stop!", True),
        ('He said "go."', True),
        ("move the screenshots into", False),
        ("so I was thinking,", False),
        ("and then...", False),
        ("and then…", False),
        ("", False),
    ],
)
def test_sentence_final_is_read_off_the_last_token(text, final):
    assert is_sentence_final(text) is final


def _fake_model_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "model.bin").write_bytes(b"")
    return root


def test_resolves_an_explicit_model_directory(tmp_path):
    d = _fake_model_dir(tmp_path / "mine")
    assert resolve_local_model(str(d)) == d


def test_resolves_the_hf_cache_snapshot_named_by_refs_main(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    repo = tmp_path / "models--Systran--faster-whisper-tiny.en"
    _fake_model_dir(repo / "snapshots" / "old")
    new = _fake_model_dir(repo / "snapshots" / "abc123")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123\n")
    assert resolve_local_model("tiny.en") == new


def test_env_var_picks_the_model(tmp_path, monkeypatch):
    d = _fake_model_dir(tmp_path / "chosen")
    monkeypatch.setenv("DAA_STT_LOCAL_MODEL", str(d))
    assert resolve_local_model() == d


def test_a_missing_model_is_unavailable_not_a_download(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.delenv("DAA_STT_LOCAL_MODEL", raising=False)
    assert resolve_local_model() is None
    assert LocalTranscriber.available() is False
    # build_local degrades to the echoing fake rather than returning None.
    assert isinstance(build_local(Settings()), FakeTranscriber)
    with no_network(), pytest.raises(STTUnavailable, match="no local whisper model"):
        LocalTranscriber().transcribe(AudioChunk(pcm=b"\x00\x00" * 1600))


def test_build_local_honours_the_flag(monkeypatch):
    monkeypatch.setattr(LocalTranscriber, "available", staticmethod(lambda *a: True))
    assert isinstance(build_local(Settings(stt_local=True)), LocalTranscriber)
    assert isinstance(build_local(Settings(stt_local=False)), FakeTranscriber)


def test_importing_stt_loads_no_speech_stack():
    code = (
        "import sys, daa, daa.voice.stt as s, daa.voice.mic;"
        "s.LocalTranscriber.available();"
        "heavy=[m for m in ('numpy','faster_whisper','ctranslate2','huggingface_hub',"
        "'onnxruntime','av','torch') if m in sys.modules];"
        "print(','.join(heavy))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=60)
    assert out.stdout.strip() == "", f"imported at module scope: {out.stdout.strip()}"


def test_fake_mic_text_is_decoded_by_the_fake_not_the_model():
    """build_loop hands tests a real LocalTranscriber whenever a model is on
    disk. A FakeMic payload is text, and must never be fed to whisper as noise
    -- nor pay for a model load."""
    stt = LocalTranscriber(model_path="/nonexistent")
    chunk = next(iter(FakeMic(utterances=["hey daa open safari"]).segments()))
    heard = stt.transcribe(chunk)
    assert heard.text == "hey daa open safari"
    assert heard.source == "local"
    assert stt._model is None


def test_unknown_encodings_are_refused():
    with pytest.raises(STTUnavailable, match="opus"):
        LocalTranscriber().transcribe(AudioChunk(pcm=b"x", encoding="opus"))


# -- the offline guard ------------------------------------------------------


def _loopback_server() -> tuple[socket.socket, tuple[str, int]]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    return server, server.getsockname()


def test_guard_refuses_connect_even_to_loopback():
    # conftest lets loopback through; the guard does not. Nothing leaves.
    server, addr = _loopback_server()
    try:
        with no_network(), pytest.raises(NetworkForbidden):
            socket.create_connection(addr, timeout=0.2)
        with pytest.raises(TimeoutError):
            server.accept()
    finally:
        server.close()


def test_guard_refuses_dns():
    # A lookup alone says what we were about to fetch.
    with no_network(), pytest.raises(NetworkForbidden):
        socket.getaddrinfo("example.com", 443)


def test_guard_is_scoped_to_its_thread_and_its_block():
    server, addr = _loopback_server()
    errors: list[BaseException] = []

    def other_thread() -> None:
        try:
            socket.create_connection(addr, timeout=0.5).close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        with no_network():
            with no_network():
                pass
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
            # Still guarded after the nested block exits.
            with pytest.raises(NetworkForbidden):
                socket.create_connection(addr, timeout=0.2)
        assert errors == [], "a tool thread was blocked by the STT guard"
        socket.create_connection(addr, timeout=0.5).close()  # guard released
    finally:
        server.close()


@dataclass
class _Seg:
    text: str
    no_speech_prob: float = 0.05
    avg_logprob: float = -0.2


@dataclass
class _FakeWhisper:
    segments: list[_Seg] = field(default_factory=list)
    during: Any = None
    calls: int = 0

    def transcribe(self, audio: Any, **kw: Any) -> tuple[Iterator[_Seg], None]:
        self.calls += 1
        if self.during is not None:
            self.during()
        return iter(self.segments), None


def _with_model(model: _FakeWhisper) -> LocalTranscriber:
    stt = LocalTranscriber()
    stt._model = model
    return stt


PCM = b"\x10\x00" * 16_000


@needs_model  # numpy ships with faster-whisper
def test_a_decoder_that_reaches_for_the_network_is_stopped_before_it_connects():
    server, addr = _loopback_server()
    try:
        model = _FakeWhisper(
            segments=[_Seg("hi.")],
            during=lambda: socket.create_connection(addr, timeout=0.2),
        )
        with pytest.raises(STTUnavailable, match="network"):
            _with_model(model).transcribe(AudioChunk(pcm=PCM))
        with pytest.raises(TimeoutError):
            server.accept()
    finally:
        server.close()


@needs_model
@pytest.mark.parametrize(
    ("segs", "complete", "text", "partial"),
    [
        ([_Seg(" Open Safari.")], True, "Open Safari.", False),
        ([_Seg(" move the screenshots into")], True, "move the screenshots into", True),
        # The VAD cut on its timeout: partial whatever the punctuation says.
        ([_Seg(" Move the screenshots.")], False, "Move the screenshots.", True),
        # Whisper's own "not speech" flag drops the hallucinated word.
        ([_Seg(" you", no_speech_prob=0.83)], True, "", False),
        ([_Seg(" Open"), _Seg(" Safari.")], True, "Open Safari.", False),
    ],
)
def test_text_partial_and_no_speech_filter(segs, complete, text, partial):
    heard = _with_model(_FakeWhisper(segments=segs)).transcribe(
        AudioChunk(pcm=PCM, complete=complete)
    )
    assert (heard.text, heard.partial, heard.source) == (text, partial, "local")
    assert 0.0 <= heard.confidence <= 1.0


# ---------------------------------------------------------------------------
# 2. the real model
# ---------------------------------------------------------------------------


def _say(tmp: Path, name: str, text: str) -> Path:
    out = tmp / f"{name}.wav"
    subprocess.run(
        ["say", "-v", "Samantha", "-r", "175", "--file-format=WAVE",
         "--data-format=LEI16@16000", "-o", str(out), text],
        check=True, timeout=60,
    )
    return out


def _pcm(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)
        return w.readframes(w.getnframes())


UTTERANCES = {
    "wake": "hey daa open safari",
    "overheard": "so I think we should ship it on Friday",
    "weather": "hey daa what's the weather like",
}


@pytest.fixture(scope="module")
def speech(tmp_path_factory) -> dict[str, bytes]:
    if not HAVE_SAY:
        pytest.skip("macOS `say` not available")
    tmp = tmp_path_factory.mktemp("speech")
    return {k: _pcm(_say(tmp, k, v)) for k, v in UTTERANCES.items()}


@pytest.fixture(scope="module")
def local() -> LocalTranscriber:
    stt = LocalTranscriber()
    stt.warm()
    return stt


def _norm(text: str) -> str:
    return " ".join("".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split())


@needs_speech
def test_real_model_transcribes_a_wake_utterance(speech, local):
    heard = local.transcribe(AudioChunk(pcm=speech["wake"]))
    assert "open safari" in _norm(heard.text)
    assert heard.source == "local" and heard.partial is False
    # Loose on purpose: the contract (<300ms for 3s) is measured by the eval
    # harness, not asserted in a shared CI box.
    assert heard.latency_ms < 2_000


@needs_speech
def test_real_model_returns_nothing_for_silence(local):
    assert local.transcribe(AudioChunk(pcm=b"\x00\x00" * 48_000)).empty


@needs_speech
def test_real_model_loads_and_decodes_with_every_socket_refused(speech, monkeypatch):
    """No network, ever -- including LOAD. Stricter than conftest's guard:
    loopback and DNS are refused too, and the transcriber is fresh so the
    model load happens under it."""

    def refuse(*a: Any, **kw: Any) -> Any:
        raise AssertionError(f"local STT touched the network: {a!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    heard = LocalTranscriber().transcribe(AudioChunk(pcm=speech["weather"]))
    assert "weather" in _norm(heard.text)


# ---------------------------------------------------------------------------
# 3. end to end, from a file
# ---------------------------------------------------------------------------


class WavFileMic:
    """A file-backed AudioSource: PCM split into 30ms blocks and pushed through
    the SAME EnergyVAD the microphone uses. Only the device read differs."""

    def __init__(self, pcm: bytes, *, vad: EnergyVAD | None = None) -> None:
        self.vad = vad or EnergyVAD()
        self._pcm = pcm
        self._listener: SpeechListener | None = None
        self._closed = False
        self.onsets = 0

    def _blocks(self) -> Iterator[bytes]:
        step = self.vad.block_frames * 2
        for i in range(0, len(self._pcm) - step + 1, step):
            yield self._pcm[i:i + step]

    def _onset(self) -> None:
        self.onsets += 1
        if self._listener is not None:
            self._listener()

    def segments(self) -> Iterator[AudioChunk]:
        yield from self.vad.segment(self._blocks(), on_onset=self._onset,
                                    closed=lambda: self._closed)

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        self._listener = listener

    def close(self) -> None:
        self._closed = True


def _stream(speech: Mapping[str, bytes], order: Sequence[str]) -> bytes:
    gap = b"\x00\x00" * 16_000  # 1s of room tone between speakers
    return gap + b"".join(speech[k] + gap for k in order)


class TranscriptJev:
    """A Jev provider that answers the gate from the TRANSCRIPT it is shown:
    addressed iff it heard the wake word. That makes the gate's decision a
    function of what whisper actually produced from the audio."""

    def __init__(self) -> None:
        self.utterances: list[str] = []

    def ask(self, state: Mapping[str, Any], questions: Mapping[str, Any], *,
            timeout_s: float = 2.0) -> Any:
        from daa.jev.client import FakeJev

        heard = str(state["utterance"])
        self.utterances.append(heard)
        # Keyed on "hey", not on the wake word's spelling: tiny.en renders
        # "daa" as "DAO" or "da" depending on what precedes it (Apple's engine
        # says "KDA"). How the real gate copes with that is Jev's problem.
        woke = "hey" in _norm(heard).split()
        return FakeJev({"addressed": 0.97 if woke else 0.02, "end_of_turn": 0.95,
                        "needs_planner": 0.05, "stop": 0.01}).ask(state, questions)


OPEN = ToolSpec(name="open_app", description="Open an application",
                params={"name": {"type": "string"}}, floor=RiskTier.SILENT,
                activation_hint="opening apps")


@dataclass
class SpyTool:
    spec: ToolSpec = OPEN
    runs: list[ResolvedAction] = field(default_factory=list)
    resolves: list[dict[str, Any]] = field(default_factory=list)

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        self.resolves.append(dict(kwargs))
        return ResolvedAction(tool=self.spec.name, args=kwargs,
                              targets=tuple(str(v) for v in kwargs.values()), explicit=True)

    def run(self, action: ResolvedAction) -> ToolResult:
        self.runs.append(action)
        return ToolResult(ok=True, summary="Opened Safari.")


class Registry:
    def __init__(self, tool: SpyTool) -> None:
        self.tool = tool

    def get(self, name: str) -> SpyTool:
        if name != self.tool.spec.name:
            raise KeyError(name)
        return self.tool

    def specs(self) -> list[ToolSpec]:
        return [self.tool.spec]


def _policy(action: ResolvedAction, spec: ToolSpec, assessment: Any, settings: Any):
    return Disposition(tier=RiskTier.SILENT, reason="test", assessment=assessment)


class _Risk:
    def assess(self, action: Any, utterance: str, history: Any) -> RiskAssessment:
        return RiskAssessment(blast_radius=0.1, unrecoverable=0.0, explicitly_requested=1.0,
                              target_confidence="certain", confidence=0.95, synthetic=True)


@needs_speech
def test_file_audio_through_vad_whisper_gate_and_loop(speech, local):
    from daa.jev.gate import AddressGate
    from daa.voice.loop import VoiceLoop

    jev = TranscriptJev()
    tool = SpyTool()
    llm = FakeLLM(turns=[LLMTurn(tool_calls=(ToolCall("open_app", {"name": "Safari"}),))])
    events: list[Any] = []
    mic = WavFileMic(_stream(speech, ["overheard", "wake"]))
    loop = VoiceLoop(
        settings=Settings(dry_run=True, always_on=False),
        mic=mic,
        local_stt=local,
        cloud_stt=None,
        gate=AddressGate(jev, Settings(dry_run=True)),
        router=None,
        risk=_Risk(),
        confirm=None,
        registry=Registry(tool),
        policy_decide=_policy,
        journal=None,
        audit=events.append,
        llm=llm,
    )
    outcomes = loop.run()

    # The VAD found exactly the two utterances in the file.
    assert mic.onsets == 2 and len(outcomes) == 2
    # The gate was shown what whisper heard -- real audio -> real text.
    assert len(jev.utterances) == 2
    assert "ship it on friday" in _norm(jev.utterances[0])
    assert "open safari" in _norm(jev.utterances[1])

    overheard, wake = outcomes
    assert overheard.woke is False and overheard.dropped_reason == "not addressed"
    dropped = [e for e in events if getattr(e, "kind", "") == "dropped"]
    assert dropped and all("ship" not in str(e.payload).lower() for e in dropped), (
        "unaddressed speech was written to the audit log"
    )

    assert wake.woke is True
    assert len(llm.seen) == 1, "the model saw more (or less) than the woken utterance"
    # dry_run: the loop resolved the action, cleared policy, and stopped short
    # of the tool -- which is the loop's decision, not the transcriber's.
    assert tool.resolves == [{"name": "Safari"}]
    assert [d.tier for d in wake.dispositions] == [RiskTier.SILENT]
    assert wake.results and wake.results[0].ok and "Safari" in wake.results[0].summary
    assert tool.runs == []


@needs_speech
def test_daa_listen_end_to_end_from_a_file(speech, monkeypatch, tmp_path, capsys):
    """`daa listen` as shipped: cli -> build_mic -> build_loop -> build_local.
    Only build_mic is swapped (for the file); every other provider is the one
    `daa listen` builds on a keyless machine -- FakeJev, FakeLLM."""
    from daa import cli
    from daa.voice import mic as mic_mod

    monkeypatch.setenv("DAA_STT_LOCAL_MODEL", str(MODEL_DIR))
    monkeypatch.setenv("HOME", str(tmp_path))  # audit/undo land here, not in ~
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: Settings()))
    file_mic = WavFileMic(_stream(speech, ["wake", "weather"]))
    monkeypatch.setattr(mic_mod, "build_mic", lambda settings=None, **kw: file_mic)

    built: list[Any] = []
    real_build = cli._build

    def capture(*a: Any, **kw: Any) -> Any:
        loop = real_build(*a, **kw)
        built.append(loop)
        return loop

    monkeypatch.setattr(cli, "_build", capture)
    assert cli.main(["listen", "--max-segments", "2"]) == 0

    (loop,) = built
    assert isinstance(loop.local_stt, LocalTranscriber), "daa listen did not get local STT"
    heard = [_norm(o.utterance) for o in loop.outcomes]
    assert len(heard) == 2
    assert "open safari" in heard[0] and "weather" in heard[1]
    assert file_mic._closed is True


@needs_model
def test_default_model_is_the_measured_one():
    # The README table and the latency budget were measured on this model.
    assert DEFAULT_LOCAL_MODEL == "tiny.en"
    assert MODEL_DIR is not None
