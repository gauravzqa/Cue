"""The accessibility layer. Element model, bounded walk, input primitives.

No tool logic, no policy, no spoken sentences live here -- those are in
`naming.py` and `tools.py`. This module's whole job is to turn the macOS
Accessibility API into a small, bounded, honest value type.

Four rules are enforced here rather than by the callers, because a caller that
has to remember them will eventually forget one:

1. **The walk never reads `AXValue`.** Not once, not for naming, not for undo.
   `AXValue` is where the text of your email, the contents of your document and
   the password in a login form live. Every label this module produces comes
   from `AXTitle`, `AXDescription`, `AXHelp` or `AXPlaceholderValue`, all of
   which are written by the app's developer, not by the user. `ui_click`'s
   resolver runs before any risk gate has looked at it, so what it is
   STRUCTURALLY unable to read matters more than what it promises not to log.

2. **Nothing here mutates the target app during a walk.** In particular
   `AXManualAccessibility` -- the flag that makes an Electron app build its
   accessibility tree -- is off by default and must be asked for explicitly.
   Setting it turns on a whole subsystem inside somebody else's process, which
   is a side effect, and `resolve()` is not allowed to have any.

3. **Every AX call is bounded.** `AXUIElementSetMessagingTimeout` on the app
   element, a node cap, a depth cap and a wall-clock deadline. AX calls are
   synchronous IPC into an app that may be beachballing; an unbounded walk is a
   resolver that hangs the voice loop.

4. **A missing grant is a `Degraded`, never an exception.** One revoked TCC
   grant costs one capability. The three failure modes macOS can report are
   genuinely different and get genuinely different remedies: -25211 "you have
   no Accessibility grant", -25208 "this app has no accessibility server at
   all", and an empty tree "this app has a server and it has nothing to say".
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from daa.tools.base import Degraded
from daa.tools.permissions import ACCESSIBILITY, REMEDY, require

# ---------------------------------------------------------------------------
# Bounds. These are the whole defence for "resolve() reads the screen".
# ---------------------------------------------------------------------------

MAX_DEPTH = 12
MAX_NODES = 800
WALK_BUDGET_S = 1.5
# Per-message timeout on the app element, inherited by its descendants. Short
# enough that a hung app costs one element, not the turn.
MESSAGING_TIMEOUT_S = 0.25
# The menu bar is walked as a separate root with its own, smaller budget: a
# deep menu tree must not be able to crowd the windows out of the cap.
MAX_MENU_NODES = 300
MAX_MENU_DEPTH = 6
# Longest label we will carry into a spoken sentence. A "label" long enough to
# be a paragraph is not a label.
MAX_LABEL_CHARS = 60

# AXError codes worth telling apart. Everything else collapses to "unknown".
AX_SUCCESS = 0
AX_ATTRIBUTE_UNSUPPORTED = -25205
AX_ACTION_UNSUPPORTED = -25206
AX_NOT_IMPLEMENTED = -25208       # the target app answers no AX requests at all
AX_API_DISABLED = -25211          # WE are not a trusted accessibility client
AX_INVALID_ELEMENT = -25202
AX_CANNOT_COMPLETE = -25204

# Roles whose AXValue is user content. Listed so that a future edit that adds
# value-reading has to delete this comment to do it.
SECURE_ROLES = frozenset({"AXSecureTextField"})
TEXT_ENTRY_ROLES = frozenset(
    {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSecureTextField"}
)
# What `ui_click` is allowed to consider a candidate. Pressing static text is
# not a thing, and offering it as a candidate only widens what a resolver reads.
PRESSABLE_ACTIONS = frozenset({"AXPress", "AXConfirm", "AXPick", "AXOpen"})
CLICKABLE_ROLES = frozenset(
    {
        "AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXMenuButton",
        "AXPopUpButton", "AXLink", "AXDisclosureTriangle", "AXTab", "AXToolbarButton",
        "AXCell", "AXRow", "AXImage", "AXStaticText",
    }
)
# Window subroles that mean "this is a modal the user is being asked to answer".
ALERT_SUBROLES = frozenset({"AXDialog", "AXSystemDialog", "AXSystemFloatingWindow"})
ALERT_ROLES = frozenset({"AXSheet", "AXAlert"})

# Bundle ids that mean the window belongs to macOS itself rather than to an app
# the user launched. A press inside one of these is a press on a permission or
# security decision -- see naming.assess().
#
# Deliberately NOT the whole of "com.apple.*". The design note proposed that,
# and it is wrong: TextEdit's unsaved-changes sheet would then read back as
# "this is a macOS permission dialog", which is a false sentence in the one
# place this package cannot afford one. An over-firing system detector trains
# the user to click through the warning it exists to give.
SYSTEM_BUNDLE_PREFIXES = (
    "com.apple.UserNotificationCenter",
    "com.apple.SecurityAgent",
    "com.apple.coreservices.",
    "com.apple.tccd",
    "com.apple.controlcenter",
    "com.apple.loginwindow",
    "com.apple.systempreferences",
    "com.apple.ScreenSharing",
)
SYSTEM_BUNDLE_IDS = frozenset(
    {
        "com.apple.UserNotificationCenter",
        "com.apple.systempreferences",
        "com.apple.SecurityAgent",
        "com.apple.coreservices.uiagent",
        "com.apple.controlcenter",
        "com.apple.loginwindow",
    }
)
# Apps where typed text is a command line. Not a refusal -- a floor.
TERMINAL_BUNDLE_IDS = frozenset(
    {
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable",
        "dev.warp.Warp", "com.mitchellh.ghostty", "io.alacritty",
        "net.kovidgoyal.kitty", "co.zeit.hyper",
    }
)
# Chromium-family apps build no accessibility tree until a client asks. The ask
# is `AXManualAccessibility`, which is Electron's documented opt-in.
# `AXEnhancedUserInterface` also works and is NEVER used here: it makes
# Chromium animate window moves, which breaks window management elsewhere in
# this package's sibling tools.
MANUAL_AX_HINTS = ("electron", "chrome", "chromium", "slack", "code", "vscode", "discord")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Element:
    """One nameable node of an app's accessibility tree.

    `ref` is the live `AXUIElementRef`. It is excluded from equality and from
    the identity digest because it is a process-local handle, not a fact about
    the element, and it never leaves this package -- `ResolvedAction.args` must
    stay serialisable, so a tool re-finds its element by `identity` at run time
    rather than carrying a pointer across the confirmation.
    """

    role: str = ""
    subrole: str = ""
    role_description: str = ""
    title: str = ""
    description: str = ""
    help: str = ""
    placeholder: str = ""
    identifier: str = ""
    enabled: bool = True
    focused: bool = False
    actions: tuple[str, ...] = ()
    window_title: str = ""
    window_subrole: str = ""
    in_alert: bool = False
    is_default_button: bool = False
    app: str = ""
    bundle_id: str = ""
    pid: int = 0
    depth: int = 0
    path: tuple[int, ...] = ()
    position: tuple[float, float] | None = None
    size: tuple[float, float] | None = None
    ref: Any = field(default=None, compare=False, repr=False)

    @property
    def is_secure(self) -> bool:
        return self.role in SECURE_ROLES or self.subrole in SECURE_ROLES

    @property
    def is_text_entry(self) -> bool:
        return self.role in TEXT_ENTRY_ROLES or self.subrole in TEXT_ENTRY_ROLES

    @property
    def is_pressable(self) -> bool:
        if any(a in PRESSABLE_ACTIONS for a in self.actions):
            return True
        return self.role in CLICKABLE_ROLES

    @property
    def label(self) -> str:
        """The author-written name of this control. NEVER the user's content.

        Deliberately does not consult `AXValue`. For a text field AXValue is
        what the user typed; for a static text it is the document. Neither is a
        label, and reading either would make `resolve()` a screen reader.
        """
        for candidate in (self.title, self.description, self.help, self.placeholder):
            flat = " ".join(str(candidate or "").split())
            if flat:
                return flat[:MAX_LABEL_CHARS].strip()
        return ""

    @property
    def identity(self) -> str:
        """A digest of what this element IS, stable across two walks.

        The tree path is deliberately EXCLUDED. A toolbar button that moves
        from index three to index four is the same button, and an identity that
        said otherwise would abort every sequence on a cosmetic reflow. The
        cost is that two identically-labelled controls in one window collide --
        which is the ambiguity case, and ambiguity is refused rather than
        guessed, so the collision is caught by the tool instead of acted on.
        """
        parts = (
            self.bundle_id, self.window_title, self.role, self.subrole,
            self.role_description, self.title, self.description, self.identifier,
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]

    @property
    def center(self) -> tuple[float, float] | None:
        if self.position is None or self.size is None:
            return None
        w, h = self.size
        if w <= 0 or h <= 0:
            return None
        return (self.position[0] + w / 2.0, self.position[1] + h / 2.0)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One bounded read of one app's tree, plus an honest account of the read."""

    app: str = ""
    bundle_id: str = ""
    pid: int = 0
    elements: tuple[Element, ...] = ()
    snapshot_id: str = ""
    degraded: Degraded | None = None
    truncated: bool = False
    nodes_visited: int = 0
    elapsed_ms: float = 0.0
    ax_error: int = 0
    manual_accessibility_set: bool = False

    @property
    def ok(self) -> bool:
        return self.degraded is None

    def shape(self) -> dict[str, Any]:
        """Counts and roles only -- what an audit row may carry about a walk.

        Never the labels. A walk of Mail's window and a walk of a blank
        TextEdit produce the same kind of row, which is the point.
        """
        counts: dict[str, int] = {}
        for el in self.elements:
            counts[el.role or "AXUnknown"] = counts.get(el.role or "AXUnknown", 0) + 1
        return {
            "app": self.app,
            "elements": len(self.elements),
            "nodes_visited": self.nodes_visited,
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "roles": counts,
        }


