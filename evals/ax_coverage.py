"""How much of the user's real Mac can computer use actually name?

The number that decides whether `ui_click` is a product or a demo is not
measured anywhere else: of the apps someone really has open, what fraction
expose an accessibility tree with controls that `naming.compose_target` can
turn into a sentence? This walks every running regular app, READ-ONLY, and
reports per app:

  * reachable, or the raw AXError (-25211 means THIS process has no grant)
  * nodes walked in its windows, and whether daa's caps truncated the walk
  * actionable elements (a press-like action, or a text-entry role)
  * nameable actionable elements (compose_target returns a sentence)
  * nameable menu-bar items, with the system Apple menu counted separately
  * whether the app is Chromium/Electron (its tree is off until a client flips
    Electron's manual-accessibility switch, which daa's tools never do), and
    whether daa's own name-based hint agrees

    .venv/bin/python -m evals.ax_coverage            # table
    .venv/bin/python -m evals.ax_coverage --json     # machine-readable

Three rules, enforced structurally rather than promised:

1. **Read-only.** All AX access goes through `ReadOnlyAX`, an allow-list
   facade: create an application element, set the messaging timeout, copy an
   attribute, list action NAMES, unpack a point. Anything else -- perform an
   action, set an attribute, raise, focus -- raises `ReadOnlyViolation` before
   it reaches macOS. `tests/test_computer_live_coverage.py` also scans this
   file's source for the write APIs. (A read can still make an app do lazy
   work -- AppKit builds a menu the first time its children are read -- but
   nothing is pressed, typed, focused, raised or activated.)
2. **No user content leaves the walk.** Labels are read, because nameability
   is a property of the label, and then dropped: a report row carries counts,
   AX role names and the bundle id. Never a label, never a window title, never
   an app's display name. "Re: layoffs -- Mail" is the user's business.
   `AXValue` is refused by the facade, as it is everywhere else in daa.
3. **No grant, no traceback.** Without Accessibility every app reports
   `-25211` and the run ends with one line saying how to grant it. It never
   prompts: the trust probe is `AXIsProcessTrusted`, the non-prompting form.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from daa.tools.computer import ax
from daa.tools.computer.naming import compose_target

# Deeper and wider than the tools' own caps would under-report what an app
# offers; the SAME caps report what the tools would actually see. Default to
# the tools' caps -- that is the question -- and say when they truncated.
DEFAULT_MAX_DEPTH = ax.MAX_DEPTH
DEFAULT_MAX_NODES = ax.MAX_NODES
DEFAULT_BUDGET_S = 3.0

GRANT_LINE = (
    "Accessibility is not granted to {exe}. To measure coverage, add it under System "
    "Settings > Privacy & Security > Accessibility, then run this again."
)


# ---------------------------------------------------------------------------
# Rule 1: the read-only facade
# ---------------------------------------------------------------------------


class ReadOnlyViolation(RuntimeError):
    """Something tried to use a non-read AX API through the coverage walk."""


READ_FUNCTIONS = frozenset(
    {
        "AXIsProcessTrusted",
        "AXUIElementCreateApplication",
        "AXUIElementSetMessagingTimeout",   # bounds OUR calls; changes nothing in the app
        "AXUIElementCopyAttributeValue",
        "AXUIElementCopyActionNames",       # the NAMES of actions; performs none
        "AXValueGetValue",                  # unpacks a CGPoint/CGSize locally
    }
)
# User content, and the attributes that address it. Refused by name.
FORBIDDEN_ATTRIBUTES = frozenset(
    {"AXValue", "AXSelectedText", "AXSelectedTextRange", "AXVisibleCharacterRange",
     "AXValueDescription"}
)
# Their pyobjc constant names, derived rather than spelled, so that the source
# scan in the tests can insist the literal never appears in this file.
FORBIDDEN_CONSTANTS = frozenset(f"k{name}Attribute" for name in FORBIDDEN_ATTRIBUTES)


class ReadOnlyAX:
    """An allow-list view of `ApplicationServices`. Reads only."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_CONSTANTS:
            raise ReadOnlyViolation(f"{name} addresses user content")
        if name.startswith("kAX") and name.endswith(("Attribute", "Type")):
            return getattr(self._module, name)
        if name == "AXUIElementCopyAttributeValue":
            return self._copy
        if name in READ_FUNCTIONS:
            return getattr(self._module, name)
        raise ReadOnlyViolation(f"{name} is not a read, and the coverage walk only reads")

    def _copy(self, ref: Any, attribute: Any, out: Any) -> Any:
        if str(attribute) in FORBIDDEN_ATTRIBUTES:
            raise ReadOnlyViolation(f"{attribute} is user content")
        return self._module.AXUIElementCopyAttributeValue(ref, attribute, out)


