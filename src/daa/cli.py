"""Command line entry point.

Four subcommands, in order of how often you will actually type them:

    daa say "<text>"   one-shot through the whole pipeline with no mic. This is
                       the dev loop -- it must work with zero keys, zero audio
                       hardware and zero network, or nobody will use it.
    daa doctor         what is actually wired: settings, keys, TCC grants, and
                       for every provider whether it is LIVE or a FAKE. A
                       system with this many optional pieces needs one place
                       that tells the truth about which ones are on.
    daa listen         the real loop.
    daa undo           reverse the last recorded mutation. The journal is a
                       file on disk, so the row is validated against the
                       registry and confirmed out loud before it runs -- see
                       VoiceLoop.undo_last.

Nothing here does any work itself. The CLI's job is to construct a VoiceLoop
and print what it decided; every rule lives in loop.py, safety/ and jev/.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import fields
from typing import Any

from daa.config import Settings

# Keys that may exist. Never printed, only ever reported present/absent --
# a doctor command that echoes a secret into a terminal scrollback is a bug.
_KEYS = (
    ("TYPESAFE_API_KEY", "Jev judgments"),
    ("DEEPSEEK_API_KEY", "conversational model"),
    ("ASSEMBLYAI_API_KEY", "cloud STT rescore"),
    ("INWORLD_API_KEY", "TTS"),
    ("OPENAI_API_KEY", "TTS fallback"),
)


def _mark(ok: bool) -> str:
    return "live" if ok else "fake"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _tcc_status() -> list[tuple[str, str]]:
    """Best-effort TCC probe.

    Every check is wrapped: the point of doctor is to run on a broken machine,
    so a missing pyobjc framework must produce "unknown", never a traceback.
    """
    out: list[tuple[str, str]] = []

    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio  # type: ignore

        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        out.append(("microphone", {0: "not asked", 1: "restricted", 2: "denied", 3: "granted"}.get(
            int(status), f"status {status}")))
    except Exception:  # noqa: BLE001 -- isolation boundary, see comment
        out.append(("microphone", "unknown (AVFoundation not installed)"))

    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        out.append(("accessibility", "granted" if AXIsProcessTrusted() else "denied"))
    except Exception:  # noqa: BLE001 -- isolation boundary, see comment
        out.append(("accessibility", "unknown (ApplicationServices not installed)"))

    # Full Disk Access has no API; readability of the TCC database is the
    # standard proxy, and a failure here is exactly the "denied" answer.
    tcc = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
    try:
        with open(tcc, "rb") as handle:
            handle.read(1)
        out.append(("full disk access", "granted"))
    except Exception:  # noqa: BLE001 -- isolation boundary, see comment
        out.append(("full disk access", "denied or unknown"))

    return out


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = Settings.load()
    write = sys.stdout.write

    write("settings\n")
    for f in fields(settings):
        value = getattr(settings, f.name)
        if f.name.endswith("_api_key"):
            value = "set" if value else "unset"
        write(f"  {f.name:<18} {value}\n")

    write("\napi keys\n")
    for name, purpose in _KEYS:
        write(f"  {name:<20} {'present' if os.environ.get(name) else 'missing':<8} {purpose}\n")

    write("\ntcc permissions\n")
    for name, status in _tcc_status():
        write(f"  {name:<18} {status}\n")

    write("\nproviders\n")
    from daa.voice import loop as loop_mod
    from daa.voice import mic as mic_mod
    from daa.voice import stt as stt_mod
    from daa.voice import tts as tts_mod

    write(f"  mic                {_mark(mic_mod.SoundDeviceMic.available())} "
          f"({'sounddevice' if mic_mod.SoundDeviceMic.available() else 'FakeMic'})\n")
    write(f"  stt.local          {_mark(stt_mod.LocalTranscriber.available())} "
          f"({stt_mod.LocalTranscriber.describe()})\n")
    write(f"  stt.cloud          {_mark(stt_mod.build_cloud(settings) is not None)}\n")
    speaker = tts_mod.build_speaker(settings)
    write(f"  tts                {_mark(speaker.name != 'fake')} ({speaker.name})\n")
    write(f"  llm                {_mark(bool(settings.deepseek_api_key))} ({settings.voice_model})\n")
    write(f"  jev                {_mark(settings.jev_live)}\n")

    loop = loop_mod.build_loop(settings, mic=_null_mic())
    missing = getattr(loop, "missing", [])
    write("\nmodules\n")
    for name, obj in (
        ("jev.gate", loop.gate),
        ("jev.router", loop.router),
        ("jev.risk", loop.risk),
        ("jev.confirm", loop.confirm),
        ("tools.registry", loop.registry),
        ("safety.policy", loop.policy_decide),
        ("safety.undo", loop.journal),
        ("safety.audit", loop.audit),
    ):
        write(f"  {name:<18} {'wired' if obj is not None else 'MISSING'}\n")
    for line in missing:
        write(f"    ! {line}\n")

    if loop.policy_decide is None:
        write("\nsafety policy is not wired: every action will be REFUSED.\n")
    return 0


def _null_mic() -> Any:
    from daa.voice.mic import FakeMic

    return FakeMic()


# ---------------------------------------------------------------------------
# say / listen / undo
# ---------------------------------------------------------------------------


def _build(settings: Settings, *, mic: Any = None, silent: bool = False) -> Any:
    from daa.voice.loop import build_loop
    from daa.voice.tts import FakeSpeaker

    loop = build_loop(settings, mic=mic)
    if silent:
        # --silent swaps the speaker rather than skipping TTS, so the code path
        # under test is still the code path that ships.
        loop.speaker = FakeSpeaker()
    return loop


def _print_outcome(outcome: Any) -> None:
    for line in outcome.spoken:
        print(f"daa> {line}")
    if not outcome.spoken:
        reason = outcome.dropped_reason or "nothing to say"
        print(f"daa> ({reason})")


def cmd_say(args: argparse.Namespace) -> int:
    settings = Settings.load()
    loop = _build(settings, silent=args.silent)
    outcome = loop.handle_text(" ".join(args.text), gated=args.gate, replies=args.reply or ())
    _print_outcome(outcome)
    # Always 0: a dropped or refused utterance is the system working, not a
    # CLI failure. Only an unwired safety policy (see `listen`) is an error.
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    settings = Settings.load()
    from daa.voice.mic import build_mic

    loop = _build(settings, mic=build_mic(settings), silent=args.silent)
    if loop.policy_decide is None:
        print("safety policy not wired; run `daa doctor`. Refusing to listen.", file=sys.stderr)
        return 2
    print("listening. ctrl-c to stop.", file=sys.stderr)
    status = 0
    try:
        loop.run(max_segments=args.max_segments)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- see comment
        # The loop already isolates every stage it knows about; this is the
        # backstop for the one it does not. A traceback here costs the user
        # their whole session -- the mic, the buffered utterance, the lot --
        # and tells them nothing they can act on. Report and exit cleanly.
        print(f"\nthe loop stopped on an unexpected error: {exc}", file=sys.stderr)
        print("run `daa doctor`; this is a bug, please report it.", file=sys.stderr)
        status = 1
    finally:
        if loop.mic is not None:
            loop.mic.close()
        if loop.speaker is not None:
            loop.speaker.stop()
    return status


def cmd_undo(args: argparse.Namespace) -> int:
    settings = Settings.load()
    loop = _build(settings, silent=args.silent)
    if args.list:
        if loop.journal is None:
            print("no undo journal wired.", file=sys.stderr)
            return 2
        history = list(loop.journal.history())
        if not history:
            print("nothing to undo.")
            return 0
        for i, entry in enumerate(reversed(history), 1):
            print(f"{i}. {getattr(entry, 'description', entry)}")
        return 0
    outcome = loop.undo_last(replies=("yes",) if args.yes else ())
    _print_outcome(outcome)
    return 0


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------


def cmd_bridge(args: argparse.Namespace) -> int:
    """Speak the dock protocol on stdin/stdout. Not for humans.

    THE FIRST LINE OF THIS FUNCTION IS THE IMPORTANT ONE. fd 1 is the protocol
    and nothing else: one stray `print()` anywhere in the tree -- ours, a
    dependency's, at import time, on a machine we have never seen -- corrupts
    the stream, and the symptom is a dock that silently stops updating. So the
    real stdout is taken away and given a private handle before anything else
    is imported, and every remaining writer is pointed at stderr, which the
    dock tees to ~/Library/Logs/daa/python.log and to its own stderr.
    """
    # `daa.ui` imports nothing but os and sys, so reaching this function
    # cannot itself have printed. Everything heavier is imported after.
    from daa.ui import steal_stdout

    real_stdout = steal_stdout()

    from daa.ui.bridge import serve

    return serve(real_stdout)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daa", description="voice-driven macOS assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    listen = sub.add_parser("listen", help="run the real voice loop")
    listen.add_argument("--silent", action="store_true", help="don't actually play audio")
    listen.add_argument("--max-segments", type=int, default=None, help="stop after N segments")
    listen.set_defaults(func=cmd_listen)

    say = sub.add_parser("say", help="one-shot through the pipeline, no mic")
    say.add_argument("text", nargs="+")
    say.add_argument("--gate", action="store_true", help="run the address gate on typed text too")
    say.add_argument("--reply", action="append", help="scripted answer to a confirmation prompt")
    say.add_argument("--silent", action="store_true", help="don't actually play audio")
    say.set_defaults(func=cmd_say)

    doctor = sub.add_parser("doctor", help="what is wired, what is live, what is fake")
    doctor.set_defaults(func=cmd_doctor)

    bridge = sub.add_parser(
        "bridge",
        help="speak the dock protocol on stdin/stdout (not for humans)",
    )
    bridge.set_defaults(func=cmd_bridge)

    undo = sub.add_parser("undo", help="reverse the last recorded mutation")
    undo.add_argument("--list", action="store_true", help="show the journal instead of undoing")
    undo.add_argument("--yes", action="store_true",
                      help="pre-answer the spoken confirmation undo always asks for")
    undo.add_argument("--silent", action="store_true", help="don't actually play audio")
    undo.set_defaults(func=cmd_undo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