def token(snapshot_id: str, element: Element) -> str:
    """An opaque handle that GOES STALE on purpose.

    A bare identity says "the same element"; a token says "the same element, as
    of that read". Anything holding a token across a confirmation is holding
    something it must re-check, and the shape of the string makes that obvious.
    """
    return f"{snapshot_id}:{element.identity}"


# ---------------------------------------------------------------------------
# Error discrimination -- three failures, three different sentences
# ---------------------------------------------------------------------------


def explain_ax_error(code: int, app: str = "") -> Degraded:
    who = app or "that app"
    if code == AX_API_DISABLED:
        return Degraded(
            ACCESSIBILITY,
            REMEDY[ACCESSIBILITY],
            f"kAXErrorAPIDisabled ({code}) -- this process is not a trusted "
            "accessibility client",
            {"ax_error": code},
        )
    if code == AX_NOT_IMPLEMENTED:
        return Degraded(
            "ax_tree",
            f"{who} does not answer accessibility requests, so I cannot name "
            "anything in it. Tell me what to do another way.",
            f"kAXErrorNotImplemented ({code})",
            {"ax_error": code},
        )
    if code == AX_ATTRIBUTE_UNSUPPORTED:
        return Degraded(
            "ax_tree",
            f"{who} does not expose its windows to me.",
            f"kAXErrorAttributeUnsupported ({code})",
            {"ax_error": code},
        )
    if code == AX_CANNOT_COMPLETE:
        return Degraded(
            "ax_tree",
            f"{who} did not answer in time. It may be busy.",
            f"kAXErrorCannotComplete ({code})",
            {"ax_error": code},
        )
    return Degraded(
        "ax_tree",
        f"I could not read the controls in {who}.",
        f"AXError {code}",
        {"ax_error": code},
    )


