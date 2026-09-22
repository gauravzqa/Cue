"""A real AppKit app, inside the test process, for driving the `ui_*` tools.

Every other computer-use test runs on a recorded tree (`computer_tree.py`).
This module is what lets the same tools run against REAL macOS Accessibility
on a machine where Accessibility is denied -- which is every CI machine and,
at the time of writing, the developer's machine too.

Why this works without a grant: macOS only gates CROSS-process AX. A process
reading and writing its own accessibility tree through
`AXUIElementCreateApplication(os.getpid())` gets `kAXErrorSuccess`, while the
same calls against any other pid return `-25211 kAXErrorAPIDisabled`. So the
tools get a real `NSWindow` full of real controls, and every
`AXUIElementCopyAttributeValue`, `AXUIElementPerformAction` and
`AXUIElementSetAttributeValue` they make is a real call into AppKit's real
accessibility server.

Exactly three things are substituted, and each is named here so nobody has to
discover it:

1. **App discovery.** `ax.running_apps()` lists apps with a *regular*
   activation policy. This process deliberately is not one (a regular app gets
   a Dock icon and can steal focus), so `install()` makes `running_apps()`
   return one entry -- this process's own pid. This is the "test seam", and it
   lives entirely in test code: nothing in `src/` changed, and no tool
   argument can reach it.

2. **The window-server hop for synthetic keystrokes.** `ax.type_text` and
   `ax.post_key` build real `CGEvent`s and hand them to `CGEventPost` at the
   HID tap. That hop is refused here, for two reasons: posting needs a
   PostEvent grant this process does not have (and asking for one prompts), and
   an event posted at the HID tap goes to *whatever app is frontmost* -- the
   user's terminal, while they run the suite. `InProcessEventTransport` takes
   the exact `CGEvent` daa built, converts it with `NSEvent.eventWithCGEvent_`
   and dispatches it to the fixture window that holds AX focus, the way
   `NSApplication.sendEvent_` would. What is verified: the event daa builds
   carries the right characters, and AX focus puts them in the right field.
   What is NOT verified: which process the real window server would have
   delivered it to. (See `test_computer_live.py` for why that matters.)
   Synthetic MOUSE events are refused outright and recorded, so a test can
   assert that the coordinate fallback never fired.

3. **Nothing else.** `_api()` is wrapped by `RecordingAX`, which delegates
   every call to the real `ApplicationServices` and only writes down what was
   asked for -- so a live test can assert "no `AXValue` was requested", "the
   only attribute ever SET was `AXFocused`" and "no application element was
   ever created for another pid".

Windows are made invisible three ways -- alpha 0, ordered to the back, and
parked mostly off-screen -- and every one is closed in teardown, including on
failure. The app runs with the default *prohibited* activation policy, so it
never becomes active, never owns the menu bar and never takes key focus from
whatever the user is doing.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import ApplicationServices as _AS  # type: ignore
import objc  # type: ignore
import Quartz as _Q  # type: ignore
from AppKit import (  # type: ignore
    NSAlert,
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSEvent,
    NSEventMaskAny,
    NSEventTypeFlagsChanged,
    NSEventTypeKeyDown,
    NSEventTypeKeyUp,
    NSMenu,
    NSMenuItem,
    NSPopUpButton,
    NSSecureTextField,
    NSText,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSDate, NSDefaultRunLoopMode, NSObject  # type: ignore

from daa.tools.computer import ax

APP_NAME = "daa AX Fixture"
# Synthetic. The Python interpreter has no bundle id of its own, and a real one
# (com.apple.*) would switch on naming.is_system_window.
BUNDLE_ID = "dev.daa.ax-fixture"
MENU_TITLE = "Fixture"
MENU_ITEM = "Reticulate Splines"
# Far enough left and down that AppKit's constraint leaves only a sliver on
# screen; alpha 0 hides the sliver.
PARK_AT = (-6000.0, -6000.0)


# ---------------------------------------------------------------------------
# The Objective-C target every control reports to
# ---------------------------------------------------------------------------


class DaaAXFixtureTarget(NSObject):
    """Records every action message it receives, by the sender's title."""

    def init(self):
        instance = objc.super(DaaAXFixtureTarget, self).init()
        if instance is None:
            return None
        instance.hits = []
        instance.callbacks = {}
        return instance

    def hit_(self, sender):
        title = str(sender.title() or "")
        self.hits.append(title)
        callback = self.callbacks.get(title)
        if callback is not None:
            callback()


