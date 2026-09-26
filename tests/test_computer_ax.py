"""The accessibility layer: bounds, error discrimination, and what it may read.

The most important test in this file is the one that reads the source. Every
other rule here is a behaviour that a future edit could preserve while breaking
the guarantee; "this module never asks for AXValue" is a property of the text,
and it is the property that decides whether `ui_click.resolve()` -- which runs
before any risk gate -- is able to read the contents of your screen or only the
names of its controls.
"""

from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

import pytest

from computer_tree import el, field, tree
from daa.tools.base import Degraded
from daa.tools.computer import ax
from daa.tools.permissions import ACCESSIBILITY

AX_SOURCE = Path(inspect.getfile(ax)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What the walk is structurally unable to read
# ---------------------------------------------------------------------------


def test_the_walk_never_asks_for_ax_value():
    """AXValue is the user's content: the email body, the document, the password.

    A label is written by the app's developer; a value is written by the user.
    The design note in the plan put AXValue in the naming chain as a fallback;
    it is dropped here, because a resolver that reads it is a screen reader
    running before the gate, and the fallback buys almost nothing -- an element
    with no title, description, help or placeholder is not something a spoken
    target can match against anyway.
    """
    assert "kAXValueAttribute" not in AX_SOURCE
    assert "AXSelectedText" not in AX_SOURCE


def test_label_sources_are_author_written_only():
    element = ax.Element(title="", description="", help="Send this message", placeholder="")
    assert element.label == "Send this message"
    assert ax.Element(title="Save", description="ignored").label == "Save"
    assert ax.Element().label == ""


def test_labels_are_clipped_so_a_paragraph_can_never_become_a_target():
    element = ax.Element(title="x" * 400)
    assert len(element.label) <= ax.MAX_LABEL_CHARS


def test_secure_fields_are_recognised_by_role_and_by_subrole():
    assert ax.Element(role="AXSecureTextField").is_secure
    assert ax.Element(role="AXTextField", subrole="AXSecureTextField").is_secure
    assert not ax.Element(role="AXTextField").is_secure


# ---------------------------------------------------------------------------
# Identity and tokens
# ---------------------------------------------------------------------------


def test_identity_ignores_the_tree_path():
    """A toolbar button that shifts one slot is the same button."""
    a = el("Save", path=(0, 3))
    b = el("Save", path=(0, 4))
    assert a.identity == b.identity


def test_identity_separates_two_different_controls():
    assert el("Save").identity != el("Save As…").identity
    assert el("Save", window="Untitled").identity != el("Save", window="Budget").identity
    assert el("Save", app="TextEdit").identity != el(
        "Save", app="Pages", bundle_id="com.apple.iWork.Pages"
    ).identity


def test_the_handle_is_not_part_of_identity_or_equality():
    one, two = el("Save"), el("Save")
    assert one.ref is not two.ref
    assert one == two and one.identity == two.identity


def test_a_token_goes_stale_on_purpose():
    element = el("Save")
    assert ax.token("snapA", element) != ax.token("snapB", element)
    assert ax.token("snapA", element).endswith(element.identity)


# ---------------------------------------------------------------------------
# Error discrimination: three failures, three sentences
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,capability",
    [
        (ax.AX_API_DISABLED, ACCESSIBILITY),
        (ax.AX_NOT_IMPLEMENTED, "ax_tree"),
        (ax.AX_ATTRIBUTE_UNSUPPORTED, "ax_tree"),
        (ax.AX_CANNOT_COMPLETE, "ax_tree"),
        (-999, "ax_tree"),
    ],
)
def test_every_ax_error_degrades_with_a_spoken_remedy(code, capability):
    degraded = ax.explain_ax_error(code, "Slack")
    assert isinstance(degraded, Degraded)
    assert degraded.capability == capability
    assert degraded.remedy and degraded.remedy[0].isupper() or "Slack" in degraded.remedy
    assert degraded.detail


def test_a_missing_grant_and_a_silent_app_do_not_read_back_the_same():
    """-25211 is "you have no grant"; -25208 is "this app answers nothing".

    Conflating them produces a tool that tells the user to open System Settings
    when the real answer is that Warp has no element tree and never will.
    """
    no_grant = ax.explain_ax_error(ax.AX_API_DISABLED, "Warp")
    no_tree = ax.explain_ax_error(ax.AX_NOT_IMPLEMENTED, "Warp")
    assert no_grant.remedy != no_tree.remedy
    assert "Accessibility" in no_grant.remedy
    assert "Accessibility" not in no_tree.remedy


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_the_walk_is_bounded_by_construction():
    assert 0 < ax.MAX_DEPTH <= 20
    assert 0 < ax.MAX_NODES <= 2000
    assert 0 < ax.WALK_BUDGET_S <= 3.0
    assert 0 < ax.MESSAGING_TIMEOUT_S <= 1.0
    assert ax.MAX_MENU_NODES < ax.MAX_NODES