# ---------------------------------------------------------------------------
# pyobjc, loaded lazily so that importing this module needs no macOS at all
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _api() -> Any | None:
    """The ApplicationServices symbols, or None off macOS / without pyobjc."""
    if sys.platform != "darwin":
        return None
    try:
        import ApplicationServices as A  # type: ignore
    except Exception:  # noqa: BLE001 - a missing binding costs the capability
        return None
    return A


@lru_cache(maxsize=1)
def _quartz() -> Any | None:
    if sys.platform != "darwin":
        return None
    try:
        import Quartz  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return Quartz


def _copy(api: Any, ref: Any, attribute: str) -> tuple[int, Any]:
    try:
        return api.AXUIElementCopyAttributeValue(ref, attribute, None)
    except Exception:  # noqa: BLE001
        return AX_CANNOT_COMPLETE, None


def _copy_str(api: Any, ref: Any, attribute: str) -> str:
    err, value = _copy(api, ref, attribute)
    if err != AX_SUCCESS or value is None:
        return ""
    try:
        return " ".join(str(value).split())
    except Exception:  # noqa: BLE001
        return ""


def _copy_bool(api: Any, ref: Any, attribute: str, default: bool) -> bool:
    err, value = _copy(api, ref, attribute)
    if err != AX_SUCCESS or value is None:
        return default
    return bool(value)