def load_api() -> ReadOnlyAX | None:
    if sys.platform != "darwin":
        return None
    try:
        import ApplicationServices  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return ReadOnlyAX(ApplicationServices)


# ---------------------------------------------------------------------------
# Chromium / Electron, by what is in the bundle rather than what it is called
# ---------------------------------------------------------------------------

_CHROMIUM_FRAMEWORK_HINTS = ("electron", "chromium", "chrome", "browser framework", "edge framework")
_CHROMIUM_MARKERS = ("icudtl.dat", "chrome_100_percent.pak", "resources.pak")


def bundle_path(pid: int) -> str:
    try:
        from AppKit import NSRunningApplication  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    try:
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        url = running.bundleURL() if running is not None else None
        return str(url.path()) if url is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def is_chromium_family(path: str) -> bool:
    """Does the app bundle ship a Chromium or Electron framework?

    By content, not by name: Electron apps may rename the framework to
    "<Product> Framework.framework" (Codex does), so a framework that carries
    Chromium's ICU data file or .pak resources counts too.
    """
    if not path:
        return False
    frameworks = Path(path) / "Contents" / "Frameworks"
    try:
        entries = [p for p in frameworks.iterdir() if p.name.lower().endswith(".framework")]
    except OSError:
        return False
    for fw in entries:
        if any(h in fw.name.lower() for h in _CHROMIUM_FRAMEWORK_HINTS):
            return True
        for resources in (fw / "Resources", fw / "Versions" / "Current" / "Resources"):
            if any((resources / marker).exists() for marker in _CHROMIUM_MARKERS):
                return True
    return False


# ---------------------------------------------------------------------------
# One app
# ---------------------------------------------------------------------------


@dataclass
class AppCoverage:
    """One row. Every field is a count, a flag, a role name or the bundle id."""

    bundle_id: str
    pid: int
    chromium: bool = False
    daa_manual_hint: bool = False
    reachable: bool = False
    ax_error: int = 0
    windows: int = 0
    nodes: int = 0
    actionable: int = 0
    nameable: int = 0
    menu_nameable: int = 0
    apple_menu_items: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    unnameable_roles: dict[str, int] = field(default_factory=dict)
    nameable_roles: dict[str, int] = field(default_factory=dict)

    @property
    def fraction(self) -> float:
        return self.nameable / self.actionable if self.actionable else 0.0


def _role_key(role: str) -> str:
    # Roles are developer-written constants, but a custom role is still a
    # string an app chose; only the AX vocabulary is echoed.
    return role if role.startswith("AX") and role.isascii() and len(role) <= 40 else "custom"


def _actionable(el: ax.Element) -> bool:
    return any(a in ax.PRESSABLE_ACTIONS for a in el.actions) or el.is_text_entry


