"""Audio in, text out; text in, audio out.

Re-exports only the Protocols, their fakes and the loop. Deliberately does NOT
import the real providers (sounddevice, AssemblyAI, Inworld, openai) -- every
one of those is optional, and `import daa.voice` must work on a machine with
none of them installed.
"""

from __future__ import annotations

from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import TurnOutcome, VoiceLoop, build_loop
from daa.voice.mic import AudioChunk, AudioSource, FakeMic
from daa.voice.stt import FakeTranscriber, STTUnavailable, Transcriber, Transcription
from daa.voice.transcript import Transcript, Turn
from daa.voice.tts import FakeSpeaker, Speaker

__all__ = [
    "AudioChunk",
    "AudioSource",
    "FakeLLM",
    "FakeMic",
    "FakeSpeaker",
    "FakeTranscriber",
    "LLMTurn",
    "STTUnavailable",
    "Speaker",
    "ToolCall",
    "Transcriber",
    "Transcript",
    "Transcription",
    "Turn",
    "TurnOutcome",
    "VoiceLoop",
    "build_loop",
]