def _copy_point(api: Any, ref: Any, attribute: str, kind: Any) -> tuple[float, float] | None:
    err, value = _copy(api, ref, attribute)
    if err != AX_SUCCESS or value is None:
        return None
    try:
        ok, out = api.AXValueGetValue(value, kind, None)
        if not ok or out is None:
            return None
        return (float(out.x), float(out.y))
    except Exception:  # noqa: BLE001
        return None


def _copy_size(api: Any, ref: Any, attribute: str, kind: Any) -> tuple[float, float] | None:
    err, value = _copy(api, ref, attribute)
    if err != AX_SUCCESS or value is None:
        return None
    try:
        ok, out = api.AXValueGetValue(value, kind, None)
        if not ok or out is None:
            return None
        return (float(out.width), float(out.height))
    except Exception:  # noqa: BLE001
        return None


def _actions(api: Any, ref: Any) -> tuple[str, ...]:
    try:
        err, names = api.AXUIElementCopyActionNames(ref, None)
    except Exception:  # noqa: BLE001
        return ()
    if err != AX_SUCCESS or not names:
        return ()
    return tuple(str(n) for n in names)


# ---------------------------------------------------------------------------
# Running applications
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppInfo:
    name: str
    bundle_id: str
    pid: int


def running_apps() -> list[AppInfo]:
    """Apps with a normal activation policy. Never raises."""
    if sys.platform != "darwin":
        return []
    try:
        from AppKit import NSWorkspace  # type: ignore
    except Exception:  # noqa: BLE001
        return []
    out: list[AppInfo] = []
    try:
        for app in NSWorkspace.sharedWorkspace().runningApplications() or []:
            try:
                if int(app.activationPolicy()) != 0:
                    continue
                out.append(
                    AppInfo(
                        name=str(app.localizedName() or ""),
                        bundle_id=str(app.bundleIdentifier() or ""),
                        pid=int(app.processIdentifier()),
                    )
                )
            except Exception:  # noqa: BLE001,S112 - one bad app, not no apps
                continue
    except Exception:  # noqa: BLE001
        return []
    return [a for a in out if a.name]


def wants_manual_accessibility(app: AppInfo) -> bool:
    """Is this app in the Chromium family, where the tree is off by default?"""
    blob = f"{app.bundle_id} {app.name}".lower()
    return any(hint in blob for hint in MANUAL_AX_HINTS)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _element(
    api: Any,
    ref: Any,
    *,
    app: AppInfo,
    depth: int,
    path: tuple[int, ...],
    window_title: str,
    window_subrole: str,
    in_alert: bool,
    default_identity: Any,
) -> Element:
    return Element(
        role=_copy_str(api, ref, api.kAXRoleAttribute),
        subrole=_copy_str(api, ref, api.kAXSubroleAttribute),
        role_description=_copy_str(api, ref, api.kAXRoleDescriptionAttribute),
        title=_copy_str(api, ref, api.kAXTitleAttribute),
        description=_copy_str(api, ref, api.kAXDescriptionAttribute),
        help=_copy_str(api, ref, api.kAXHelpAttribute),
        placeholder=_copy_str(api, ref, api.kAXPlaceholderValueAttribute),
        identifier=_copy_str(api, ref, api.kAXIdentifierAttribute),
        enabled=_copy_bool(api, ref, api.kAXEnabledAttribute, True),
        focused=_copy_bool(api, ref, api.kAXFocusedAttribute, False),
        actions=_actions(api, ref),
        window_title=window_title,
        window_subrole=window_subrole,
        in_alert=in_alert,
        is_default_button=default_identity is not None and bool(ref == default_identity),
        app=app.name,
        bundle_id=app.bundle_id,
        pid=app.pid,
        depth=depth,
        path=path,
        position=_copy_point(api, ref, api.kAXPositionAttribute, api.kAXValueCGPointType),
        size=_copy_size(api, ref, api.kAXSizeAttribute, api.kAXValueCGSizeType),
        ref=ref,
    )


def set_manual_accessibility(pid: int, on: bool = True) -> bool:
    """Ask a Chromium-family app to build its accessibility tree.

    This MUTATES the target process -- it switches a subsystem on inside
    somebody else's app -- so it is never called from a resolver. Only
    `ui_describe.run()`, which the user was told about, may do it.
    """
    api = _api()
    if api is None:
        return False
    try:
        app_ref = api.AXUIElementCreateApplication(int(pid))
        err = api.AXUIElementSetAttributeValue(app_ref, "AXManualAccessibility", bool(on))
        return int(err) == AX_SUCCESS
    except Exception:  # noqa: BLE001
        return False


