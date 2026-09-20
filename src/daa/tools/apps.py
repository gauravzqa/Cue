"""Launching and listing applications.

`open_app` is where the two-phase contract earns its keep. "Open messages" can
mean Messages.app, Message Filtering, or a third-party Messages client, and the
speech recognizer will happily hand over "open massages". So resolve() reads the
real /Applications tree, scores every candidate, and reports the winner TOGETHER
with the runners-up; the confirmation the user hears is the app that will
actually launch, not the words they said.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult
from daa.tools.base import (
    BaseTool,
    Degraded,
    ShellTool,
    param,
    rank_candidates,
    spec,
)

# Two levels deep covers /Applications/Utilities and /System/Applications/Utilities
# without walking a user's entire home directory on every utterance.
APP_DIRS: tuple[str, ...] = (
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/Applications/Utilities",
    "~/Applications",
)

# Below this the best candidate is a guess, not a match, and we refuse rather
# than launch something the user never named.
MATCH_FLOOR = 0.55
# Within this distance of the winner, the runner-up is a genuine ambiguity the
# user should hear about.
AMBIGUOUS_MARGIN = 0.08

# Reverse-DNS bundle identifiers: com.apple.Safari, org.mozilla.firefox.
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}$")


def _app_dirs() -> list[Path]:
    seen: list[Path] = []
    for raw in APP_DIRS:
        path = Path(raw).expanduser()
        if path.is_dir() and path not in seen:
            seen.append(path)
    return seen


def discover_apps(refresh: bool = False) -> list[Path]:
    if refresh:
        _discover_cached.cache_clear()
    return list(_discover_cached())


@lru_cache(maxsize=1)
def _discover_cached() -> tuple[Path, ...]:
    found: dict[str, Path] = {}
    for directory in _app_dirs():
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue  # an unreadable folder costs us that folder, nothing more
        for entry in entries:
            if entry.suffix == ".app":
                found.setdefault(entry.stem.lower(), entry)
            elif entry.is_dir() and not entry.name.startswith("."):
                try:
                    for nested in sorted(entry.iterdir()):
                        if nested.suffix == ".app":
                            found.setdefault(nested.stem.lower(), nested)
                except OSError:
                    continue
    return tuple(found.values())


def app_name(path: str | Path) -> str:
    return Path(path).stem


class OpenApp(ShellTool):
    binary = "/usr/bin/open"
    mutates = True
    verb = "open"

    spec = spec(
        "open_app",
        "Launch or focus a macOS application by name.",
        {
            "name": param("string", "Spoken application name, e.g. 'safari'", required=True),
            "files": param("array", "Optional file paths to open with that app"),
        },
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            open launch start bring up switch to fire up an app or program by name --
            safari, chrome, mail, messages, notes, terminal, spotify, slack, vs code,
            finder, calendar, music, photos, preview, system settings. Use when the user
            names a piece of software and wants it running or in front of them. Also
            'open this file in Preview'. Not for opening a URL or a folder.
        """,
        tags=("apps", "launch"),
        # The inverse of "open" is "quit", and quitting an app the user has
        # since typed into is worse than leaving a window open -- see run().
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        name = str(kwargs.get("name") or "").strip()
        files = [str(f) for f in (kwargs.get("files") or [])]
        explicit = bool(kwargs.get("explicit", True))

        # Reverse-DNS input is an identifier, not a spoken name: it addresses
        # the app exactly, and fuzzy-matching it against .app stems would turn
        # a precise request into a guess.
        if _BUNDLE_ID.match(name):
            return self.action(
                targets=[name], explicit=explicit, name=name, bundle_id=name, path=None,
                alternates=[], files=files, score=1.0, confident=True, ambiguous=False,
            )

        candidates = discover_apps()
        ranked = rank_candidates(name, candidates, key=lambda p: p.stem, limit=5)
        if not ranked:
            return self.action(
                targets=[], explicit=explicit, name=name, path=None, alternates=[],
                files=files, score=0.0, reason="no installed application matched",
            )

        score, best = ranked[0]
        if score < MATCH_FLOOR:
            # Below the floor there is no resolved target, so there is nothing
            # for the confirmation to read back -- which is the correct outcome.
            return self.action(
                targets=[], explicit=explicit, name=name, path=None, files=files,
                score=round(score, 3), confident=False, ambiguous=False,
                alternates=[{"name": p.stem, "path": str(p), "score": round(s, 3)}
                            for s, p in ranked],
                reason="no installed application was close enough",
            )
        # Only report runners-up that are plausible. Listing every weak match
        # turns a useful "or did you mean..." into noise the user tunes out.
        alternates = [
            {"name": p.stem, "path": str(p), "score": round(s, 3)}
            for s, p in ranked[1:]
            if s >= max(MATCH_FLOOR, score - 0.25)
        ]
        # Near-ties are the dangerous case: report them so the spoken
        # confirmation can be "Safari, or did you mean Safari Technology Preview?"
        # An exact name match is never a tie, however close the runner-up scores.
        ambiguous = bool(alternates) and score < 1.0 and (score - ranked[1][0]) < AMBIGUOUS_MARGIN
        # A fuzzy match with live runners-up is the resolver CHOOSING, not the
        # user naming. Saying so is what lets policy ask before acting on a
        # guess -- and it can only ever lower a caller's claim, never raise it.
        guessed = score < 1.0 and bool(alternates)
        return self.action(
            targets=[best.stem],
            explicit=explicit and not guessed,
            name=name,
            path=str(best),
            app=best.stem,
            score=round(score, 3),
            confident=score >= MATCH_FLOOR and not ambiguous,
            ambiguous=ambiguous,
            alternates=alternates,
            files=files,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        path = action.args.get("path")
        bundle_id = action.args.get("bundle_id")
        if not path and not bundle_id:
            return self.failed(
                "I could not find an app by that name.",
                action.args.get("reason", "no match"),
                query=action.args.get("name"),
            )
        if path and float(action.args.get("score", 0.0)) < MATCH_FLOOR:
            return self.failed(
                "I am not sure which app you meant.",
                "best match below confidence floor",
                best=action.args.get("app"),
                alternates=action.args.get("alternates", []),
            )

        label = action.args.get("app") or bundle_id or "that app"
        argv = [self.binary, "-b", bundle_id] if bundle_id else [self.binary, "-a", str(path)]
        argv += [str(f) for f in action.args.get("files", [])]

        result = self.sh(argv, mutating=True)
        if result.skipped:
            return self.dry(f"I would open {label}.", app=label)
        if not result.ok:
            return self.failed(f"I could not open {label}.", result.failure_reason, app=label)
        # No UndoAction on purpose: the inverse of "open" is "quit", and quitting
        # an app the user has since typed into is a worse outcome than leaving a
        # window open. ANNOUNCE floor means the user hears what happened anyway.
        return ToolResult(ok=True, summary=f"Opened {label}.", data={"app": label, "path": path})


class ListRunningApps(BaseTool):
    verb = "list the apps that are running"

    spec = spec(
        "list_running_apps",
        "List applications currently running with a user interface.",
        {"include_background": param("boolean", "Include agents without a UI")},
        floor=RiskTier.SILENT,
        activation_hint="""
            what apps are open running right now, which programs am I running, list open
            applications, is slack open, am I still in a zoom call, what is currently
            frontmost active in focus. Read-only inventory of running software. Use before
            switching or quitting so the user hears real names.
        """,
        tags=("apps", "read"),
        # Read-only inventory; nothing to invert.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return self.action(
            targets=[], explicit=bool(kwargs.get("explicit", True)),
            include_background=bool(kwargs.get("include_background", False)),
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        try:
            from AppKit import (
                NSApplicationActivationPolicyRegular,  # type: ignore
                NSWorkspace,  # type: ignore
            )
        except Exception as exc:  # noqa: BLE001
            return Degraded("appkit", "I need the macOS app frameworks for that.", str(exc)).as_result(
                "I cannot see running apps on this machine."
            )

        include_background = bool(action.args.get("include_background", False))
        apps: list[dict[str, Any]] = []
        try:
            running: Iterable[Any] = NSWorkspace.sharedWorkspace().runningApplications()
            frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
            front_pid = int(frontmost.processIdentifier()) if frontmost else -1
            for app in running:
                policy = int(app.activationPolicy())
                if not include_background and policy != int(NSApplicationActivationPolicyRegular):
                    continue
                apps.append(
                    {
                        "name": str(app.localizedName() or ""),
                        "bundle_id": str(app.bundleIdentifier() or ""),
                        "pid": int(app.processIdentifier()),
                        "active": int(app.processIdentifier()) == front_pid,
                        "hidden": bool(app.isHidden()),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return self.failed("I could not read the list of running apps.", str(exc))

        apps.sort(key=lambda a: a["name"].lower())
        front = next((a["name"] for a in apps if a["active"]), "")
        if not apps:
            return ToolResult(ok=True, summary="Nothing seems to be running.", data={"apps": []})
        tail = f", and you are in {front}" if front else ""
        return ToolResult(
            ok=True,
            summary=f"{len(apps)} apps are open{tail}.",
            data={"apps": apps, "frontmost": front, "count": len(apps)},
        )


def find_app(name: str) -> Path | None:
    """Convenience for other tools that need an app path, not a full resolve."""
    ranked = rank_candidates(name, discover_apps(), key=lambda p: p.stem, limit=1)
    if not ranked or ranked[0][0] < MATCH_FLOOR:
        return None
    return ranked[0][1]


__all__ = [
    "APP_DIRS",
    "ListRunningApps",
    "OpenApp",
    "app_name",
    "discover_apps",
    "find_app",
]
