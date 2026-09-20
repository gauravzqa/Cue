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

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

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


class LocalTranscriber:
    """On-device STT. Currently a stub with a very deliberate seam.

    TODO(stt-local): back this with whisper.cpp (`pywhispercpp`) or Parakeet
    via MLX. The contract that implementation must keep:
      * < 300ms for a 3s segment on M-series, because this runs before the
        address gate and the gate runs before anything feels responsive;
      * no network, ever -- an utterance that does not wake us must not leave
        the laptop, and that guarantee is enforced here, not by policy;
      * return partial=True when the decoder's last token is not sentence-final.
    Until then `transcribe` raises STTUnavailable and the loop falls back,
    rather than silently returning "" and making every segment look like noise.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path

    @property
    def name(self) -> str:
        return "local"

    @staticmethod
    def available() -> bool:
        # No local backend is wired yet. Kept as a method so doctor can report
        # it without importing anything heavy.
        return False

    def transcribe(self, chunk: AudioChunk) -> Transcription:
        raise STTUnavailable(
            "local STT backend not installed; see TODO(stt-local) in daa/voice/stt.py"
        )


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