def snapshot(
    app_query: str,
    *,
    window: str | None = None,
    max_depth: int = MAX_DEPTH,
    max_nodes: int = MAX_NODES,
    budget_s: float = WALK_BUDGET_S,
    enable_manual_accessibility: bool = False,
    include_menu_bar: bool = True,
    apps: Sequence[AppInfo] | None = None,
) -> Snapshot:
    """One bounded read of one app's controls. Never raises, never prompts.

    `enable_manual_accessibility` defaults to False and every resolver leaves it
    that way: see `set_manual_accessibility`.
    """
    from daa.tools.base import rank_candidates  # local: keeps import order flat

    started = time.monotonic()
    pool = list(apps) if apps is not None else running_apps()
    if not pool:
        return Snapshot(
            app=app_query,
            degraded=Degraded(
                "ax_tree", "I cannot see which applications are running.",
                "no running applications with a normal activation policy",
            ),
        )
    ranked = rank_candidates(app_query, pool, key=lambda a: a.name, limit=3)
    ranked = [(score, a) for score, a in ranked if score >= 0.55]
    if not ranked:
        return Snapshot(
            app=app_query,
            degraded=Degraded(
                "ax_tree", f"{app_query} does not seem to be running.",
                f"no running app matched {app_query!r}",
            ),
        )
    app = ranked[0][1]

    api = _api()
    if api is None:
        return Snapshot(
            app=app.name, bundle_id=app.bundle_id, pid=app.pid,
            degraded=Degraded(
                ACCESSIBILITY, REMEDY[ACCESSIBILITY],
                "the accessibility bindings are not available in this process",
            ),
        )
    state = require(ACCESSIBILITY)
    manual_set = False
    if enable_manual_accessibility and wants_manual_accessibility(app):
        manual_set = set_manual_accessibility(app.pid, True)

    try:
        app_ref = api.AXUIElementCreateApplication(app.pid)
        try:
            api.AXUIElementSetMessagingTimeout(app_ref, MESSAGING_TIMEOUT_S)
        except Exception:  # noqa: BLE001,S110 - advisory; the deadline still bounds us
            pass
        err, windows = api.AXUIElementCopyAttributeValue(app_ref, api.kAXWindowsAttribute, None)
    except Exception as exc:  # noqa: BLE001
        return Snapshot(
            app=app.name, bundle_id=app.bundle_id, pid=app.pid,
            degraded=Degraded("ax_tree", f"I could not read {app.name}.", str(exc)),
        )

    if err != AX_SUCCESS:
        # A denied grant is reported as the grant, not as a mystery error, even
        # when the raw code arrives as something else.
        degraded = explain_ax_error(int(err), app.name)
        if not state.granted and int(err) == AX_API_DISABLED:
            degraded = explain_ax_error(AX_API_DISABLED, app.name)
        return Snapshot(
            app=app.name, bundle_id=app.bundle_id, pid=app.pid,
            degraded=degraded, ax_error=int(err),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    deadline = started + max(0.05, float(budget_s))
    collected: list[Element] = []
    visited = 0
    truncated = False

    for w_index, win in enumerate(windows or []):
        if visited >= max_nodes or time.monotonic() > deadline:
            truncated = True
            break
        w_title = _copy_str(api, win, api.kAXTitleAttribute)
        w_subrole = _copy_str(api, win, api.kAXSubroleAttribute)
        w_role = _copy_str(api, win, api.kAXRoleAttribute)
        if window and w_title and window.strip().lower() not in w_title.lower():
            continue
        in_alert = w_subrole in ALERT_SUBROLES or w_role in ALERT_ROLES
        derr, default_button = _copy(api, win, api.kAXDefaultButtonAttribute)
        default_identity = default_button if derr == AX_SUCCESS else None

        stack: list[tuple[Any, int, tuple[int, ...]]] = [(win, 0, (w_index,))]
        while stack:
            if visited >= max_nodes or time.monotonic() > deadline:
                truncated = True
                break
            ref, depth, path = stack.pop()
            visited += 1
            element = _element(
                api, ref, app=app, depth=depth, path=path,
                window_title=w_title, window_subrole=w_subrole,
                in_alert=in_alert or (depth > 0 and _is_alert_container(api, ref)),
                default_identity=default_identity,
            )
            if depth > 0 and element.label:
                collected.append(element)
            if depth >= max_depth:
                truncated = True
                continue
            cerr, children = _copy(api, ref, api.kAXChildrenAttribute)
            if cerr != AX_SUCCESS or not children:
                continue
            for c_index, child in reversed(list(enumerate(children))):
                stack.append((child, depth + 1, (*path, c_index)))

    # The menu bar is not a window, so it is a separate root -- and it gets its
    # own node budget rather than sharing one, because a big menu bar would
    # otherwise eat the whole cap and silently truncate the windows that the
    # user was actually talking about.
    if include_menu_bar and not window:
        menu_deadline = min(deadline, time.monotonic() + budget_s / 2.0)
        merr, menu_bar = _copy(api, app_ref, getattr(api, "kAXMenuBarAttribute", "AXMenuBar"))
        if merr == AX_SUCCESS and menu_bar is not None:
            menu_visited = 0
            stack = [(menu_bar, 0, (-1,))]
            while stack:
                if menu_visited >= MAX_MENU_NODES or time.monotonic() > menu_deadline:
                    truncated = True
                    break
                ref, depth, path = stack.pop()
                menu_visited += 1
                visited += 1
                element = _element(
                    api, ref, app=app, depth=depth, path=path,
                    window_title="", window_subrole="AXMenuBar",
                    in_alert=False, default_identity=None,
                )
                if depth > 0 and element.label:
                    collected.append(element)
                if depth >= MAX_MENU_DEPTH:
                    continue
                cerr, children = _copy(api, ref, api.kAXChildrenAttribute)
                if cerr != AX_SUCCESS or not children:
                    continue
                for c_index, child in reversed(list(enumerate(children))):
                    stack.append((child, depth + 1, (*path, c_index)))

    elapsed = (time.monotonic() - started) * 1000.0
    snap_id = hashlib.sha256(
        f"{app.pid}|{app.bundle_id}|{started}|{visited}".encode()
    ).hexdigest()[:12]
    if not collected:
        return Snapshot(
            app=app.name, bundle_id=app.bundle_id, pid=app.pid, snapshot_id=snap_id,
            degraded=Degraded(
                "ax_tree",
                f"I cannot see anything I can name in {app.name}.",
                "the accessibility tree was empty or had no labelled controls",
                {"nodes_visited": visited},
            ),
            truncated=truncated, nodes_visited=visited, elapsed_ms=elapsed,
            manual_accessibility_set=manual_set,
        )
    return Snapshot(
        app=app.name, bundle_id=app.bundle_id, pid=app.pid,
        elements=tuple(collected), snapshot_id=snap_id, truncated=truncated,
        nodes_visited=visited, elapsed_ms=elapsed, manual_accessibility_set=manual_set,
    )


def _is_alert_container(api: Any, ref: Any) -> bool:
    role = _copy_str(api, ref, api.kAXRoleAttribute)
    return role in ALERT_ROLES


def find_by_identity(snap: Snapshot, identity: str) -> list[Element]:
    return [el for el in snap.elements if el.identity == identity]


# ---------------------------------------------------------------------------
# Hit testing -- the rule that makes a coordinate answerable
# ---------------------------------------------------------------------------


def hit_test(x: float, y: float) -> Element | None:
    """What is actually at this point, according to the same tree the readback
    used? Returns None when the point names nothing.

    A coordinate that does not hit-test to a nameable element is a coordinate
    you may not click. This package never takes a coordinate as an argument;
    the rule is applied INTERNALLY, to the synthetic-click fallback, so that a
    click posted at an element's centre is checked against the element the
    sentence named before the button goes down.
    """
    api = _api()
    if api is None:
        return None
    try:
        system = api.AXUIElementCreateSystemWide()
        api.AXUIElementSetMessagingTimeout(system, MESSAGING_TIMEOUT_S)
        err, ref = api.AXUIElementCopyElementAtPosition(system, float(x), float(y), None)
    except Exception:  # noqa: BLE001
        return None
    if err != AX_SUCCESS or ref is None:
        return None
    try:
        perr, pid = api.AXUIElementGetPid(ref, None)
        pid = int(pid) if perr == AX_SUCCESS and pid else 0
    except Exception:  # noqa: BLE001
        pid = 0
    name = ""
    bundle = ""
    for info in running_apps():
        if info.pid == pid:
            name, bundle = info.name, info.bundle_id
            break
    window_title = _window_title_of(api, ref)
    return Element(
        role=_copy_str(api, ref, api.kAXRoleAttribute),
        subrole=_copy_str(api, ref, api.kAXSubroleAttribute),
        role_description=_copy_str(api, ref, api.kAXRoleDescriptionAttribute),
        title=_copy_str(api, ref, api.kAXTitleAttribute),
        description=_copy_str(api, ref, api.kAXDescriptionAttribute),
        help=_copy_str(api, ref, api.kAXHelpAttribute),
        placeholder=_copy_str(api, ref, api.kAXPlaceholderValueAttribute),
        identifier=_copy_str(api, ref, api.kAXIdentifierAttribute),
        actions=_actions(api, ref),
        window_title=window_title,
        app=name, bundle_id=bundle, pid=pid, ref=ref,
    )


def _window_title_of(api: Any, ref: Any) -> str:
    err, win = _copy(api, ref, api.kAXWindowAttribute)
    if err != AX_SUCCESS or win is None:
        err, win = _copy(api, ref, api.kAXTopLevelUIElementAttribute)
    if err != AX_SUCCESS or win is None:
        return ""
    return _copy_str(api, win, api.kAXTitleAttribute)


# ---------------------------------------------------------------------------
# Input primitives. Each returns (ok, detail); none raises.
# ---------------------------------------------------------------------------


def press(element: Element) -> tuple[bool, str]:
    """AXPress the element in place.

    Preferred over a synthetic click because it does not move the user's
    pointer, does not raise a window and does not change key focus -- for an
    assistant that runs while you work, that is the difference between a usable
    tool and one you turn off.
    """
    api = _api()
    if api is None or element.ref is None:
        return False, "no accessibility handle for that element"
    action = next((a for a in element.actions if a in PRESSABLE_ACTIONS), None)
    if action is None:
        return False, "that element has no press action"
    try:
        err = int(api.AXUIElementPerformAction(element.ref, action))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if err != AX_SUCCESS:
        return False, explain_ax_error(err, element.app).detail
    return True, ""


def click_center(element: Element) -> tuple[bool, str]:
    """Synthetic click at the element's centre, hit-tested first.

    Only reachable when the element exposes no press action. The hit test is
    not decoration: it is the rule from the design applied to the one place a
    coordinate exists at all. If the point under the cursor is not the element
    the sentence named, the click does not happen.
    """
    quartz = _quartz()
    if quartz is None:
        return False, "Quartz is not available in this process"
    center = element.center
    if center is None:
        return False, "that element has no position on screen"
    x, y = center
    landed = hit_test(x, y)
    if landed is None:
        return False, "nothing nameable is at that point on screen"
    if landed.identity != element.identity:
        return False, "the point I would click is no longer that element"
    try:
        source = quartz.CGEventSourceCreate(quartz.kCGEventSourceStateHIDSystemState)
        down = quartz.CGEventCreateMouseEvent(
            source, quartz.kCGEventLeftMouseDown, (x, y), quartz.kCGMouseButtonLeft
        )
        up = quartz.CGEventCreateMouseEvent(
            source, quartz.kCGEventLeftMouseUp, (x, y), quartz.kCGMouseButtonLeft
        )
        # Without this a pair of single clicks is not a double click and, more
        # importantly, some native targets ignore a click whose state is unset.
        for event in (down, up):
            quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, 1)
        quartz.CGEventPost(quartz.kCGHIDEventTap, down)
        quartz.CGEventPost(quartz.kCGHIDEventTap, up)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