# ---------------------------------------------------------------------------
# The app: one per process
# ---------------------------------------------------------------------------


class FixtureApp:
    """`NSApplication` with a main menu, launched once, never activated.

    `finishLaunching` is what registers the process's accessibility server:
    before it, a self-process AX read returns `-25208 kAXErrorNotImplemented`.
    """

    _instance: FixtureApp | None = None

    def __init__(self) -> None:
        self.app = NSApplication.sharedApplication()
        self.target = DaaAXFixtureTarget.alloc().init()
        main = NSMenu.alloc().initWithTitle_("Main")
        top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(MENU_TITLE, None, "")
        main.addItem_(top)
        submenu = NSMenu.alloc().initWithTitle_(MENU_TITLE)
        top.setSubmenu_(submenu)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(MENU_ITEM, "hit:", "")
        item.setTarget_(self.target)
        submenu.addItem_(item)
        self.app.setMainMenu_(main)
        self.app.finishLaunching()
        self.windows: list[FixtureWindow] = []
        # The ONLY labels an AXPress from a live test may land on. Every Cocoa
        # app's menu bar also carries the system Apple menu -- Restart, Shut
        # Down, Log Out, Force Quit, Recent Items -- and AppKit populates it in
        # THIS process the first time AX walks it. RecordingAX refuses a press
        # on anything not listed here, so a bad match can never reach those.
        self.pressable: set[str] = {MENU_ITEM}

    @classmethod
    def shared(cls) -> FixtureApp:
        if cls._instance is None:
            cls._instance = FixtureApp()
        return cls._instance

    def pump(self, seconds: float = 0.05) -> None:
        """Run the event loop briefly so deferred AppKit work (sheets) lands."""
        until = NSDate.dateWithTimeIntervalSinceNow_(float(seconds))
        while True:
            event = self.app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                NSEventMaskAny, until, NSDefaultRunLoopMode, True
            )
            if event is None:
                break
            self.app.sendEvent_(event)

    def visible_windows(self) -> int:
        return sum(1 for w in (self.app.windows() or []) if w.isVisible())

    @property
    def is_active(self) -> bool:
        return bool(self.app.isActive())

    def window(self, title: str, *, size: tuple[float, float] = (420.0, 320.0)) -> FixtureWindow:
        win = FixtureWindow(self, title, size)
        self.windows.append(win)
        return win

    def close_all(self) -> None:
        """Close every fixture window. Safe to call twice; never raises."""
        for win in list(self.windows):
            win.close()
        self.windows.clear()
        # Belt and braces: anything AppKit still holds (an alert window that
        # outlived its sheet) goes too.
        for stray in list(self.app.windows() or []):
            try:
                stray.orderOut_(None)
                stray.close()
            except Exception:  # noqa: BLE001,S110 - teardown must not raise
                pass
        self.pump(0.05)


