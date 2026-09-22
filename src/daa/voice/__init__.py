"""Audio in, text out. Nothing goes back out as audio.

Re-exports only the Protocols, their fakes and the loop. Deliberately does NOT
import the real providers (sounddevice, AssemblyAI) -- both are optional, and
`import daa.voice` must work on a machine with neither installed.
"""

from __future__ import annotations

from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import TurnOutcome, VoiceLoop, build_loop
from daa.voice.mic import AudioChunk, AudioSource, FakeMic
from daa.voice.stt import FakeTranscriber, STTUnavailable, Transcriber, Transcription
from daa.voice.transcript import Transcript, Turn

__all__ = [
    "AudioChunk",
    "AudioSource",
    "FakeLLM",
    "FakeMic",
    "FakeTranscriber",
    "LLMTurn",
    "STTUnavailable",
    "ToolCall",
    "Transcriber",
    "Transcript",
    "Transcription",
    "Turn",
    "TurnOutcome",
    "VoiceLoop",
    "build_loop",
]