def focus(element: Element) -> tuple[bool, str]:
    api = _api()
    if api is None or element.ref is None:
        return False, "no accessibility handle for that element"
    try:
        err = int(api.AXUIElementSetAttributeValue(element.ref, api.kAXFocusedAttribute, True))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if err != AX_SUCCESS:
        return False, explain_ax_error(err, element.app).detail
    return True, ""


def type_text(text: str, *, chunk: int = 16) -> tuple[bool, str]:
    """Type literal text via `CGEventKeyboardSetUnicodeString`.

    Not virtual key codes. Posting key codes types whatever those positions
    mean on the CURRENT keyboard layout, so `test` becomes garbage on a
    non-US layout -- and the readback promised the exact characters. A
    layout-dependent typing path makes the confirmation a lie.
    """
    quartz = _quartz()
    if quartz is None:
        return False, "Quartz is not available in this process"
    if not text:
        return False, "there was no text to type"
    try:
        source = quartz.CGEventSourceCreate(quartz.kCGEventSourceStateHIDSystemState)
        for start in range(0, len(text), chunk):
            piece = text[start : start + chunk]
            for is_down in (True, False):
                event = quartz.CGEventCreateKeyboardEvent(source, 0, is_down)
                quartz.CGEventKeyboardSetUnicodeString(event, len(piece), piece)
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


