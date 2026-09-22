"""The tree walk itself, against an in-memory Accessibility API.

The live suite (`test_computer_live.py`) drives a real AppKit window, but it
needs an UNLOCKED screen: while the screen is locked macOS publishes none of
the process's windows, and that suite skips. These tests pin the walk's
structural rules without depending on the screen at all, so they run in the
default suite everywhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from daa.tools.computer import ax

OK = 0
UNSUPPORTED = -25205


class Node:
    """One AX element. Attributes are keyed by the SAME names the fake API
    exposes as its kAX* constants, so the walk reads them unmodified."""

    def __init__(self, role: str, title: str = "", **attrs: Any) -> None:
        self.attrs: dict[str, Any] = {"AXRole": role, "AXTitle": title, **attrs}
        self.children: list[Node] = []
        self.actions: list[str] = ["AXPress"] if role in {"AXButton", "AXMenuItem"} else []

    def add(self, *kids: Node) -> Node:
        self.children.extend(kids)
        return self

    def __repr__(self) -> str:
        return f"<{self.attrs['AXRole']} {self.attrs['AXTitle']!r}>"


class FakeAPI:
    """Just enough of ApplicationServices for `ax.snapshot`."""

    kAXRoleAttribute = "AXRole"
    kAXSubroleAttribute = "AXSubrole"
    kAXRoleDescriptionAttribute = "AXRoleDescription"
    kAXTitleAttribute = "AXTitle"
    kAXDescriptionAttribute = "AXDescription"
    kAXHelpAttribute = "AXHelp"
    kAXPlaceholderValueAttribute = "AXPlaceholderValue"
    kAXIdentifierAttribute = "AXIdentifier"
    kAXEnabledAttribute = "AXEnabled"
    kAXFocusedAttribute = "AXFocused"
    kAXChildrenAttribute = "AXChildren"
    kAXWindowsAttribute = "AXWindows"
    kAXMenuBarAttribute = "AXMenuBar"
    kAXDefaultButtonAttribute = "AXDefaultButton"
    kAXPositionAttribute = "AXPosition"
    kAXSizeAttribute = "AXSize"
    kAXValueCGPointType = 1
    kAXValueCGSizeType = 2

    def __init__(self, app: Node, windows: list[Node], menu_bar: Node | None = None) -> None:
        self.app = app
        self.windows = windows
        self.menu_bar = menu_bar
        self.reads = 0

    def AXUIElementCreateApplication(self, pid: int) -> Node:
        return self.app

    def AXUIElementSetMessagingTimeout(self, ref: Any, t: float) -> int:
        return OK

    def AXUIElementCopyAttributeValue(self, ref: Node, attr: str, _: Any) -> tuple[int, Any]:
        self.reads += 1
        if attr == "AXWindows" and ref is self.app:
            return OK, list(self.windows)
        if attr == "AXMenuBar" and ref is self.app:
            return (OK, self.menu_bar) if self.menu_bar is not None else (UNSUPPORTED, None)
        if attr == "AXChildren":
            return OK, list(ref.children)
        if attr in ref.attrs:
            return OK, ref.attrs[attr]
        return UNSUPPORTED, None

    def AXUIElementCopyActionNames(self, ref: Node, _: Any) -> tuple[int, Any]:
        return OK, list(ref.actions)


APP = ax.AppInfo("Fixture", "dev.daa.test", 4242)


@pytest.fixture
def walk(monkeypatch):
    def _walk(api: FakeAPI, **kw: Any) -> ax.Snapshot:
        monkeypatch.setattr(ax, "_api", lambda: api)
        return ax.snapshot("Fixture", apps=[APP], **kw)

    return _walk


def labels(snap: ax.Snapshot) -> list[str]:
    return [e.label for e in snap.elements]


# --- the app listed as its own window ----------------------------------------


def test_an_app_that_lists_itself_as_its_window_does_not_send_the_walk_into_a_loop(walk):
    """Some processes answer kAXWindowsAttribute with the APPLICATION, and list
    the application among its own children. Unguarded, the walk descended
    app -> app -> app to the depth cap, burning the node budget on a cycle."""
    app = Node("AXApplication", "Fixture")
    window = Node("AXWindow", "Account Settings").add(Node("AXButton", "Frobnicate"))
    app.add(app, window)                       # itself, then its real window
    snap = walk(FakeAPI(app, windows=[app]), include_menu_bar=False)

    assert "Frobnicate" in labels(snap)
    frob = next(e for e in snap.elements if e.label == "Frobnicate")
    assert frob.window_title == "Account Settings"
    assert not any(e.role == "AXApplication" for e in snap.elements)
    assert snap.nodes_visited < 10, f"the walk looped: {snap.nodes_visited} nodes"


def test_a_window_whose_subtree_contains_the_app_is_not_walked_back_into_it(walk):
    app = Node("AXApplication", "Fixture")
    window = Node("AXWindow", "Main").add(Node("AXButton", "Save"), app)
    snap = walk(FakeAPI(app, windows=[window]), include_menu_bar=False)
    assert labels(snap) == ["Save"]
    assert snap.nodes_visited < 10


# --- alert sheets --------------------------------------------------------------


def test_buttons_inside_an_alert_sheet_inherit_the_alert_and_its_default_button(walk):
    """A real NSAlert sheet is an AXSheet CHILD of its window. Whether a button
    is 'in an alert' is a property of its ancestry, and the default button
    belongs to the sheet, not the window."""
    app = Node("AXApplication", "Fixture")
    cont = Node("AXButton", "Continue")
    sheet = Node("AXSheet", "", AXDefaultButton=cont).add(cont, Node("AXButton", "Cancel"))
    window = Node("AXWindow", "Account Settings").add(Node("AXButton", "Help"), sheet)
    snap = walk(FakeAPI(app, windows=[window]), include_menu_bar=False)
    by = {e.label: e for e in snap.elements}

    assert by["Continue"].in_alert and by["Cancel"].in_alert
    assert by["Continue"].is_default_button
    assert not by["Cancel"].is_default_button
    assert not by["Help"].in_alert, "a control beside the sheet is not in the alert"


# --- the Apple menu -------------------------------------------------------------


def _menu_bar() -> Node:
    apple = Node("AXMenuBarItem", "Apple").add(
        Node("AXMenu", "").add(
            Node("AXMenuItem", "Lock Screen"),
            Node("AXMenuItem", "Restart…"),
            Node("AXMenuItem", "Log Out Sanjay…"),
        )
    )
    file_ = Node("AXMenuBarItem", "File").add(Node("AXMenu", "").add(Node("AXMenuItem", "Open")))
    return Node("AXMenuBar", "").add(apple, file_)


def test_the_apple_menu_is_never_walked(walk):
    """Lock Screen / Restart / Log Out <full name> / Recent Items belong to the
    machine, not the app. Offering them read 'lock screen' back as an in-app
    action and put the user's name and recent files in the model's context."""
    app = Node("AXApplication", "Fixture")
    window = Node("AXWindow", "Main").add(Node("AXButton", "Save"))
    snap = walk(FakeAPI(app, windows=[window], menu_bar=_menu_bar()))
    got = labels(snap)

    assert "Open" in got and "File" in got, "the app's own menus are still walked"
    for system in ("Lock Screen", "Restart…", "Log Out Sanjay…", "Apple"):
        assert system not in got


def test_the_apple_menu_is_identified_by_position_not_by_title(walk):
    """Titles are localised. A German Apple menu is still skipped."""
    bar = _menu_bar()
    bar.children[0].attrs["AXTitle"] = "Apfel"
    bar.children[0].children[0].children[0].attrs["AXTitle"] = "Bildschirm sperren"
    app = Node("AXApplication", "Fixture")
    snap = walk(FakeAPI(app, windows=[Node("AXWindow", "W").add(Node("AXButton", "Ok"))],
                        menu_bar=bar))
    assert "Bildschirm sperren" not in labels(snap)
    assert "Open" in labels(snap)


# --- the coverage harness shares these rules -----------------------------------


def test_the_coverage_harness_does_not_count_menus_as_window_controls():
    """The harness had its own copy of the window walk. For a process that
    lists the application as its own window, it descended app -> menu bar
    and counted every menu item as a window control -- 474 on the live
    fixture, against 5 real controls. That is the one number the harness
    exists to measure, overstated by two orders of magnitude."""
    from evals import ax_coverage

    app = Node("AXApplication", "Fixture")
    window = Node("AXWindow", "Account Settings").add(
        Node("AXButton", "Frobnicate"), Node("AXButton", "Cancel")
    )
    menu = Node("AXMenuBar", "").add(
        *[Node("AXMenuBarItem", f"Menu {i}").add(
            Node("AXMenu", "").add(*[Node("AXMenuItem", f"Item {i}.{j}") for j in range(20)])
        ) for i in range(6)]
    )
    app.add(app, window, menu)                 # itself, its window, its menu bar
    api = ax_coverage.ReadOnlyAX(FakeAPI(app, windows=[app], menu_bar=menu))

    row = ax_coverage.walk_app(APP, api)

    assert row.nameable == 2, f"counted {row.nameable} window controls, expected the 2 buttons"
    assert row.windows == 1
