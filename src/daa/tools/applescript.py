"""Arbitrary AppleScript / JXA -- the escape hatch, deliberately the hardest to reach.

Floor is CONFIRM_VISUAL and it never moves. Everything else in tools/ has a
fixed shape a user can hold in their head: move_to_trash takes paths, open_app
takes a name. This one takes a program, written by a language model, that can
drive any scriptable app on the machine. A mishearing cannot be caught by
reading the target back, because the target IS the program -- so voice alone is
never enough, and the user sees the code before it runs.

resolve() therefore does static inspection rather than execution: which apps the
script addresses, and which verbs in it are destructive. That feeds the risk
gate with facts about the actual code instead of the sentence that produced it.
"""

from __future__ import annotations

import re
from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult
from daa.tools.base import ShellTool, as_sentence, param, spec, spoken_snippet
from daa.tools.permissions import AUTOMATION, require

OSASCRIPT = "/usr/bin/osascript"

_TELL_APP = re.compile(r'tell\s+application\s+(?:id\s+)?"([^"]+)"', re.IGNORECASE)
_JXA_APP = re.compile(r'Application\(\s*["\']([^"\']+)["\']\s*\)')

# Verbs whose presence changes what this script IS, not merely what it does.
# Surfaced to the safety layer; never used to silently refuse here, because the
# refusal decision belongs to safety/policy.py.
DANGEROUS = {
    "do shell script": "runs a shell command",
    "delete": "deletes items",
    "erase": "erases a disk",
    "empty trash": "empties the Trash",
    "quit application": "quits apps",
    "shut down": "shuts the machine down",
    "restart": "restarts the machine",
    "keystroke": "types keystrokes into whatever is frontmost",
    "key code": "sends raw key codes",
    "set the clipboard": "overwrites the clipboard",
    "system events": "drives the UI directly",
    "password": "mentions a password",
    "administrator privileges": "asks for your admin password",
}

LANGUAGES = {"applescript": "AppleScript", "javascript": "JavaScript", "jxa": "JavaScript"}


def inspect_script(script: str) -> dict[str, Any]:
    """Static read of what a script reaches for. Never executes anything."""
    lowered = script.lower()
    apps = sorted({*(m.group(1) for m in _TELL_APP.finditer(script)),
                   *(m.group(1) for m in _JXA_APP.finditer(script))})
    flags = sorted({reason for needle, reason in DANGEROUS.items() if needle in lowered})
    return {
        "applications": apps,
        "flags": flags,
        "lines": len([ln for ln in script.splitlines() if ln.strip()]),
        "chars": len(script),
    }


class RunAppleScript(ShellTool):
    binary = OSASCRIPT
    timeout_s = 30.0
    mutates = True
    # The target says "a script that controls Mail", so the verb stays bare.
    verb = "run"
    # There is no general inverse of an arbitrary program. Paid for with the
    # strongest confirmation tier rather than papered over with a fake undo --
    # tests/test_undo_coverage.py enforces that trade.
    irreversible = True

    spec = spec(
        "run_applescript",
        "Run an AppleScript or JXA script against scriptable applications.",
        {
            "script": param("string", "The script source to run", required=True),
            "language": param("string", "applescript (default) or javascript"),
            "purpose": param("string", "One line describing what the script is for"),
        },
        floor=RiskTier.CONFIRM_VISUAL,
        activation_hint="""
            last resort scripting for something no other tool covers: drive a scriptable
            Mac app directly, read or change data inside Mail Notes Music Finder Safari
            Keynote Terminal, talk to an app that has no shortcut and no dedicated tool.
            Prefer run_shortcut, open_app or the file tools whenever one of them fits.
            Arbitrary generated code, shown on screen and approved by eye before it runs
            -- never authorised by voice alone, and never undoable.
        """,
        tags=("applescript", "escape-hatch", "irreversible"),
        # An arbitrary program has no inverse; that is what `irreversible` and
        # the CONFIRM_VISUAL floor are paying for.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        script = str(kwargs.get("script") or "")
        language = LANGUAGES.get(str(kwargs.get("language") or "applescript").lower(), "AppleScript")
        purpose = str(kwargs.get("purpose") or "").strip()
        facts = inspect_script(script)

        # `purpose` is written by the MODEL. Using it as the readback asks the
        # user to check the model against a sentence the model wrote: a script
        # that empties an inbox can introduce itself as "check what time my next
        # meeting is", and it did. So the readback is derived only from the code
        # -- the apps it really addresses and the verbs it really contains --
        # and the script itself travels in `args`, where the on-screen
        # confirmation prints it in full. The stated purpose stays in args too,
        # clearly labelled, for the log and for the card; it is never the target.
        apps = facts["applications"]
        target = f"a script that controls {', '.join(apps)}" if apps else "a script"

        consequences: dict[str, str] = {}
        if facts["flags"]:
            consequences["does"] = "the script " + ", ".join(facts["flags"])
        if script.strip():
            consequences["review"] = "you will see the whole script on screen before it runs"

        return self.action(
            targets=[target] if script.strip() else [],
            explicit=bool(kwargs.get("explicit", True)),
            consequences=consequences,
            # The ONLY authoritative description of what will happen. The
            # visual confirmation prints args, so this is what the user reads.
            script=script,
            language=language,
            stated_purpose=purpose,
            purpose=purpose,
            applications=apps,
            flags=facts["flags"],
            lines=facts["lines"],
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        script = str(action.args.get("script") or "")
        if not script.strip():
            return self.failed("There was no script to run.", "empty script")
        language = str(action.args.get("language") or "AppleScript")
        if self.dry_run:
            return self.dry("I would run that script.", language=language)

        apps = list(action.args.get("applications") or [])
        state = require(AUTOMATION)
        # A denied Automation grant makes osascript fail with an opaque -1743.
        # Translate it up front into something the user can act on.
        if state.status == "denied" and apps:
            return self.failed(f"I am not allowed to control {apps[0]}.", state.remedy or state.detail)

        # Script source goes in on stdin: it can be thousands of characters and
        # contain anything, and `-e` would put all of that in an argv slot.
        result = self.sh([self.binary, "-l", language, "-"], mutating=True, input_text=script)
        if not result.ok:
            detail = result.failure_reason
            if "-1743" in detail or "not allowed" in detail.lower():
                return self.failed(
                    "macOS blocked that script.", f"{detail}. {require(AUTOMATION).remedy}"
                )
            return self.failed("That script did not run.", detail)

        output = result.stdout.strip()
        snippet = spoken_snippet(output)
        summary = as_sentence(f"It says {snippet}" if snippet else "Done")
        return ToolResult(
            ok=True, summary=summary,
            data={"output": output, "language": language, "applications": apps},
        )


__all__ = ["DANGEROUS", "RunAppleScript", "inspect_script"]
