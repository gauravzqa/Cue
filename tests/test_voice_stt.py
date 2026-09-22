"""Mic, STT and TTS boundaries. No hardware, no network, no keys.

The recurring assertion in this file is that the OPTIONAL half of each module
stays optional: importing daa.voice must not import sounddevice, httpx or
openai, and a missing backend must raise STTUnavailable (which the loop
handles) rather than something the loop would crash on.
"""

from __future__ import annotations

import sys

import pytest

from daa.config import Settings
from daa.voice.mic import AudioChunk, AudioSource, FakeMic, SoundDeviceMic, build_mic
from daa.voice.stt import (
    AssemblyAITranscriber,
    FakeTranscriber,
    LocalTranscriber,
    STTUnavailable,
    Transcriber,
    Transcription,
    build_cloud,
    build_local,
)
from daa.voice.tts import FakeSpeaker, FallbackSpeaker, SaySpeaker, Speaker

# ---------------------------------------------------------------------------
# mic
# ---------------------------------------------------------------------------


def test_fake_mic_satisfies_the_protocol():
    assert isinstance(FakeMic(), AudioSource)


def test_fake_mic_yields_opaque_bytes_not_text():
    mic = FakeMic(utterances=["hello there"])
    chunk = next(iter(mic.segments()))
    assert isinstance(chunk.pcm, bytes)
    # The loop must be unable to read the mic without a Transcriber.
    assert not hasattr(chunk, "text")


def test_fake_mic_marks_incomplete_segments():
    mic = FakeMic(utterances=["one", "two"], incomplete=frozenset({0}))
    chunks = list(mic.segments())
    assert chunks[0].complete is False
    assert chunks[1].complete is True


def test_fake_mic_fires_speech_onset_before_the_segment():
    order: list[str] = []
    mic = FakeMic(utterances=["a", "b"])
    mic.set_speech_listener(lambda: order.append("onset"))
    for _chunk in mic.segments():
        order.append("segment")
    assert order == ["onset", "segment", "onset", "segment"]


def test_fake_mic_can_be_pushed_to_mid_run():
    mic = FakeMic(utterances=["first"])
    seen = []
    for chunk in mic.segments():
        seen.append(chunk.pcm.decode())
        if len(seen) == 1:
            mic.push("second")
    assert seen == ["first", "second"]


def test_fake_mic_stops_when_closed():
    mic = FakeMic(utterances=["a", "b", "c"])
    seen = []
    for chunk in mic.segments():
        seen.append(chunk.pcm)
        mic.close()
    assert len(seen) == 1


def test_audio_chunk_duration():
    assert AudioChunk(pcm=b"\x00" * 32_000, sample_rate=16_000).duration_s == 1.0


def test_build_mic_degrades_to_a_fake_without_portaudio():
    mic = build_mic(Settings())
    assert isinstance(mic, (FakeMic, SoundDeviceMic))
    if not SoundDeviceMic.available():
        assert isinstance(mic, FakeMic)


def test_sounddevice_is_never_imported_at_module_scope():
    # The whole point of the lazy import: `import daa` on a machine with no
    # audio stack must work.
    assert "sounddevice" not in sys.modules


def test_sounddevice_rms_is_pure_python():
    # Runs without numpy, which is an optional extra.
    mic = SoundDeviceMic()
    assert mic.vad.rms(b"\x00\x00" * 100) == 0.0
    assert mic.vad.rms(b"\xff\x7f" * 100) > 32_000


# ---------------------------------------------------------------------------
# stt
# ---------------------------------------------------------------------------


def test_fake_transcriber_satisfies_the_protocol():
    assert isinstance(FakeTranscriber(), Transcriber)


def test_fake_transcriber_round_trips_fake_mic():
    mic = FakeMic(utterances=["move the screenshots"])
    stt = FakeTranscriber(source="local")
    chunk = next(iter(mic.segments()))
    assert stt.transcribe(chunk).text == "move the screenshots"
    assert stt.calls == ["move the screenshots"]


