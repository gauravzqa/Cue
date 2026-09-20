"""Audio capture, segmented by VAD.

The mic hands the rest of the pipeline SEGMENTS, not a raw sample stream.
That boundary is deliberate: voice activity detection is the one piece of this
system that must run on every millisecond of audio, so it lives as close to the
hardware as possible and everything downstream gets to be lazy and turn-shaped.

Nothing above this file ever sees PCM again -- stt.py turns a chunk into text
and the chunk is dropped. A segment that never wakes the gate is never written
anywhere, which is the whole privacy story: it does not leave the laptop, and
it does not even leave this process.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Fired when the VAD sees speech BEGIN, not when the segment completes.
# Barge-in has to cut TTS on the first syllable; waiting for end-of-segment
# means the assistant talks over the user for a full utterance.
SpeechListener = Callable[[], None]


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One VAD-delimited segment of speech.

    `pcm` is 16-bit signed mono at `sample_rate`. The fakes put UTF-8 text in
    here instead, which is not a hack so much as the point: the loop must never
    be able to read the mic without going through a Transcriber, and the only
    way to guarantee that is for the mic's payload to be opaque bytes.
    """

    pcm: bytes
    sample_rate: int = 16_000
    # Monotonic clock at the first speech frame. Used for latency accounting,
    # never for ordering -- segments are already ordered by the iterator.
    started_at: float = 0.0
    # False when the VAD cut the segment on a timeout rather than on silence,
    # i.e. the user is probably still talking. The address gate gets told, so
    # it can answer end_of_turn honestly instead of guessing from text alone.
    complete: bool = True

    @property
    def duration_s(self) -> float:
        # 2 bytes per sample, mono.
        return len(self.pcm) / 2 / self.sample_rate


@runtime_checkable
class AudioSource(Protocol):
    """Where speech comes from. One real implementation, one fake, no third."""

    def segments(self) -> Iterator[AudioChunk]:
        """Yield speech segments until the source is exhausted or closed."""
        ...

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        """Register the barge-in hook. Called on speech ONSET, from whatever
        thread the audio callback runs on, so the listener must be cheap and
        non-blocking -- in practice it is exactly `speaker.stop`."""
        ...

    def close(self) -> None:
        ...


@dataclass(slots=True)
class FakeMic:
    """Replays a scripted list of utterances as opaque chunks.

    The primary test fixture for the whole system. `utterances` are plain
    strings because that is what a test wants to write; they are encoded here
    and only FakeTranscriber may decode them, which keeps the mic->STT seam
    real even in tests.
    """

    utterances: list[str] = field(default_factory=list)
    sample_rate: int = 16_000
    # Segments the test wants marked as "user is still talking" (by index).
    incomplete: frozenset[int] = frozenset()
    _listener: SpeechListener | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    # Everything handed out, so a test can assert the loop consumed what it should.
    emitted: list[str] = field(default_factory=list)

    def push(self, text: str) -> None:
        """Append an utterance mid-run. Lets a test answer a confirmation
        prompt that it could not have known would be asked."""
        self.utterances.append(text)

    def segments(self) -> Iterator[AudioChunk]:
        index = 0
        while index < len(self.utterances) and not self._closed:
            text = self.utterances[index]
            # Fire onset BEFORE yielding: the loop's barge-in handler must have
            # already killed any in-flight TTS by the time it sees the segment.
            if self._listener is not None:
                self._listener()
            self.emitted.append(text)
            yield AudioChunk(
                pcm=text.encode("utf-8"),
                sample_rate=self.sample_rate,
                started_at=float(index),
                complete=index not in self.incomplete,
            )
            index += 1

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        self._listener = listener

    def close(self) -> None:
        self._closed = True


class SoundDeviceMic:
    """Real capture via PortAudio.

    `sounddevice` is an optional extra and pulls in a C library, so it is
    imported inside `segments()`. A laptop without it must still be able to
    `import daa`, run the tests, and use `daa say`.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        block_ms: int = 30,
        silence_ms: int = 700,
        max_segment_s: float = 15.0,
        threshold: int = 500,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.silence_ms = silence_ms
        self.max_segment_s = max_segment_s
        # RMS over 16-bit samples. Crude on purpose: silero-vad is the intended
        # implementation (see TODO below) and a threshold that is merely
        # sometimes wrong is better than a hard dependency on torch.
        self.threshold = threshold
        self._listener: SpeechListener | None = None
        self._closed = False

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        self._listener = listener

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def available() -> bool:
        try:
            import sounddevice  # noqa: F401
        except Exception:  # noqa: BLE001 -- any import failure means 'not available'
            return False
        return True

    def _rms(self, block: bytes) -> float:
        import array
        import math

        samples = array.array("h")
        samples.frombytes(block[: len(block) - len(block) % 2])
        if not samples:
            return 0.0
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def segments(self) -> Iterator[AudioChunk]:
        import time

        import sounddevice as sd  # TODO(vad): swap the RMS gate for silero-vad.

        block_frames = int(self.sample_rate * self.block_ms / 1000)
        silence_blocks = max(1, self.silence_ms // self.block_ms)
        max_blocks = int(self.max_segment_s * 1000 / self.block_ms)

        stream = sd.RawInputStream(
            samplerate=self.sample_rate, blocksize=block_frames, channels=1, dtype="int16"
        )
        stream.start()
        try:
            buffer: list[bytes] = []
            quiet = 0
            started_at = 0.0
            while not self._closed:
                raw, _overflowed = stream.read(block_frames)
                block = bytes(raw)
                loud = self._rms(block) >= self.threshold
                if loud:
                    if not buffer:
                        started_at = time.monotonic()
                        if self._listener is not None:
                            self._listener()
                    buffer.append(block)
                    quiet = 0
                elif buffer:
                    # Keep trailing silence in the segment: STT models need the
                    # decay of the last word or they clip it.
                    buffer.append(block)
                    quiet += 1

                if not buffer:
                    continue
                if quiet >= silence_blocks:
                    yield AudioChunk(
                        pcm=b"".join(buffer),
                        sample_rate=self.sample_rate,
                        started_at=started_at,
                        complete=True,
                    )
                    buffer, quiet = [], 0
                elif len(buffer) >= max_blocks:
                    yield AudioChunk(
                        pcm=b"".join(buffer),
                        sample_rate=self.sample_rate,
                        started_at=started_at,
                        complete=False,
                    )
                    buffer, quiet = [], 0
        finally:
            stream.stop()
            stream.close()


def build_mic(settings: object | None = None, *, utterances: list[str] | None = None) -> AudioSource:
    """Real mic when PortAudio is present, FakeMic otherwise.

    Degrading to a fake rather than raising keeps `daa listen` runnable on a
    machine with no audio stack -- it just has nothing to hear.
    """

    if utterances is not None:
        return FakeMic(utterances=list(utterances))
    if SoundDeviceMic.available():
        return SoundDeviceMic()
    return FakeMic()
