"""Enumerating and focusing windows.

Two different TCC grants meet here, and conflating them produces a tool that
looks broken when it is merely blinkered:

  Quartz CGWindowList  gives owner, geometry and window id to ANY process.
                       Window TITLES are redacted without Screen Recording.
  Accessibility (AX)   is the only way to raise one specific window of an app.
                       Without it, the best we can do is activate the app.

So list_windows degrades to app-level answers rather than failing, and says so
in its data; focus_window falls back to activating the owning application and
tells the user what it could not do. A revoked grant costs a capability, never
the process.
"""

from __future__ import annotations

from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, UndoAction
from daa.tools.base import BaseTool, Degraded, param, rank_candidates, spec
from daa.tools.permissions import ACCESSIBILITY, SCREEN_RECORDING, require

# Layer 0 is the normal document layer. Anything above it is a menu bar extra,
# a dock tile or a status item -- never something a user asks to switch to.
NORMAL_LAYER = 0
MIN_USEFUL_AREA = 40 * 40
# Fuzzy matching against window titles is noisy -- a six-letter app name will
# weakly "match" almost anything. Below this a candidate is a coincidence, and
# focusing the wrong window is the one mistake a user notices immediately.
MATCH_FLOOR = 0.55


def _window_rows() -> tuple[list[dict[str, Any]], str]:
    try:
        import Quartz  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return [], f"Quartz unavailable: {exc}"
    try:
        raw = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)

    rows: list[dict[str, Any]] = []
    for entry in raw or []:
        try:
            if int(entry.get("kCGWindowLayer", 1)) != NORMAL_LAYER:
                continue
            bounds = entry.get("kCGWindowBounds") or {}
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            if width * height < MIN_USEFUL_AREA:
                continue
            rows.append(
                {
                    "app": str(entry.get("kCGWindowOwnerName") or ""),
                    "title": str(entry.get("kCGWindowName") or ""),
                    "window_id": int(entry.get("kCGWindowNumber", 0)),
                    "pid": int(entry.get("kCGWindowOwnerPID", 0)),
                    "bounds": {
                        "x": float(bounds.get("X", 0)), "y": float(bounds.get("Y", 0)),
                        "width": width, "height": height,
                    },
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: (r["app"].lower(), r["title"].lower()))
    return rows, ""


def _label(row: dict[str, Any]) -> str:
    title = row.get("title") or ""
    app = row.get("app") or "a window"
    return f"{title} in {app}" if title else app


class ListWindows(BaseTool):
    verb = "list the windows that are open"

    spec = spec(
        "list_windows",
        "List the on-screen windows, with their app and title where permitted.",
        {"app": param("string", "Only windows belonging to this application")},
        floor=RiskTier.SILENT,
        activation_hint="""
            what windows are open on screen, which tabs documents do I have up, list open
            windows for chrome safari finder, how many windows of that app, what am I
            looking at, find the window called something. Read-only. Window titles need
            Screen Recording permission; without it this still reports which apps have
            windows open.
        """,
        tags=("windows", "read"),
        # Read-only; nothing to invert.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return self.action(
            targets=[], explicit=bool(kwargs.get("explicit", True)),
            app=kwargs.get("app") or None,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        rows, error = _window_rows()
        if error:
            return Degraded("quartz", "I cannot see the windows on this machine.", error).as_result(
                "I could not look at your windows."
            )
        app_filter = action.args.get("app")
        if app_filter:
            ranked = rank_candidates(str(app_filter), rows, key=lambda r: r["app"], limit=50)
            rows = [row for score, row in ranked if score >= MATCH_FLOOR]

        titles_visible = require(SCREEN_RECORDING).granted
        data: dict[str, Any] = {
            "windows": rows,
            "count": len(rows),
            "titles_available": titles_visible,
        }
        if not titles_visible:
            data["remedy"] = require(SCREEN_RECORDING).remedy

        if not rows:
            return ToolResult(ok=True, summary="I do not see any open windows.", data=data)
        apps = sorted({r["app"] for r in rows if r["app"]})
        head = ", ".join(apps[:3])
        more = f" and {len(apps) - 3} more" if len(apps) > 3 else ""
        return ToolResult(
            ok=True,
            summary=f"{len(rows)} windows are open, across {head}{more}.",
            data=data,
        )


def _activate_pid(pid: int) -> tuple[bool, str]:
    try:
        from AppKit import (  # type: ignore
            NSApplicationActivateAllWindows,  # type: ignore
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"pyobjc unavailable: {exc}"
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None:
            return False, f"no running app with pid {pid}"
        ok = bool(
            app.activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
            )
        )
        return ok, "" if ok else "the app refused to come forward"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _raise_window(pid: int, title: str) -> tuple[bool, str]:
    """Raise one specific window. Requires Accessibility; degrades to False."""
    try:
        from ApplicationServices import (  # type: ignore
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementPerformAction,
            AXUIElementSetAttributeValue,
            kAXMainAttribute,
            kAXRaiseAction,
            kAXTitleAttribute,
            kAXWindowsAttribute,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"accessibility api unavailable: {exc}"
    try:
        element = AXUIElementCreateApplication(int(pid))
        err, windows = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute, None)
        if err != 0 or not windows:
            return False, "the app did not expose its windows"
        chosen = None
        if title:
            for win in windows:
                werr, wtitle = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
                if werr == 0 and wtitle and str(wtitle) == title:
                    chosen = win
                    break
        chosen = chosen if chosen is not None else windows[0]
        AXUIElementSetAttributeValue(chosen, kAXMainAttribute, True)
        raised = AXUIElementPerformAction(chosen, kAXRaiseAction)
        return raised == 0, "" if raised == 0 else "the window would not come forward"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _frontmost() -> dict[str, Any] | None:
    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return {"app": str(app.localizedName() or ""), "pid": int(app.processIdentifier())}
    except Exception:  # noqa: BLE001
        return None


class FocusWindow(BaseTool):
    mutates = True
    verb = "switch to"

    spec = spec(
        "focus_window",
        "Bring a specific window, or an application's window, to the front.",
        {
            "app": param("string", "Application name, e.g. 'chrome'"),
            "title": param("string", "Window title to match"),
            "window_id": param("integer", "Exact window id from list_windows"),
        },
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            switch to focus bring forward go back to that window, put chrome in front,
            show me the document I had open, jump to slack, front and center, come back to
            my editor, activate that app's window. Raises one window rather than launching
            anything. Needs Accessibility permission to pick a specific window; otherwise
            it just brings the app forward.
        """,
        tags=("windows", "focus"),
        # Undo is another focus_window: put the user back where they were.
        inverses=("focus_window",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        rows, error = _window_rows()
        window_id = kwargs.get("window_id")
        app = str(kwargs.get("app") or "").strip()
        title = str(kwargs.get("title") or "").strip()
        explicit = bool(kwargs.get("explicit", True))

        if window_id is None and not app and not title:
            # Nothing to resolve. Guessing "the first window" here would produce a
            # confident-sounding confirmation for a target the user never named.
            return self.action(
                targets=[], explicit=explicit, app=None, title=None, window_id=None,
                pid=None, alternates=[], reason="you did not say which window",
            )
        if window_id is not None:
            match = next((r for r in rows if r["window_id"] == int(window_id)), None)
            candidates = [match] if match else []
        else:
            # App first, then title within it. Doing it the other way round lets a
            # stray title match drag in a window from an app the user never named.
            pool = rows
            if app:
                pool = [
                    row
                    for score, row in rank_candidates(app, rows, key=lambda r: r["app"], limit=50)
                    if score >= MATCH_FLOOR
                ]
            if title:
                ranked = rank_candidates(
                    title, pool, key=lambda r: r["title"] or r["app"], limit=4
                )
                candidates = [row for score, row in ranked if score >= MATCH_FLOOR]
                # Screen Recording redacts titles; falling back to the app's own
                # windows beats telling the user nothing is open when it is.
                if not candidates and app:
                    candidates = pool[:4]
            else:
                candidates = pool[:4]

        if not candidates:
            return self.action(
                targets=[], explicit=explicit, app=app or None, title=title or None,
                window_id=None, pid=None, alternates=[],
                reason=error or "no matching window is open",
            )
        best = candidates[0]
        # An exact window id is the user (or list_windows) naming one window.
        # Anything else is a fuzzy match, possibly against a title Screen
        # Recording redacted, possibly with rivals -- a guess, and said to be.
        exact_title = bool(title) and best.get("title") == title
        guessed = window_id is None and not exact_title and (
            len(candidates) > 1 or bool(title)
        )
        return self.action(
            targets=[_label(best)],
            explicit=explicit and not guessed,
            app=best["app"],
            title=best["title"],
            window_id=best["window_id"],
            pid=best["pid"],
            alternates=[_label(r) for r in candidates[1:]],
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        pid = action.args.get("pid")
        if not pid:
            return self.failed(
                "I could not find that window.", str(action.args.get("reason") or "no match"),
                app=action.args.get("app"),
            )
        label = action.targets[0] if action.targets else str(action.args.get("app") or "that window")
        if self.dry_run:
            return self.dry(f"I would switch to {label}.", app=action.args.get("app"))

        previous = _frontmost()
        ax = require(ACCESSIBILITY)
        raised, raise_error = (False, ax.remedy)
        if ax.granted:
            raised, raise_error = _raise_window(int(pid), str(action.args.get("title") or ""))

        activated, activate_error = _activate_pid(int(pid))
        if not activated and not raised:
            return Degraded(
                "focus", ax.remedy if not ax.granted else "That app would not come forward.",
                activate_error or raise_error,
            ).as_result(f"I could not switch to {label}.")

        # Undo is honest and cheap here: put the user back in whatever they were
        # in. Skipped when we were already there, so "undo" never becomes a no-op
        # the user has to discover by trying it.
        undo = (
            UndoAction(
                description=f"go back to {previous['app']}",
                tool="focus_window",
                args={"app": previous["app"], "window_id": None},
            )
            if previous and previous.get("pid") != int(pid)
            else None
        )
        return ToolResult(
            ok=True,
            summary=f"Switched to {label}.",
            data={
                "app": action.args.get("app"),
                "window_id": action.args.get("window_id"),
                "raised_specific_window": raised,
                "accessibility": ax.granted,
                "remedy": None if ax.granted else ax.remedy,
            },
            undo=undo,
        )


__all__ = ["FocusWindow", "ListWindows"]
