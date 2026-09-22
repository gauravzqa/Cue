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

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Fired when the VAD sees speech BEGIN, not when the segment completes.
# Nothing subscribes today -- daa has no audio out, so there is nothing to cut
# off when the user starts talking. The hook stays because ONSET and SEGMENT
# are genuinely different moments, and anything that needs the earlier one
# (a listening indicator, a future interruption) needs it on the first
# syllable rather than a whole utterance later.
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
    # What `pcm` actually holds. "pcm_s16le" is real audio; "text" is FakeMic's
    # UTF-8 stand-in. Declared rather than sniffed so a real transcriber can
    # refuse -- or hand back to the fake -- a payload that is not audio, instead
    # of decoding a test string as noise.
    encoding: str = "pcm_s16le"

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
        """Register the speech-onset hook. Called on speech ONSET, from
        whatever thread the audio callback runs on, so the listener must be
        cheap and non-blocking."""
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
            # Fire onset BEFORE yielding: a listener must see the onset by
            # the time anything downstream sees the segment.
            if self._listener is not None:
                self._listener()
            self.emitted.append(text)
            yield AudioChunk(
                pcm=text.encode("utf-8"),
                sample_rate=self.sample_rate,
                started_at=float(index),
                complete=index not in self.incomplete,
                encoding="text",
            )
            index += 1

    def set_speech_listener(self, listener: SpeechListener | None) -> None:
        self._listener = listener

    def close(self) -> None:
        self._closed = True