def walk_app(
    app: ax.AppInfo,
    api: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    budget_s: float = DEFAULT_BUDGET_S,
    chromium: bool = False,
) -> AppCoverage:
    """Walk one app read-only. Never raises for an AX failure."""
    row = AppCoverage(
        bundle_id=app.bundle_id or f"pid:{app.pid}",
        pid=app.pid,
        chromium=chromium,
        daa_manual_hint=ax.wants_manual_accessibility(app),
    )
    started = time.monotonic()
    try:
        app_ref = api.AXUIElementCreateApplication(int(app.pid))
        try:
            api.AXUIElementSetMessagingTimeout(app_ref, ax.MESSAGING_TIMEOUT_S)
        except ReadOnlyViolation:
            raise
        except Exception:  # noqa: BLE001,S110 - advisory
            pass
        err, windows = api.AXUIElementCopyAttributeValue(app_ref, api.kAXWindowsAttribute, None)
    except ReadOnlyViolation:
        raise
    except Exception:  # noqa: BLE001
        row.ax_error = ax.AX_CANNOT_COMPLETE
        return row
    if int(err) != ax.AX_SUCCESS:
        row.ax_error = int(err)
        row.elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        return row

    row.reachable = True
    deadline = started + max(0.05, float(budget_s))
    unnameable: Counter[str] = Counter()
    nameable: Counter[str] = Counter()

    def visit(ref: Any, depth: int, path: tuple[int, ...]) -> ax.Element:
        # window_title is deliberately empty: this walk never reads a title
        # of a window, and nameability does not depend on it.
        return ax._element(
            api, ref, app=app, depth=depth, path=path, window_title="",
            window_subrole="", in_alert=False, default_identity=None,
        )

    windows = list(windows or [])
    row.windows = len(windows)
    for w_index, win in enumerate(windows):
        stack: list[tuple[Any, int, tuple[int, ...]]] = [(win, 0, (w_index,))]
        while stack:
            if row.nodes >= max_nodes or time.monotonic() > deadline:
                row.truncated = True
                break
            ref, depth, path = stack.pop()
            row.nodes += 1
            if depth > 0:
                el = visit(ref, depth, path)
                if _actionable(el):
                    row.actionable += 1
                    if compose_target(el) is not None:
                        row.nameable += 1
                        nameable[_role_key(el.role)] += 1
                    else:
                        unnameable[_role_key(el.role)] += 1
            if depth >= max_depth:
                row.truncated = True
                continue
            cerr, children = api.AXUIElementCopyAttributeValue(ref, api.kAXChildrenAttribute, None)
            if cerr == ax.AX_SUCCESS and children:
                for c_index, child in reversed(list(enumerate(children))):
                    stack.append((child, depth + 1, (*path, c_index)))

    merr, menu_bar = api.AXUIElementCopyAttributeValue(app_ref, api.kAXMenuBarAttribute, None)
    if merr == ax.AX_SUCCESS and menu_bar is not None:
        menu_nodes = 0
        stack = [(menu_bar, 0, (-1,))]
        while stack and menu_nodes < ax.MAX_MENU_NODES and time.monotonic() <= deadline:
            ref, depth, path = stack.pop()
            menu_nodes += 1
            if depth > 0:
                el = visit(ref, depth, path)
                if _actionable(el) and compose_target(el) is not None:
                    # The first menu-bar item is the system Apple menu: Restart,
                    # Shut Down, Log Out <name>, Recent Items. Counted apart --
                    # it is the same in every app and it is not the app's.
                    if path[:2] == (-1, 0):
                        row.apple_menu_items += 1
                    else:
                        row.menu_nameable += 1
            if depth >= ax.MAX_MENU_DEPTH:
                continue
            cerr, children = api.AXUIElementCopyAttributeValue(ref, api.kAXChildrenAttribute, None)
            if cerr == ax.AX_SUCCESS and children:
                for c_index, child in reversed(list(enumerate(children))):
                    stack.append((child, depth + 1, (*path, c_index)))

    row.unnameable_roles = dict(unnameable.most_common())
    row.nameable_roles = dict(nameable.most_common())
    row.elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    return row


# ---------------------------------------------------------------------------
# All apps, and the report
# ---------------------------------------------------------------------------


def _error_name(code: int) -> str:
    return {
        ax.AX_API_DISABLED: "kAXErrorAPIDisabled (this process has no Accessibility grant)",
        ax.AX_NOT_IMPLEMENTED: "kAXErrorNotImplemented (app answers no AX requests)",
        ax.AX_CANNOT_COMPLETE: "kAXErrorCannotComplete (busy or timed out)",
        ax.AX_ATTRIBUTE_UNSUPPORTED: "kAXErrorAttributeUnsupported (no windows attribute)",
    }.get(code, "AXError")


def survey(
    apps: Sequence[ax.AppInfo],
    api: Any,
    *,
    chromium_of: Callable[[ax.AppInfo], bool] | None = None,
    **caps: Any,
) -> list[AppCoverage]:
    own = os.getpid()
    rows = []
    for app in apps:
        if app.pid == own:
            continue
        chromium = chromium_of(app) if chromium_of else is_chromium_family(bundle_path(app.pid))
        rows.append(walk_app(app, api, chromium=chromium, **caps))
    return rows