def post_key(keycode: int, flags: int) -> tuple[bool, str]:
    quartz = _quartz()
    if quartz is None:
        return False, "Quartz is not available in this process"
    try:
        source = quartz.CGEventSourceCreate(quartz.kCGEventSourceStateHIDSystemState)
        for is_down in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(source, int(keycode), is_down)
            quartz.CGEventSetFlags(event, int(flags))
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


def modifier_mask(modifiers: Iterable[str]) -> int:
    quartz = _quartz()
    if quartz is None:
        return 0
    table: Mapping[str, str] = {
        "command": "kCGEventFlagMaskCommand",
        "shift": "kCGEventFlagMaskShift",
        "option": "kCGEventFlagMaskAlternate",
        "control": "kCGEventFlagMaskControl",
        "fn": "kCGEventFlagMaskSecondaryFn",
    }
    mask = 0
    for name in modifiers:
        attr = table.get(name)
        if attr and hasattr(quartz, attr):
            mask |= int(getattr(quartz, attr))
    return mask




def focused_element() -> Element | None:
    """Whatever currently has key focus, system-wide.

    Used for exactly one thing: refusing to type into a password field that
    appeared after the sentence the user approved was spoken. A deferred typing
    step in a sequence types into whatever has focus, so "whatever has focus"
    has to be inspectable before the first character goes out.
    """
    api = _api()
    if api is None:
        return None
    try:
        system = api.AXUIElementCreateSystemWide()
        api.AXUIElementSetMessagingTimeout(system, MESSAGING_TIMEOUT_S)
        err, ref = api.AXUIElementCopyAttributeValue(
            system, "AXFocusedUIElement", None
        )
    except Exception:  # noqa: BLE001
        return None
    if err != AX_SUCCESS or ref is None:
        return None
    return Element(
        role=_copy_str(api, ref, api.kAXRoleAttribute),
        subrole=_copy_str(api, ref, api.kAXSubroleAttribute),
        role_description=_copy_str(api, ref, api.kAXRoleDescriptionAttribute),
        title=_copy_str(api, ref, api.kAXTitleAttribute),
        placeholder=_copy_str(api, ref, api.kAXPlaceholderValueAttribute),
        window_title=_window_title_of(api, ref),
        ref=ref,
    )


