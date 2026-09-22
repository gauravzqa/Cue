"""EnergyVAD: the segmenter the real microphone uses, driven from synthetic PCM.

No audio device is opened anywhere in this file. `SoundDeviceMic.segments` is
`EnergyVAD.segment` over `RawInputStream` reads; these tests drive the same
`segment` with generated blocks, so the only line of the mic path they do not
reach is the device read itself.
"""

from __future__ import annotations

import math
import struct
import sys

from daa.voice.mic import AudioChunk, EnergyVAD, SoundDeviceMic

RATE = 16_000
BLOCK = 480  # 30ms at 16kHz


def tone(ms: int, amp: int = 8_000, freq: float = 220.0) -> bytes:
    n = RATE * ms // 1000
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * freq * i / RATE))
                                   for i in range(n)))


def quiet(ms: int, amp: int = 0) -> bytes:
    return tone(ms, amp=amp) if amp else b"\x00\x00" * (RATE * ms // 1000)


def blocks(pcm: bytes) -> list[bytes]:
    step = BLOCK * 2
    return [pcm[i:i + step] for i in range(0, len(pcm) - step + 1, step)]


def run(pcm: bytes, vad: EnergyVAD | None = None, **kw) -> list[AudioChunk]:
    vad = vad or EnergyVAD(clock=lambda: 0.0)
    return list(vad.segment(blocks(pcm), **kw))


def test_two_utterances_separated_by_silence_are_two_segments():
    pcm = quiet(300) + tone(600) + quiet(900) + tone(450) + quiet(900)
    chunks = run(pcm)
    assert len(chunks) == 2
    assert all(c.complete for c in chunks)
    assert all(c.encoding == "pcm_s16le" for c in chunks)


def test_pre_roll_keeps_the_audio_just_before_onset():
    # 600ms of speech + 700ms trailing silence + 210ms pre-roll.
    chunks = run(quiet(600) + tone(600) + quiet(900))
    assert len(chunks) == 1
    assert 1.45 <= chunks[0].duration_s <= 1.56, chunks[0].duration_s
    # The segment STARTS with the pre-roll (silence here), not with the tone.
    assert chunks[0].pcm[:BLOCK * 2] == b"\x00\x00" * BLOCK


def test_a_click_is_not_handed_to_stt():
    # One loud 30ms block: under min_speech_ms, dropped. Whisper would otherwise
    # be asked to find a word in a door slam.
    assert run(quiet(300) + tone(30) + quiet(900)) == []


def test_timeout_cut_is_marked_incomplete():
    vad = EnergyVAD(max_segment_s=1.0, clock=lambda: 0.0)
    chunks = run(tone(2_200) + quiet(900), vad)
    assert [c.complete for c in chunks][:2] == [False, False]
    assert chunks[-1].complete is True


def test_steady_hum_is_learned_instead_of_being_one_endless_segment():
    # A hum ABOVE the fixed threshold from the very first block. Without the
    # adaptive floor this is a 15s timeout segment, forever.
    vad = EnergyVAD(threshold=100, clock=lambda: 0.0)
    hum = quiet(20_000, amp=400)  # rms ~283, well over threshold=100
    chunks = run(hum, vad)
    assert len(chunks) <= 1, "the hum kept producing segments"
    assert all(c.duration_s < 10 for c in chunks)
    assert vad.gate() > 283, "the floor never learned the hum"
    # Speech over the learned hum still segments.
    assert len(list(vad.segment(blocks(tone(600, amp=8_000) + hum[:32_000])))) == 1


def test_speech_does_not_teach_the_floor_to_ignore_speech():
    # Word-length bursts with gaps: the gaps pull the floor back down.
    vad = EnergyVAD(clock=lambda: 0.0)
    words = (tone(300) + quiet(120)) * 12 + quiet(900)
    chunks = run(words * 3, vad)
    assert len(chunks) == 3
    assert vad.gate() == vad.threshold


def test_onset_fires_once_per_segment_before_it_is_yielded():
    order: list[str] = []
    vad = EnergyVAD(clock=lambda: 0.0)
    pcm = quiet(300) + tone(600) + quiet(900) + tone(600) + quiet(900)
    for _chunk in vad.segment(blocks(pcm), on_onset=lambda: order.append("onset")):
        order.append("segment")
    assert order == ["onset", "segment", "onset", "segment"]


def test_closed_stops_segmentation():
    state = {"closed": False}
    vad = EnergyVAD(clock=lambda: 0.0)
    pcm = tone(600) + quiet(900) + tone(600) + quiet(900)
    seen = []
    for chunk in vad.segment(blocks(pcm), closed=lambda: state["closed"]):
        seen.append(chunk)
        state["closed"] = True
    assert len(seen) == 1


def test_source_ending_mid_speech_still_yields_what_was_heard():
    chunks = run(quiet(300) + tone(600))
    assert len(chunks) == 1 and chunks[0].complete


def test_sounddevice_mic_uses_the_same_segmenter_and_imports_nothing():
    mic = SoundDeviceMic(silence_ms=300, threshold=700)
    assert isinstance(mic.vad, EnergyVAD)
    assert mic.vad.silence_blocks == 10 and mic.vad.threshold == 700
    assert "sounddevice" not in sys.modules


def test_sounddevice_mic_segments_are_the_vads(monkeypatch):
    """Swap ONLY the device read for canned blocks: everything after it is the
    code that ships, including onset -> listener and close()."""
    mic = SoundDeviceMic()
    pcm = quiet(300) + tone(600) + quiet(900)
    monkeypatch.setattr(mic, "_blocks", lambda: iter(blocks(pcm)))
    onsets: list[int] = []
    mic.set_speech_listener(lambda: onsets.append(1))
    chunks = list(mic.segments())
    assert len(chunks) == 1 and onsets == [1]