def summarise(rows: Sequence[AppCoverage]) -> dict[str, Any]:
    reachable = [r for r in rows if r.reachable]
    with_names = [r for r in reachable if r.nameable > 0]
    errors = Counter(r.ax_error for r in rows if not r.reachable)
    actionable = sum(r.actionable for r in reachable)
    nameable = sum(r.nameable for r in reachable)
    return {
        "apps": len(rows),
        "reachable": len(reachable),
        "apps_with_a_nameable_window_control": len(with_names),
        "fraction_of_apps_nameable": round(len(with_names) / len(rows), 3) if rows else 0.0,
        "actionable_elements": actionable,
        "nameable_elements": nameable,
        "fraction_of_elements_nameable": round(nameable / actionable, 3) if actionable else 0.0,
        "chromium_family": sum(1 for r in rows if r.chromium),
        "daa_hint_disagrees_with_bundle": sum(1 for r in rows if r.chromium != r.daa_manual_hint),
        "truncated": sum(1 for r in rows if r.truncated),
        "errors": {str(code): count for code, count in sorted(errors.items())},
    }


def render(rows: Sequence[AppCoverage], summary: dict[str, Any], *, trusted: bool) -> str:
    """The table. Built from AppCoverage fields only, so it cannot carry a label."""
    header = (
        f"{'bundle id':<44} {'reach':<6} {'nodes':>6} {'act':>5} {'named':>6} "
        f"{'%':>5} {'menu':>5} {'cr':>3} {'hint':>4}  note"
    )
    lines = [header]
    for r in rows:
        if r.reachable:
            note = "truncated by daa's caps" if r.truncated else ""
            if r.unnameable_roles:
                top = ", ".join(f"{k}x{v}" for k, v in list(r.unnameable_roles.items())[:3])
                note = (note + "; " if note else "") + f"unnamed: {top}"
            lines.append(
                f"{r.bundle_id[:44]:<44} {'yes':<6} {r.nodes:>6} {r.actionable:>5} "
                f"{r.nameable:>6} {r.fraction * 100:>4.0f}% {r.menu_nameable:>5} "
                f"{'Y' if r.chromium else '-':>3} {'Y' if r.daa_manual_hint else '-':>4}  {note}"
            )
        else:
            lines.append(
                f"{r.bundle_id[:44]:<44} {'no':<6} {'':>6} {'':>5} {'':>6} {'':>5} {'':>5} "
                f"{'Y' if r.chromium else '-':>3} {'Y' if r.daa_manual_hint else '-':>4}  "
                f"{r.ax_error} {_error_name(r.ax_error)}"
            )
    lines.append("")
    lines.append(
        f"{summary['apps']} apps, {summary['reachable']} reachable, "
        f"{summary['apps_with_a_nameable_window_control']} with at least one nameable "
        f"window control ({summary['fraction_of_apps_nameable'] * 100:.0f}%); "
        f"{summary['nameable_elements']}/{summary['actionable_elements']} actionable "
        f"elements nameable; {summary['chromium_family']} Chromium/Electron; "
        f"daa's name hint disagrees with the bundle for "
        f"{summary['daa_hint_disagrees_with_bundle']}."
    )
    lines.append(
        "cr = ships a Chromium/Electron framework; hint = daa's name-based "
        "wants_manual_accessibility(). Labels and window titles are never printed."
    )
    if not trusted:
        lines.append(GRANT_LINE.format(exe=sys.executable))
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    apps: Sequence[ax.AppInfo] | None = None,
    api: Any = None,
    trusted: bool | None = None,
    chromium_of: Callable[[ax.AppInfo], bool] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S, help="seconds per app")
    args = parser.parse_args(list(argv) if argv is not None else None)

    api = api if api is not None else load_api()
    if api is None:
        print("The macOS accessibility bindings are not available here; nothing to measure.")
        return 0
    if trusted is None:
        try:
            trusted = bool(api.AXIsProcessTrusted())   # non-prompting
        except Exception:  # noqa: BLE001
            trusted = False
    pool = list(apps) if apps is not None else ax.running_apps()
    rows = survey(
        pool, api, chromium_of=chromium_of,
        max_nodes=args.max_nodes, max_depth=args.max_depth, budget_s=args.budget,
    )
    summary = summarise(rows)
    if args.json:
        print(json.dumps(
            {"trusted": trusted, "summary": summary, "apps": [asdict(r) for r in rows]},
            indent=2, ensure_ascii=True,
        ))
        if not trusted:
            print(GRANT_LINE.format(exe=sys.executable), file=sys.stderr)
    else:
        print(render(rows, summary, trusted=trusted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