@dataclass
class FixtureWindow:
    fx: FixtureApp
    title: str
    size: tuple[float, float]
    nswindow: Any = None
    alerts: list[Any] = field(default_factory=list)
    responses: list[int] = field(default_factory=list)
    _next_y: float = 12.0

    def __post_init__(self) -> None:
        w, h = self.size
        self.nswindow = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0.0, 0.0), (w, h)),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.nswindow.setTitle_(self.title)
        self.nswindow.setReleasedWhenClosed_(False)
        self.nswindow.setAlphaValue_(0.0)

    @property
    def content(self) -> Any:
        return self.nswindow.contentView()

    def _frame(self, width: float = 180.0, height: float = 24.0) -> tuple:
        frame = ((12.0, self._next_y), (width, height))
        self._next_y += height + 6.0
        return frame

    # -- controls ----------------------------------------------------------

    def button(
        self,
        title: str,
        *,
        key: str = "",
        command: bool = False,
        on_hit: Callable[[], None] | None = None,
        parent: Any = None,
    ) -> Any:
        control = NSButton.alloc().initWithFrame_(self._frame(140.0, 28.0))
        control.setTitle_(title)
        self.fx.pressable.add(title)
        control.setTarget_(self.fx.target)
        control.setAction_("hit:")
        if key:
            control.setKeyEquivalent_(key)
            if command:
                from AppKit import NSEventModifierFlagCommand  # type: ignore

                control.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
        if on_hit is not None:
            self.fx.target.callbacks[title] = on_hit
        (parent or self.content).addSubview_(control)
        return control

    def text_field(self, placeholder: str, *, secure: bool = False) -> Any:
        cls = NSSecureTextField if secure else NSTextField
        control = cls.alloc().initWithFrame_(self._frame(220.0, 24.0))
        control.setPlaceholderString_(placeholder)
        self.content.addSubview_(control)
        return control

    def popup(self, items: Sequence[str], *, label: str = "") -> Any:
        control = NSPopUpButton.alloc().initWithFrame_pullsDown_(self._frame(160.0, 26.0), False)
        control.addItemsWithTitles_(list(items))
        control.setTarget_(self.fx.target)
        control.setAction_("hit:")
        if label:
            control.setAccessibilityLabel_(label)
            self.fx.pressable.add(label)
        self.content.addSubview_(control)
        return control

    def nested_groups(self, depth: int, leaf_title: str) -> Any:
        """`depth` accessible AXGroups, one inside the next, a button at the bottom.

        Plain NSViews are ignored by AX and would flatten away; these declare
        themselves accessibility elements so the tree really is this deep.
        """
        from AppKit import NSAccessibilityGroupRole  # type: ignore

        parent = self.content
        for level in range(depth):
            group = NSView.alloc().initWithFrame_(((0.0, 0.0), (200.0, 60.0)))
            group.setAccessibilityElement_(True)
            group.setAccessibilityRole_(NSAccessibilityGroupRole)
            group.setAccessibilityLabel_(f"Level {level}")
            parent.addSubview_(group)
            parent = group
        leaf = NSButton.alloc().initWithFrame_(((0.0, 0.0), (120.0, 24.0)))
        leaf.setTitle_(leaf_title)
        self.fx.pressable.add(leaf_title)
        leaf.setTarget_(self.fx.target)
        leaf.setAction_("hit:")
        parent.addSubview_(leaf)
        return leaf

    def many_buttons(self, count: int, prefix: str = "Filler") -> None:
        for index in range(count):
            control = NSButton.alloc().initWithFrame_(((240.0, 0.0), (20.0, 20.0)))
            control.setTitle_(f"{prefix} {index}")
            self.content.addSubview_(control)

    # -- lifecycle ---------------------------------------------------------

    def show(self) -> FixtureWindow:
        """Order in behind everything, invisible, parked off-screen."""
        self.nswindow.orderBack_(None)
        self.nswindow.setFrameOrigin_(PARK_AT)
        self.fx.pump(0.05)
        return self

    def alert_sheet(self, message: str, buttons: Sequence[str]) -> Any:
        """An NSAlert as a sheet. The first button is the default button."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        for title in buttons:
            alert.addButtonWithTitle_(title)
            self.fx.pressable.add(title)
        alert.beginSheetModalForWindow_completionHandler_(
            self.nswindow, lambda response: self.responses.append(int(response))
        )
        # A sheet is its own window and does not inherit the parent's alpha.
        alert.window().setAlphaValue_(0.0)
        self.alerts.append(alert)
        self.fx.pump(0.3)
        return alert

    def remove(self, view: Any) -> None:
        view.removeFromSuperview()
        self.fx.pump(0.02)

    def close(self) -> None:
        try:
            for alert in self.alerts:
                sheet = alert.window()
                if sheet is not None and sheet.isVisible():
                    self.nswindow.endSheet_(sheet)
                    sheet.orderOut_(None)
            self.nswindow.makeFirstResponder_(None)
            self.nswindow.orderOut_(None)
            self.nswindow.close()
        except Exception:  # noqa: BLE001,S110 - teardown must not raise
            pass

    def focused_text_window(self) -> bool:
        return isinstance(self.nswindow.firstResponder(), NSText)


# ---------------------------------------------------------------------------
# Substitution 2: in-process delivery for the CGEvents daa builds
# ---------------------------------------------------------------------------


class SyntheticMouseRefused(RuntimeError):
    pass


class InProcessEventTransport:
    """Stands in for `Quartz`, with `CGEventPost` kept inside this process.

    Every other attribute is the real Quartz symbol, so daa's event
    CONSTRUCTION -- `CGEventCreateKeyboardEvent`,
    `CGEventKeyboardSetUnicodeString`, `CGEventSetFlags` -- is the real code
    path. Only the final hop is replaced.
    """

    _KEY_TYPES = (NSEventTypeKeyDown, NSEventTypeKeyUp, NSEventTypeFlagsChanged)

    def __init__(self, fx: FixtureApp) -> None:
        self.fx = fx
        self.delivered: list[tuple[int, str]] = []
        self.refused_mouse: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(_Q, name)

    def _window(self) -> Any:
        """The fixture window that holds AX focus, else the frontmost one.

        This is the stand-in for "the window server routes key events to the
        key window of the active app". It is NOT what the real window server
        would do for this process -- see the module docstring.
        """
        wins = [w for w in self.fx.windows if w.nswindow.isVisible()]
        for win in reversed(wins):
            if win.focused_text_window():
                return win.nswindow
        return wins[-1].nswindow if wins else None

    def CGEventPost(self, tap: Any, event: Any) -> None:
        ns = NSEvent.eventWithCGEvent_(event)
        kind = int(ns.type()) if ns is not None else -1
        if kind not in self._KEY_TYPES:
            self.refused_mouse.append(kind)
            raise SyntheticMouseRefused(
                "the live fixture refuses synthetic mouse events: a coordinate click "
                "would land on whatever is on screen there"
            )
        window = self._window()
        if window is None:
            return
        chars = str(ns.characters() or "") if kind != NSEventTypeFlagsChanged else ""
        self.delivered.append((kind, chars))
        # NSApplication.sendEvent_ offers a key-down to the window's key
        # equivalents first; a prohibited-policy app has no key window, so the
        # routing is done here by hand, in the same order.
        if kind == NSEventTypeKeyDown and window.performKeyEquivalent_(ns):
            return
        window.sendEvent_(ns)

    def CGEventPostToPid(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("daa is not expected to post to a pid")

    def typed_text(self) -> str:
        return "".join(chars for kind, chars in self.delivered if kind == NSEventTypeKeyDown)


# ---------------------------------------------------------------------------
# Substitution 3 (observation only): the real ApplicationServices, recorded
# ---------------------------------------------------------------------------


class RecordingAX:
    """The real `ApplicationServices`, with every AX request written down.

    One behaviour is added, and it only ever subtracts: `AXUIElementPerformAction`
    refuses (and records) any element whose title/description is not one the
    fixture created. See `FixtureApp.pressable`.
    """

    def __init__(self, allowed: set[str] | None = None) -> None:
        self.allowed = allowed
        self.refused_actions: list[str] = []
        self.read_attributes: list[str] = []
        self.set_attributes: list[tuple[str, Any, int]] = []
        self.actions: list[tuple[str, int]] = []
        self.app_pids: list[int] = []
        self.system_wide = 0
        self.messaging_timeouts: list[tuple[float, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(_AS, name)

    def AXUIElementCreateApplication(self, pid):
        self.app_pids.append(int(pid))
        return _AS.AXUIElementCreateApplication(pid)

    def AXUIElementCreateSystemWide(self):
        self.system_wide += 1
        return _AS.AXUIElementCreateSystemWide()

    def AXUIElementCopyAttributeValue(self, ref, attribute, out):
        self.read_attributes.append(str(attribute))
        return _AS.AXUIElementCopyAttributeValue(ref, attribute, out)

    def AXUIElementSetAttributeValue(self, ref, attribute, value):
        err = _AS.AXUIElementSetAttributeValue(ref, attribute, value)
        self.set_attributes.append((str(attribute), value, int(err)))
        return err

    def AXUIElementPerformAction(self, ref, action):
        if self.allowed is not None:
            names = {
                str(_AS.AXUIElementCopyAttributeValue(ref, attr, None)[1] or "")
                for attr in (_AS.kAXTitleAttribute, _AS.kAXDescriptionAttribute)
            }
            if not names & self.allowed:
                self.refused_actions.append(str(action))
                return ax.AX_CANNOT_COMPLETE
        err = _AS.AXUIElementPerformAction(ref, action)
        self.actions.append((str(action), int(err)))
        return err

    def AXUIElementSetMessagingTimeout(self, ref, seconds):
        err = _AS.AXUIElementSetMessagingTimeout(ref, seconds)
        self.messaging_timeouts.append((float(seconds), int(err)))
        return err


@dataclass
class Live:
    fx: FixtureApp
    transport: InProcessEventTransport
    recorder: RecordingAX
    # Every activation daa requested, as (pid, keystrokes delivered so far).
    # The second number is what lets a test prove ORDER: our pid was brought
    # forward before a single key reached it.
    activations: list[tuple[int, int]] = field(default_factory=list)

    @property
    def app_info(self) -> ax.AppInfo:
        return ax.AppInfo(APP_NAME, BUNDLE_ID, os.getpid())


def install(monkeypatch: Any, fx: FixtureApp) -> Live:
    """Point daa's real AX layer at this process. Undone by monkeypatch."""
    transport = InProcessEventTransport(fx)
    recorder = RecordingAX(allowed=fx.pressable)
    info = ax.AppInfo(APP_NAME, BUNDLE_ID, os.getpid())
    monkeypatch.setattr(ax, "running_apps", lambda: [info])
    monkeypatch.setattr(ax, "_api", lambda: recorder)
    monkeypatch.setattr(ax, "_quartz", lambda: transport)
    live = Live(fx=fx, transport=transport, recorder=recorder)

    # Activation, modelled. The fixture must never really become the active
    # app -- it would take focus from the user's work -- so daa's request to
    # bring it forward is RECORDED and honoured here instead. Without this the
    # tools' confirm-frontmost check refuses every keystroke, correctly, and
    # an earlier attempt at this fix hung waiting for an activation the
    # fixture is designed never to perform.
    import daa.tools.computer.tools as tools_mod

    front: dict[str, int | None] = {"pid": None}

    def _activate(pid: int) -> bool:
        live.activations.append((int(pid), len(transport.delivered)))
        front["pid"] = int(pid)
        return True

    monkeypatch.setattr(tools_mod, "activate", _activate)
    monkeypatch.setattr(tools_mod, "frontmost_pid", lambda: front["pid"])
    return live


def frontmost_pid() -> int:
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier()) if front is not None else 0


__all__ = [
    "APP_NAME",
    "BUNDLE_ID",
    "MENU_ITEM",
    "MENU_TITLE",
    "FixtureApp",
    "FixtureWindow",
    "InProcessEventTransport",
    "Live",
    "RecordingAX",
    "SyntheticMouseRefused",
    "frontmost_pid",
    "install",
]
