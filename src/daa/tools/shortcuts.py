"""User-authored Shortcuts -- the highest-leverage surface on macOS.

One Automation grant for the Shortcuts app buys sanctioned, user-authored access
to Messages, Notes, Reminders, Calendar, HomeKit, Focus modes, Photos and the
rest, instead of a separate per-app Automation prompt (and a separate scripting
dictionary to get wrong) for each one. The user already decided what each
Shortcut is allowed to do when they built it; we only decide WHICH one runs.

Which is why resolve() matches the spoken name against the real installed list
before anything executes. "Run send it" should never fire "Send Everything",
and the confirmation the user hears is the shortcut's actual title.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult
from daa.tools.base import ShellTool, as_sentence, param, rank_candidates, spec, spoken_snippet

SHORTCUTS_BIN = "/usr/bin/shortcuts"

# A shortcut may legitimately drive several apps, so give it room -- but never
# forever, because a shortcut waiting on a dialog would otherwise hold the
# voice loop open indefinitely.
RUN_TIMEOUT_S = 90.0
MATCH_FLOOR = 0.55
# How much of the input a shortcut will be handed can be read aloud. Long
# enough that a whole text message survives intact; capped so a pasted essay
# cannot hold the confirmation open forever.
SPOKEN_INPUT_CHARS = 240


def spoken_input(text: str) -> str:
    """The body, flattened, for the sentence the user answers.

    A shortcut named "Text My Partner" tells the user WHO is about to be
    messaged and nothing at all about WHAT. The message is composed by a
    language model and sent to another human, and CONFIRM_VOICE means a spoken
    yes sends it -- so the body is part of the confirmation, not an argument.
    """
    flat = " ".join(str(text).split())
    if len(flat) <= SPOKEN_INPUT_CHARS:
        return flat
    return flat[:SPOKEN_INPUT_CHARS].rstrip() + "..."


def list_installed(timeout: float = 15.0) -> tuple[list[str], str]:
    """Returns (names, error). Never raises; an empty list is a valid answer."""
    from daa.tools.base import run_argv

    result = run_argv([SHORTCUTS_BIN, "list"], timeout=timeout, mutating=False)
    if not result.ok:
        return [], result.failure_reason
    return result.lines(), ""


class ListShortcuts(ShellTool):
    binary = SHORTCUTS_BIN
    timeout_s = 15.0
    verb = "list your shortcuts"

    spec = spec(
        "list_shortcuts",
        "List the Shortcuts installed on this Mac.",
        {"folder": param("string", "Only list shortcuts in this Shortcuts folder")},
        floor=RiskTier.SILENT,
        activation_hint="""
            what shortcuts do I have, list my shortcuts, what automations can you run,
            what can you do with shortcuts, do I have a shortcut for that, show my
            shortcut names and folders. Read-only inventory. Use before running one, and
            whenever the user asks what the assistant is able to trigger.
        """,
        tags=("shortcuts", "read"),
        # Read-only inventory; nothing to invert.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        folder = kwargs.get("folder")
        argv = [self.binary, "list"]
        if folder:
            argv += ["--folder", str(folder)]
        return self.action(
            targets=[], explicit=bool(kwargs.get("explicit", True)), argv=argv, folder=folder
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        result = self.sh(list(action.args.get("argv") or [self.binary, "list"]), mutating=False)
        if not result.ok:
            return self.failed("I could not read your shortcuts.", result.failure_reason)
        names = result.lines()
        if not names:
            return ToolResult(
                ok=True, summary="You do not have any shortcuts yet.",
                data={"shortcuts": [], "count": 0},
            )
        head = ", ".join(names[:3])
        more = f", and {len(names) - 3} more" if len(names) > 3 else ""
        return ToolResult(
            ok=True,
            summary=f"You have {len(names)} shortcuts, including {head}{more}.",
            data={"shortcuts": names, "count": len(names)},
        )


class RunShortcut(ShellTool):
    binary = SHORTCUTS_BIN
    timeout_s = RUN_TIMEOUT_S
    mutates = True
    verb = "run the shortcut"
    # A shortcut is arbitrary user-authored automation: it can send a message or
    # turn off the lights, and there is no inverse we could synthesise. Declared
    # here so test_undo_coverage can hold it to the stricter rule instead of
    # letting a missing undo pass unnoticed.
    irreversible = True

    spec = spec(
        "run_shortcut",
        "Run one of the user's own Shortcuts, optionally passing it text input.",
        {
            "name": param("string", "Shortcut name as spoken", required=True),
            "input_text": param("string", "Text to pass in as the shortcut's input"),
            "capture_output": param("boolean", "Read back whatever the shortcut returns"),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            run trigger fire execute my shortcut automation macro by name, do my morning
            routine, start focus mode, log my weight, send my standup, add to my reading
            list, turn off the lights, text my partner I'm on the way. Anything the user
            has built in the Shortcuts app -- Messages, Notes, Reminders, Calendar, Home,
            Focus, Photos, Music -- reached through one sanctioned entry point. Cannot be
            undone, so the shortcut's real name is confirmed first.
        """,
        tags=("shortcuts", "automation", "irreversible"),
        # There is no inverse of "the lights went off and a message was sent".
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        spoken = str(kwargs.get("name") or "").strip()
        input_text = kwargs.get("input_text")
        # Whatever the shortcut is about to be handed, said out loud.
        consequences = (
            {"input": f'sending this text: "{spoken_input(input_text)}"'}
            if input_text is not None and str(input_text).strip()
            else {}
        )
        installed, error = list_installed()
        ranked = rank_candidates(spoken, installed, limit=4)

        if not ranked:
            return self.action(
                targets=[], explicit=bool(kwargs.get("explicit", True)),
                consequences=consequences,
                name=spoken, resolved_name=None, alternates=[], score=0.0,
                installed_count=len(installed), list_error=error or None,
                input_text=input_text,
                capture_output=bool(kwargs.get("capture_output", True)),
                reason="no installed shortcut matched" if not error else error,
            )

        score, best = ranked[0]
        alternates = [name for _, name in ranked[1:]]
        if score < MATCH_FLOOR:
            # A weak match must not become a spoken target. "Run launch the
            # rocket" confirmed back as "Morning Routine" is how a user says yes
            # to something they never asked for.
            return self.action(
                targets=[], explicit=bool(kwargs.get("explicit", True)),
                consequences=consequences,
                name=spoken, resolved_name=None, score=round(score, 3), confident=False,
                alternates=[best, *alternates], installed_count=len(installed),
                list_error=error or None, input_text=input_text,
                capture_output=bool(kwargs.get("capture_output", True)),
                reason="no shortcut name was close enough",
            )
        return self.action(
            targets=[best],
            # Anything short of the shortcut's exact name is the resolver
            # picking one, not the user naming it -- and this tool cannot be
            # taken back, so policy should get to say so.
            explicit=bool(kwargs.get("explicit", True)) and score >= 1.0,
            consequences=consequences,
            name=spoken,
            resolved_name=best,
            score=round(score, 3),
            confident=score >= MATCH_FLOOR,
            alternates=alternates,
            installed_count=len(installed),
            list_error=error or None,
            input_text=input_text,
            capture_output=bool(kwargs.get("capture_output", True)),
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        name = action.args.get("resolved_name")
        if not name:
            return self.failed(
                "I could not find a shortcut by that name.",
                str(action.args.get("reason") or "no match"),
                query=action.args.get("name"),
            )
        if float(action.args.get("score", 0.0)) < MATCH_FLOOR:
            return self.failed(
                "I am not sure which shortcut you meant.",
                "best match below confidence floor",
                best=name, alternates=action.args.get("alternates", []),
            )
        if self.dry_run:
            return self.dry(f"I would run {name}.", shortcut=name)

        input_text = action.args.get("input_text")
        capture = bool(action.args.get("capture_output", True))
        argv = [self.binary, "run", str(name)]

        with tempfile.TemporaryDirectory(prefix="daa-shortcut-") as tmp:
            tmp_dir = Path(tmp)
            if input_text is not None:
                # Passed as a file rather than an argument: shortcut input is
                # arbitrary user text and has no business in an argv slot.
                in_path = tmp_dir / "input.txt"
                in_path.write_text(str(input_text), encoding="utf-8")
                argv += ["--input-path", str(in_path)]
            out_path = tmp_dir / "output.txt"
            if capture:
                argv += ["--output-path", str(out_path), "--output-type", "public.utf8-plain-text"]

            result = self.sh(argv, mutating=True)
            output = ""
            if capture and out_path.exists():
                try:
                    output = out_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    output = ""

        if not result.ok:
            return self.failed(f"{name} did not finish.", result.failure_reason, shortcut=name)
        # No UndoAction: see `irreversible` above. The honest signal to the loop
        # is undo=None plus the irreversible flag on the tool, not a fake inverse.
        snippet = spoken_snippet(output)
        summary = as_sentence(f"{name} says {snippet}" if snippet else f"{name} finished")
        return ToolResult(
            ok=True, summary=summary,
            data={"shortcut": name, "output": output, "stdout": result.stdout.strip()},
        )


__all__ = ["SHORTCUTS_BIN", "ListShortcuts", "RunShortcut", "list_installed", "spoken_input"]