def test_fake_transcriber_models_cloud_rescoring():
    cloud = FakeTranscriber(source="cloud", rescore={"ferari": "Ferrari"})
    chunk = AudioChunk(pcm=b"ferari")
    assert cloud.transcribe(chunk).text == "Ferrari"
    assert cloud.transcribe(AudioChunk(pcm=b"other")).text == "other"


def test_partial_flag_follows_the_vad():
    stt = FakeTranscriber()
    assert stt.transcribe(AudioChunk(pcm=b"hi", complete=False)).partial is True
    assert stt.transcribe(AudioChunk(pcm=b"hi", complete=True)).partial is False


def test_transcription_empty():
    assert Transcription(text="   ").empty is True
    assert Transcription(text="x").empty is False


def test_local_backend_raises_a_handled_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    # No model on disk: a handled STTUnavailable, never a download.
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.delenv("DAA_STT_LOCAL_MODEL", raising=False)
    with pytest.raises(STTUnavailable):
        LocalTranscriber().transcribe(AudioChunk(pcm=b"hi"))


def test_cloud_without_a_key_raises_before_touching_the_network():
    with pytest.raises(STTUnavailable):
        AssemblyAITranscriber(None).transcribe(AudioChunk(pcm=b"hi"))
    assert not AssemblyAITranscriber(None).available()


def test_build_local_always_returns_something():
    # The loop must always have a tier to feed the gate.
    assert build_local(Settings(stt_local=True)) is not None
    assert build_local(Settings(stt_local=False)) is not None


def test_build_cloud_is_none_without_a_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    # None rather than a fake: the loop distinguishes "off" from "unchanged".
    assert build_cloud(Settings()) is None


def test_build_cloud_returns_a_client_with_a_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "not-a-real-key")
    cloud = build_cloud(Settings())
    assert isinstance(cloud, AssemblyAITranscriber)
    assert cloud.available()


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


def test_fake_speaker_satisfies_the_protocol():
    assert isinstance(FakeSpeaker(), Speaker)


def test_fake_speaker_keeps_speaking_until_stopped():
    speaker = FakeSpeaker()
    speaker.say("moving forty-seven screenshots")
    assert speaker.is_speaking()
    speaker.stop()
    assert not speaker.is_speaking()
    assert speaker.interrupted == ["moving forty-seven screenshots"]
    assert speaker.stops == 1


def test_stop_is_idempotent_when_silent():
    speaker = FakeSpeaker()
    speaker.stop()
    speaker.stop()
    assert speaker.interrupted == []


def test_a_new_utterance_interrupts_the_previous_one():
    speaker = FakeSpeaker()
    speaker.say("one")
    speaker.say("two")
    assert speaker.interrupted == ["one"]
    assert speaker.last == "two"


def test_fallback_speaker_moves_on_when_a_provider_raises():
    class Broken:
        name = "broken"

        def say(self, text):
            raise RuntimeError("402")

        def stop(self):
            pass

        def is_speaking(self):
            return False

    good = FakeSpeaker()
    chain = FallbackSpeaker([Broken(), good])
    chain.say("hello")
    assert good.said == ["hello"]
    assert chain.active is good
    assert chain.failures and "broken" in chain.failures[0]


def test_fallback_speaker_stops_every_provider():
    a, b = FakeSpeaker(), FakeSpeaker()
    chain = FallbackSpeaker([a, b])
    a.say("x")
    b.say("y")
    chain.stop()
    assert not chain.is_speaking()


def test_say_speaker_reports_availability_without_running_it():
    # No subprocess is spawned by a mere availability check.
    assert isinstance(SaySpeaker.available(), bool)


def test_no_network_client_is_imported_by_the_voice_package():
    for module in ("sounddevice", "openai"):
        assert module not in sys.modules, f"{module} was imported at module scope"