def test_the_audit_shape_of_a_walk_carries_no_labels():
    snap = tree(el("Delete Account"), field("Password", secure=True))
    shape = snap.shape()
    blob = repr(shape)
    assert "Delete Account" not in blob and "Password" not in blob
    assert shape["elements"] == 2 and shape["roles"]["AXButton"] == 1


# ---------------------------------------------------------------------------
# Degradation with zero permissions -- the state this machine is actually in
# ---------------------------------------------------------------------------


def test_snapshot_degrades_rather_than_raising_when_nothing_is_running():
    snap = ax.snapshot("TextEdit", apps=[])
    assert snap.degraded is not None and not snap.ok
    assert snap.degraded.remedy


def test_snapshot_degrades_when_the_named_app_is_not_running():
    snap = ax.snapshot(
        "Definitely Not An App", apps=[ax.AppInfo("Finder", "com.apple.finder", 1)]
    )
    assert snap.degraded is not None
    assert "does not seem to be running" in snap.degraded.remedy


def test_snapshot_never_raises_against_the_real_machine():
    """No grant on this machine, so this is the degraded path, live."""
    started = time.monotonic()
    snap = ax.snapshot("Finder")
    assert isinstance(snap, ax.Snapshot)
    assert time.monotonic() - started < 6.0
    if snap.degraded is not None:
        assert snap.degraded.remedy


def test_hit_test_and_focus_probes_never_raise_without_a_grant():
    assert ax.hit_test(10.0, 10.0) is None or isinstance(ax.hit_test(10.0, 10.0), ax.Element)
    assert ax.focused_element() is None or isinstance(ax.focused_element(), ax.Element)


def test_input_primitives_refuse_cleanly_with_no_handle():
    ok, detail = ax.press(ax.Element(actions=("AXPress",)))
    assert ok is False and detail
    ok, detail = ax.click_center(ax.Element(position=None, size=None))
    assert ok is False and detail
    ok, detail = ax.focus(ax.Element())
    assert ok is False and detail
    ok, detail = ax.type_text("")
    assert ok is False and detail


def test_wants_manual_accessibility_spots_the_chromium_family():
    assert ax.wants_manual_accessibility(ax.AppInfo("Slack", "com.tinyspeck.slackmacgap", 1))
    assert ax.wants_manual_accessibility(ax.AppInfo("Google Chrome", "com.google.Chrome", 1))
    assert not ax.wants_manual_accessibility(ax.AppInfo("Finder", "com.apple.finder", 1))


def test_enhanced_user_interface_is_never_touched():
    """Setting it makes Chromium animate window moves, which breaks focus_window."""
    assert "AXEnhancedUserInterface" not in AX_SOURCE.replace(
        "`AXEnhancedUserInterface` also works and is NEVER used here", ""
    ).replace("# `AXEnhancedUserInterface`", "")


# ---------------------------------------------------------------------------
# Live, but needing no grant at all: the Stage 0b result, as a test
# ---------------------------------------------------------------------------


def _screen_is_locked() -> bool:
    """Same check as test_computer_live.py, for the same reason.

    While the screen is locked macOS does not publish this process's windows
    to Accessibility, so the probe below finds the application element and no
    window beneath it -- and fails with `assert 'daa ax probe window' in
    ['python']`, which points at the AX claim being wrong rather than at the
    lock. The live suite learned this the expensive way and skips; this test
    was written before that and kept failing misleadingly on its own.
    """
    try:
        import Quartz  # type: ignore

        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:  # noqa: BLE001 -- unknown is not locked; let the test speak
        return False


@pytest.mark.macos
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_ax_window_titles_come_from_the_app_not_from_the_window_server():
    """`kAXTitleAttribute` is answered by the target app over the AX channel.

    This is the Stage 0b experiment as a regression test. A process can read
    its OWN window title through the Accessibility API with no Accessibility
    grant at all -- the value is produced by AppKit inside the process that
    owns the window, not read out of the WindowServer's window list, which is
    the thing Screen Recording redacts. That is why this package asks for no
    Screen Recording grant, takes no screenshots, and still says "in Account
    Settings in Safari".
    """
    if _screen_is_locked():
        pytest.skip("the screen is locked, so macOS hides this process's windows from "
                    "Accessibility; unlock it and run this test again")

    import AppKit  # type: ignore
    from ApplicationServices import (  # type: ignore
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        kAXTitleAttribute,
        kAXWindowsAttribute,
    )

    title = "daa ax probe window"
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, 200, 80),
        AppKit.NSWindowStyleMaskTitled,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setTitle_(title)
    window.orderBack_(None)  # never in front of the user
    AppKit.NSAccessibilityPostNotification(
        window, AppKit.NSAccessibilityWindowCreatedNotification
    )
    try:
        for _ in range(20):
            AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        element = AXUIElementCreateApplication(AppKit.NSProcessInfo.processInfo().processIdentifier())
        err, windows = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute, None)
        assert err == 0 and windows
        titles = []
        for win in windows:
            terr, value = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
            if terr == 0 and value:
                titles.append(str(value))
        assert title in titles
    finally:
        window.close()