class EnergyVAD:
    """Turns a stream of fixed-size PCM blocks into speech segments.

    Deliberately NOT silero-vad. silero needs torch (or onnxruntime) on the
    one code path that runs on every 30ms of audio, and the roadmap is to
    remove torch, not lean on it. Energy is crude, so three cheap things are
    layered on top to make it crude in the right direction:

      * an ADAPTIVE floor: the threshold is max(`threshold`, `floor_ratio` x
        a slow-rising, fast-falling noise estimate), so a fan or a hum stops
        reading as speech within seconds instead of being one endless utterance;
      * PRE-ROLL: the `pre_roll_ms` before onset are kept, because the block
        that crosses the threshold is already mid-syllable and STT that never
        hears the "h" of "hey daa" hears "a daa";
      * a MINIMUM loud duration: a segment with fewer than `min_speech_ms` of
        above-threshold audio is a click or a door, and is discarded rather
        than handed to STT to hallucinate a word out of.

    Pure: no audio library, no clock it cannot be handed. That is what lets
    the file-backed tests drive the exact segmenter the microphone uses.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        block_ms: int = 30,
        silence_ms: int = 700,
        max_segment_s: float = 15.0,
        threshold: int = 500,
        floor_ratio: float = 3.0,
        pre_roll_ms: int = 210,
        min_speech_ms: int = 90,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.block_frames = int(sample_rate * block_ms / 1000)
        self.silence_blocks = max(1, silence_ms // block_ms)
        self.max_blocks = max(1, int(max_segment_s * 1000 / block_ms))
        self.pre_roll_blocks = max(0, pre_roll_ms // block_ms)
        self.min_speech_blocks = max(1, -(-min_speech_ms // block_ms))
        # RMS over 16-bit samples. The FLOOR of the gate; the adaptive term can
        # only raise it, so a silent room never makes the mic more trigger-happy.
        self.threshold = threshold
        self.floor_ratio = floor_ratio
        self.noise_floor = 0.0
        self._clock = clock or time.monotonic

    @staticmethod
    def rms(block: bytes) -> float:
        import array
        import math

        samples = array.array("h")
        samples.frombytes(block[: len(block) - len(block) % 2])
        if not samples:
            return 0.0
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def gate(self) -> float:
        return max(float(self.threshold), self.noise_floor * self.floor_ratio)

    def _learn(self, level: float) -> None:
        """Fast down, slow up. Speech is modulated -- every word has a gap
        that pulls the floor straight back down -- while a fan or a hum is
        steady, so only the hum survives the slow climb (~17s time constant).
        Learning from every block, not just the quiet ones, is what lets a hum
        that is ALREADY above the threshold eventually stop reading as speech.
        """
        if level < self.noise_floor:
            self.noise_floor = 0.5 * self.noise_floor + 0.5 * level
        else:
            self.noise_floor += 0.002 * (level - self.noise_floor)

    def segment(
        self,
        blocks: Iterable[bytes],
        *,
        on_onset: SpeechListener | None = None,
        closed: Callable[[], bool] | None = None,
    ) -> Iterator[AudioChunk]:
        from collections import deque

        pre_roll: deque[bytes] = deque(maxlen=self.pre_roll_blocks or None)
        buffer: list[bytes] = []
        quiet = 0
        loud_blocks = 0
        started_at = 0.0

        def emit(complete: bool) -> AudioChunk | None:
            if loud_blocks < self.min_speech_blocks:
                return None
            return AudioChunk(
                pcm=b"".join(buffer),
                sample_rate=self.sample_rate,
                started_at=started_at,
                complete=complete,
            )

        for block in blocks:
            if closed is not None and closed():
                return
            level = self.rms(block)
            loud = level >= self.gate()
            self._learn(level)
            if not loud and not buffer:
                if self.pre_roll_blocks:
                    pre_roll.append(block)
                continue
            if loud:
                if not buffer:
                    started_at = self._clock()
                    # Fire on the FIRST loud block: onset means the first
                    # syllable, not whatever the click filter decides later.
                    if on_onset is not None:
                        on_onset()
                    buffer.extend(pre_roll)
                    pre_roll.clear()
                buffer.append(block)
                loud_blocks += 1
                quiet = 0
            else:
                # Keep trailing silence in the segment: STT models need the
                # decay of the last word or they clip it.
                buffer.append(block)
                quiet += 1

            if quiet >= self.silence_blocks:
                chunk = emit(True)
                buffer, quiet, loud_blocks = [], 0, 0
                if chunk is not None:
                    yield chunk
            elif len(buffer) >= self.max_blocks:
                chunk = emit(False)
                buffer, quiet, loud_blocks = [], 0, 0
                if chunk is not None:
                    yield chunk
        # Source exhausted mid-speech (a file ending, a stream closing): hand
        # over what was heard, marked complete -- nothing more is coming.
        if buffer:
            chunk = emit(True)
            if chunk is not None:
                yield chunk


class SoundDeviceMic:
    """Real capture via PortAudio, segmented by `EnergyVAD`.

    `sounddevice` is an optional extra and pulls in a C library, so it is
    imported inside `segments()`. A laptop without it must still be able to
    `import daa`, run the tests, and use `daa say`.

    The ONLY code in this class that tests cannot reach is the
    `RawInputStream` read loop in `_blocks`: opening the microphone raises a
    TCC prompt, and no test is allowed to. Everything after the raw bytes is
    `EnergyVAD.segment`, which the file-backed tests drive directly.
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
        self.vad = EnergyVAD(
            sample_rate=sample_rate,
            block_ms=block_ms,
            silence_ms=silence_ms,
            max_segment_s=max_segment_s,
            threshold=threshold,
        )
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

    def _blocks(self) -> Iterator[bytes]:
        import sounddevice as sd

        frames = self.vad.block_frames
        stream = sd.RawInputStream(
            samplerate=self.sample_rate, blocksize=frames, channels=1, dtype="int16"
        )
        stream.start()
        try:
            while not self._closed:
                raw, _overflowed = stream.read(frames)
                yield bytes(raw)
        finally:
            stream.stop()
            stream.close()

    def segments(self) -> Iterator[AudioChunk]:
        blocks = self._blocks()
        try:
            yield from self.vad.segment(
                blocks,
                # Looked up per onset, not captured once: the loop may swap the
                # listener mid-run.
                on_onset=lambda: self._listener() if self._listener is not None else None,
                closed=lambda: self._closed,
            )
        finally:
            # Release the device the moment the consumer stops, not whenever
            # the garbage collector gets round to the inner generator.
            close = getattr(blocks, "close", None)
            if close is not None:
                close()


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
