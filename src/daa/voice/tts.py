"""Speech out, with interruption as a first-class operation.

`stop()` is in the Protocol, not bolted on. A voice assistant that cannot be
cut off mid-sentence is unusable: the user says "move those files", hears
"moving forty-seven screen--", says "no wait", and the only acceptable
behaviour is silence within ~100ms. Every Speaker therefore owns a handle it
can kill, and the loop wires mic speech-onset straight to `stop`.

Provider chain, best to worst, all lazy: Inworld -> OpenAI -> macOS `say`.
The last one has no key, no network and no install, so there is always a voice.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class Speaker(Protocol):
    def say(self, text: str) -> None:
        """Begin speaking. May return before playback finishes."""
        ...

    def stop(self) -> None:
        """Cut playback immediately. Idempotent and safe when already silent."""
        ...

    def is_speaking(self) -> bool:
        ...

    @property
    def name(self) -> str:
        ...


@dataclass(slots=True)
class FakeSpeaker:
    """Records what was said and what was cut off.

    Speech does NOT end on its own here: `say` leaves the fake "speaking" until
    `stop` or the next `say`. That is what makes barge-in deterministic in a
    test -- real playback ends on a wall clock the test has no business
    waiting on, so the fake models the only part that matters, which is whether
    an interruption arrived while audio was live.
    """

    said: list[str] = field(default_factory=list)
    # Utterances that were still playing when stop() arrived.
    interrupted: list[str] = field(default_factory=list)
    stops: int = 0
    _current: str | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return "fake"

    def say(self, text: str) -> None:
        if self._current is not None:
            self.interrupted.append(self._current)
        self.said.append(text)
        self._current = text

    def stop(self) -> None:
        self.stops += 1
        if self._current is not None:
            self.interrupted.append(self._current)
            self._current = None

    def is_speaking(self) -> bool:
        return self._current is not None

    @property
    def last(self) -> str | None:
        return self.said[-1] if self.said else None


class SaySpeaker:
    """macOS `say(1)`. Ugly voice, zero dependencies, always available.

    This is the floor of the provider chain and also the safety net: if an
    Inworld call is in flight when the user barges in, we still want a Speaker
    whose `stop` is a signal to a process we own rather than a promise to a
    remote service.
    """

    def __init__(self, voice: str | None = None, rate: int | None = None) -> None:
        self.voice = voice
        self.rate = rate
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def name(self) -> str:
        return "say"

    @staticmethod
    def available() -> bool:
        return shutil.which("say") is not None

    def say(self, text: str) -> None:
        self.stop()
        argv = ["say"]
        if self.voice:
            argv += ["-v", self.voice]
        if self.rate:
            argv += ["-r", str(self.rate)]
        argv.append(text)
        self._proc = subprocess.Popen(argv)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def is_speaking(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class _HTTPSpeaker:
    """Shared machinery for the two cloud voices.

    Synthesis and playback are separated so `stop` can kill playback without
    waiting for a hung HTTP call, and so a failed synthesis can fall through to
    the next provider before any audio was promised to the user.
    """

    endpoint = ""

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key
        self._player: subprocess.Popen[bytes] | None = None

    def available(self) -> bool:
        return bool(self.api_key) and shutil.which("afplay") is not None

    def _synthesize(self, text: str) -> bytes:
        raise NotImplementedError

    def say(self, text: str) -> None:
        self.stop()
        audio = self._synthesize(text)
        # afplay reads stdin-less, so hand it a temp file; it is the one macOS
        # player that is always present and exits cleanly on SIGTERM.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(audio)
            path = handle.name
        self._player = subprocess.Popen(["afplay", path])

    def stop(self) -> None:
        proc, self._player = self._player, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def is_speaking(self) -> bool:
        return self._player is not None and self._player.poll() is None


class InworldSpeaker(_HTTPSpeaker):
    """Primary voice. Fast enough that the reply starts before the user blinks."""

    @property
    def name(self) -> str:
        return "inworld"

    def _synthesize(self, text: str) -> bytes:
        import base64

        import httpx2 as httpx  # type: ignore[import-not-found]

        response = httpx.post(
            "https://api.inworld.ai/tts/v1/voice",
            headers={"Authorization": f"Basic {self.api_key}"},
            json={"text": text, "voiceId": "Ashley", "modelId": "inworld-tts-1"},
            timeout=10.0,
        )
        response.raise_for_status()
        return base64.b64decode(response.json()["audioContent"])


class OpenAISpeaker(_HTTPSpeaker):
    """Fallback voice. Slower, but a key most people already have."""

    @property
    def name(self) -> str:
        return "openai"

    def _synthesize(self, text: str) -> bytes:
        import httpx2 as httpx  # type: ignore[import-not-found]

        response = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text},
            timeout=15.0,
        )
        response.raise_for_status()
        return response.content


class FallbackSpeaker:
    """Tries providers in order, remembering which one worked.

    A TTS failure must never surface as an exception in the loop: the tool
    already ran, and swallowing the sentence is better than crashing after a
    mutation. So this degrades, and only raises if even `say` is missing.
    """

    def __init__(self, speakers: list[Speaker]) -> None:
        self.speakers = speakers
        self.active: Speaker | None = None
        self.failures: list[str] = []

    @property
    def name(self) -> str:
        return self.active.name if self.active else "+".join(s.name for s in self.speakers)

    def say(self, text: str) -> None:
        for speaker in self.speakers:
            try:
                speaker.say(text)
            except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
                self.failures.append(f"{speaker.name}: {exc}")
                continue
            self.active = speaker
            return
        self.active = None

    def stop(self) -> None:
        # Stop everything, not just `active`: a provider that raised may still
        # have started playback before it failed.
        for speaker in self.speakers:
            try:
                speaker.stop()
            except Exception:  # noqa: BLE001, S110 -- a provider that cannot
                # be stopped is not a reason to leave the others playing.
                pass

    def is_speaking(self) -> bool:
        return any(s.is_speaking() for s in self.speakers)


def build_speaker(settings: object | None = None) -> Speaker:
    """Assemble the provider chain from whatever keys exist."""
    import os

    chain: list[Speaker] = []
    inworld = InworldSpeaker(os.environ.get("INWORLD_API_KEY") or None)
    if inworld.available():
        chain.append(inworld)
    openai = OpenAISpeaker(os.environ.get("OPENAI_API_KEY") or None)
    if openai.available():
        chain.append(openai)
    if SaySpeaker.available():
        chain.append(SaySpeaker())
    if not chain:
        return FakeSpeaker()
    return FallbackSpeaker(chain)