def reread(element: Element) -> Element | None:
    """Re-read one element through the handle we already hold.

    Cheaper than another walk, and it is what makes "did anything actually
    happen?" answerable. After an AXPress you can look at the same element
    again; after a synthetic click at a coordinate you can only take another
    picture and guess.
    """
    api = _api()
    if api is None or element.ref is None:
        return None
    err, _ = _copy(api, element.ref, api.kAXRoleAttribute)
    if err in (AX_INVALID_ELEMENT, AX_CANNOT_COMPLETE, AX_API_DISABLED):
        return None
    return _element(
        api,
        element.ref,
        app=AppInfo(element.app, element.bundle_id, element.pid),
        depth=element.depth,
        path=element.path,
        window_title=element.window_title,
        window_subrole=element.window_subrole,
        in_alert=element.in_alert,
        default_identity=None,
    )


__all__ = [
    "ALERT_ROLES",
    "ALERT_SUBROLES",
    "AX_API_DISABLED",
    "AX_NOT_IMPLEMENTED",
    "MAX_DEPTH",
    "MAX_LABEL_CHARS",
    "MAX_MENU_DEPTH",
    "MAX_MENU_NODES",
    "MAX_NODES",
    "SYSTEM_BUNDLE_IDS",
    "SYSTEM_BUNDLE_PREFIXES",
    "TERMINAL_BUNDLE_IDS",
    "WALK_BUDGET_S",
    "AppInfo",
    "Element",
    "Snapshot",
    "click_center",
    "explain_ax_error",
    "find_by_identity",
    "focus",
    "focused_element",
    "hit_test",
    "modifier_mask",
    "post_key",
    "press",
    "reread",
    "running_apps",
    "set_manual_accessibility",
    "snapshot",
    "token",
    "type_text",
    "wants_manual_accessibility",
]
