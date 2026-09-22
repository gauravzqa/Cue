"""Speech to text, in two tiers.

The two-tier split is the reason this system can be always-on without being
expensive or creepy:

    local  -- runs on every segment, must be cheap and offline. Its output is
              only ever shown to the address gate. If the gate says "not for
              me", this transcript is discarded and nothing was uploaded.
    cloud  -- runs ONLY after the gate fires, because by then the user has
              addressed us and accuracy is worth the round trip. Rescoring a
              woken utterance is what makes "move the Ferrari screenshots"
              survive a noisy room.

Both tiers are the same Protocol, so the loop can be given the same object
twice (tests do exactly that) and nothing downstream notices.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from daa.voice.mic import AudioChunk


class STTUnavailable(RuntimeError):
    """Raised when a transcriber cannot run: no model, no key, no network.

    A distinct type because the loop treats it as "fall back to the other
    tier", not as a crash. Losing cloud rescore degrades quality; losing the
    loop loses the user's sentence.
    """


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    # 0..1. Local models are optimistic; treat this as a relative signal within
    # one tier, never as a calibrated probability (that is Jev's job).
    confidence: float = 1.0
    source: str = "fake"          # "local" | "cloud" | "fake"
    latency_ms: float = 0.0
    # True when the tier believes the user is mid-sentence. The gate takes this
    # as evidence for end_of_turn but is free to disagree.
    partial: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, chunk: AudioChunk) -> Transcription:
        ...

    @property
    def name(self) -> str:
        ...


@dataclass(slots=True)
class FakeTranscriber:
    """Decodes the text FakeMic encoded, optionally rewriting it.

    `rescore` exists to model the ONE thing the two tiers actually differ on:
    the cloud tier hears the same audio better. A test builds a local fake that
    returns "move the ferrari screen shots" and a cloud fake that rescores it
    to "move the Ferrari screenshots", then asserts the tool saw the good one.
    """

    source: str = "fake"
    rescore: Mapping[str, str] = field(default_factory=dict)
    confidence: float = 1.0
    calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"fake:{self.source}"

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        heard = chunk.pcm.decode("utf-8", errors="replace")
        self.calls.append(heard)
        return Transcription(
            text=self.rescore.get(heard, heard),
            confidence=self.confidence,
            source=self.source,
            latency_ms=0.0,
            partial=not chunk.complete,
        )


# ---------------------------------------------------------------------------
# local tier
# ---------------------------------------------------------------------------

# tiny.en rather than base.en: on an M3 Max it scores the same 9.0% WER on
# evals/ as Apple's SpeechTranscriber at ~80ms, and base.en's 7.2% costs ~165ms
# median with a ~400ms tail -- over budget on anything smaller than a Max.
# Override with DAA_STT_LOCAL_MODEL (a size name, or a model directory).
DEFAULT_LOCAL_MODEL = "tiny.en"
_HF_REPO = "Systran/faster-whisper-{name}"
# Whisper's own "this segment is not speech" signal. Every real utterance in
# evals/ scores <= 0.39; hum and fan noise that whisper turns into " you" or
# " Thank you." score >= 0.8. Dropping those here keeps a hallucinated word
# from ever reaching the address gate.
_NO_SPEECH_P = 0.6
_SENTENCE_FINAL = (".", "?", "!")

# Python-level network operations. Any of these on a thread that is inside the
# local transcriber is a bug, and it is refused BEFORE the packet exists.
_NET_EVENTS = frozenset({
    "socket.connect",
    "socket.sendto",
    "socket.sendmsg",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.gethostbyname_ex",
    "socket.gethostbyaddr",
    "urllib.Request",
    "http.client.connect",
})


class NetworkForbidden(RuntimeError):
    """The local tier tried to touch the network. Raised by the audit hook,
    from inside the offending call, so nothing is ever sent."""


_offline = threading.local()
_hook_lock = threading.Lock()
_hook_installed = False


def _audit(event: str, args: tuple[object, ...]) -> None:
    # Runs on EVERY audited event in the process, so the cheap check is first.
    if event in _NET_EVENTS and getattr(_offline, "depth", 0):
        raise NetworkForbidden(
            f"local STT attempted network I/O ({event}); refused -- an utterance "
            "that has not woken the gate must not leave the laptop"
        )


class _NoNetwork:
    """Refuses Python-level network I/O on THIS thread while active.

    A `sys.addaudithook` rather than a monkeypatch of `socket`: an audit hook
    cannot be unpatched by a library that imports socket first, sees `connect`
    AND `getaddrinfo` (a DNS lookup alone leaks what we were about to fetch),
    and fires before the syscall. Thread-local rather than process-wide so a
    tool job fetching a web page on another thread is not collateral damage.

    What it cannot see is native code opening sockets itself. CTranslate2 has
    no networking code; the model is loaded from a directory path, which is the
    other half of the guarantee (see `LocalTranscriber._load`).
    """

    def __enter__(self) -> Self:
        global _hook_installed
        if not _hook_installed:
            with _hook_lock:
                if not _hook_installed:
                    import sys

                    sys.addaudithook(_audit)
                    _hook_installed = True
        _offline.depth = getattr(_offline, "depth", 0) + 1
        return self

    def __exit__(self, *exc: object) -> None:
        _offline.depth -= 1


def no_network() -> _NoNetwork:
    return _NoNetwork()


def _hf_hub_dir() -> Path:
    import os

    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def resolve_local_model(spec: str | None = None) -> Path | None:
    """Find a CTranslate2 whisper model ON DISK. Never downloads.

    `spec` is a directory holding `model.bin`, or a size name ("tiny.en")
    looked up in the Hugging Face cache. Pure filesystem, so `daa doctor` can
    call it on every run. Getting the model there is a one-time, explicit,
    networked setup step (`pip install 'daa[stt]'`, then `hf download
    Systran/faster-whisper-tiny.en`) and is never done from this module.
    """
    import os

    spec = spec or os.environ.get("DAA_STT_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL
    direct = Path(spec).expanduser()
    if (direct / "model.bin").is_file():
        return direct
    repo = _hf_hub_dir() / ("models--" + _HF_REPO.format(name=spec).replace("/", "--"))
    snapshots = repo / "snapshots"
    ref = repo / "refs" / "main"
    try:
        if ref.is_file():
            pinned = snapshots / ref.read_text().strip()
            if (pinned / "model.bin").is_file():
                return pinned
        candidates = sorted(
            (p for p in snapshots.iterdir() if (p / "model.bin").is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def _performance_cores() -> int:
    """P-core count via sysctl, in-process. E-cores make whisper SLOWER: the
    encoder is split evenly across threads and waits on the slowest."""
    import os

    fallback = max(1, (os.cpu_count() or 4) // 2)
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        found = libc.sysctlbyname(
            b"hw.perflevel0.physicalcpu", ctypes.byref(value), ctypes.byref(size), None, 0
        )
    except Exception:  # noqa: BLE001 -- not macOS / no sysctl
        return fallback
    return value.value if found == 0 and value.value > 0 else fallback


def is_sentence_final(text: str) -> bool:
    stripped = text.rstrip().rstrip("\"')\u201d\u2019")
    if not stripped or stripped.endswith(("...", "\u2026")):
        # A trailing ellipsis is whisper's way of saying the speaker trailed off.
        return False
    return stripped.endswith(_SENTENCE_FINAL)


class LocalTranscriber:
    """On-device STT: faster-whisper (CTranslate2), int8 on the CPU.

    Why not Apple's engine, which the dock already uses at 9.0% WER? Because
    from Python it is unreachable in-process: `SpeechAnalyzer` and
    `SpeechTranscriber` are Swift-only classes with no Objective-C surface, so
    PyObjC cannot call them, and the older `SFSpeechRecognizer` needs Speech
    Recognition authorization -- a TCC prompt on the interpreter. whisper
    tiny.en matches the Swift WER on evals/ with neither problem.

    The contract, and where each clause is kept:
      * < 300ms for a 3s segment on M-series -- tiny.en, greedy decoding, no
        timestamps, P-cores only. Whisper pads every input to 30s, so the cost
        is nearly flat in segment length: ~80ms on an M3 Max.
      * no network, ever -- the model is loaded from a local DIRECTORY (never
        a hub name, so no download code runs) and every load and decode runs
        under `no_network()`, which refuses socket and DNS calls on this thread.
        A missing model is `STTUnavailable`, never a fetch.
      * partial=True when the last token is not sentence-final. Whisper
        punctuates, so this is read off the text; a segment the VAD cut on its
        timeout is partial whatever the punctuation says.

    The model loads on the FIRST `transcribe`, not at construction: `daa
    doctor` builds the loop, and doctor must not pay for a speech model.
    """

    def __init__(
        self,
        model_path: str | None = None,
        *,
        cpu_threads: int | None = None,
        beam_size: int = 1,
        language: str | None = "en",
    ) -> None:
        self.model_path = model_path
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.language = language
        self._model: object | None = None
        self._lock = threading.Lock()
        # Decodes FakeMic's text payloads. Only reachable for encoding="text"
        # chunks, i.e. only when a test hands the real loop a FakeMic.
        self._fake = FakeTranscriber(source="local")

    @property
    def name(self) -> str:
        return "local"

    @staticmethod
    def available(model_path: str | None = None) -> bool:
        """Package importable AND a model on disk. Imports nothing heavy:
        `find_spec` reads the path, it does not execute the package."""
        from importlib.util import find_spec

        try:
            if find_spec("faster_whisper") is None or find_spec("ctranslate2") is None:
                return False
        except (ImportError, ValueError):
            return False
        return resolve_local_model(model_path) is not None

    @staticmethod
    def describe(model_path: str | None = None) -> str:
        """One line for `daa doctor`: which model, or why there is none."""
        from importlib.util import find_spec

        if find_spec("faster_whisper") is None:
            return "faster-whisper not installed"
        directory = resolve_local_model(model_path)
        if directory is None:
            return "no whisper model on disk"
        name = directory.parent.parent.name.removeprefix("models--Systran--faster-whisper-")
        return f"whisper {name if directory.parent.name == 'snapshots' else directory}"

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            directory = resolve_local_model(self.model_path)
            if directory is None:
                raise STTUnavailable(
                    "no local whisper model on disk; fetch it once with "
                    "`hf download Systran/faster-whisper-tiny.en`"
                )
            try:
                with no_network():
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(
                        # A PATH, never a size name: given a name, faster-whisper
                        # calls huggingface_hub, which is download code.
                        str(directory),
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=self.cpu_threads or min(8, _performance_cores()),
                        local_files_only=True,
                    )
            except NetworkForbidden:
                raise
            except Exception as exc:  # missing package, corrupt model
                raise STTUnavailable(f"local whisper failed to load: {exc}") from exc
            return self._model

    def warm(self) -> None:
        """Load now instead of on the first utterance (~0.3s)."""
        self._load()

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        if chunk.encoding == "text":
            return self._fake.transcribe(chunk)
        if chunk.encoding != "pcm_s16le":
            raise STTUnavailable(f"local STT cannot decode {chunk.encoding!r} audio")
        started = time.monotonic()
        model = self._load()
        try:
            with no_network():
                text, confidence = self._decode(model, chunk)
        except NetworkForbidden as exc:
            # Surfaced as STTUnavailable so the loop degrades, but the message
            # is the loud one: this is a privacy bug, not a flaky backend.
            raise STTUnavailable(str(exc)) from exc
        return Transcription(
            text=text,
            confidence=confidence,
            source="local",
            latency_ms=(time.monotonic() - started) * 1000,
            partial=bool(text) and (not chunk.complete or not is_sentence_final(text)),
        )

    def _decode(self, model: object, chunk: AudioChunk) -> tuple[str, float]:
        import math

        import numpy as np

        pcm = chunk.pcm[: len(chunk.pcm) - len(chunk.pcm) % 2]
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            return "", 0.0
        if chunk.sample_rate != 16_000:
            # Whisper is trained at 16 kHz. Linear interpolation is plenty for
            # speech and keeps a resampler dependency out of the tree.
            n = round(audio.size * 16_000 / chunk.sample_rate)
            audio = np.interp(
                np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio
            ).astype(np.float32)
        segments, _info = model.transcribe(  # type: ignore[attr-defined]
            audio,
            language=self.language,
            beam_size=self.beam_size,
            without_timestamps=True,
            # Each VAD segment stands alone: carrying text across segments is
            # how whisper loops on a phrase it heard two utterances ago.
            condition_on_previous_text=False,
            vad_filter=False,
        )
        kept = [s for s in segments if s.no_speech_prob <= _NO_SPEECH_P]
        text = " ".join(s.text.strip() for s in kept if s.text.strip()).strip()
        if not text:
            return "", 0.0
        mean_logprob = sum(s.avg_logprob for s in kept) / len(kept)
        return text, max(0.0, min(1.0, math.exp(mean_logprob)))


class AssemblyAITranscriber:
    """Cloud STT. Only ever called on segments that already woke the gate."""

    def __init__(self, api_key: str | None, *, sample_rate: int = 16_000) -> None:
        self.api_key = api_key
        self.sample_rate = sample_rate

    @property
    def name(self) -> str:
        return "cloud"

    def available(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        if not self.api_key:
            raise STTUnavailable("ASSEMBLYAI_API_KEY not set")
        # Imported here, not at module scope: an httpx import on every `daa`
        # invocation is measurable, and `daa doctor` must work with no network.
        import httpx2 as httpx  # type: ignore[import-not-found]

        started = time.monotonic()
        headers = {"authorization": self.api_key}
        try:
            with httpx.Client(timeout=10.0) as client:
                upload = client.post(
                    "https://api.assemblyai.com/v2/upload",
                    headers=headers,
                    content=_wav(chunk),
                )
                upload.raise_for_status()
                job = client.post(
                    "https://api.assemblyai.com/v2/transcript",
                    headers=headers,
                    json={"audio_url": upload.json()["upload_url"], "speech_model": "best"},
                )
                job.raise_for_status()
                job_id = job.json()["id"]
                # Poll rather than webhook: this is a 2-4s utterance, and a
                # callback server is a lot of surface area for one sentence.
                for _ in range(60):
                    poll = client.get(
                        f"https://api.assemblyai.com/v2/transcript/{job_id}", headers=headers
                    )
                    poll.raise_for_status()
                    body = poll.json()
                    if body["status"] == "completed":
                        return Transcription(
                            text=body.get("text") or "",
                            confidence=float(body.get("confidence") or 0.0),
                            source="cloud",
                            latency_ms=(time.monotonic() - started) * 1000,
                        )
                    if body["status"] == "error":
                        raise STTUnavailable(str(body.get("error")))
                    time.sleep(0.2)
        except STTUnavailable:
            raise
        except Exception as exc:  # network, auth, schema drift -- all recoverable
            raise STTUnavailable(f"assemblyai: {exc}") from exc
        raise STTUnavailable("assemblyai: timed out")


def _wav(chunk: AudioChunk) -> bytes:
    """Wrap raw PCM in a WAV header so the uploader does not have to guess."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(chunk.sample_rate)
        handle.writeframes(chunk.pcm)
    return buf.getvalue()


def build_local(settings: object) -> Transcriber:
    """Local tier, or a fake that echoes -- never None.

    The loop must always have something to feed the gate; a missing local
    backend degrades quality, it does not turn the mic off.
    """
    if getattr(settings, "stt_local", True) and LocalTranscriber.available():
        return LocalTranscriber()
    return FakeTranscriber(source="local")


def build_cloud(settings: object) -> Transcriber | None:
    """Cloud rescorer, or None when there is no key.

    None rather than a fake: the loop must be able to tell "rescoring is off"
    from "rescoring returned the same text", and it logs the difference.
    """
    import os

    key = os.environ.get("ASSEMBLYAI_API_KEY") or None
    if not key:
        return None
    return AssemblyAITranscriber(key)
