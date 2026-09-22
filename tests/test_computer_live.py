"""The `ui_*` tools against REAL macOS Accessibility, in-process, with no grant.

Every other `test_computer_*.py` runs on a recorded tree. This file drives the
same tools -- `resolve()` then `run()` -- against a real `NSWindow` of real
AppKit controls, through real `AXUIElement*` calls, and checks the results
through AppKit rather than through daa. It is what keeps the recorded trees
honest: it is where we learned, for instance, that a real `NSSecureTextField`
reports role `AXTextField` with subrole `AXSecureTextField` (the fakes use the
role), and that a real `NSAlert` sheet is an `AXSheet` CHILD of the window,
not a window with an alert subrole.

How it works without an Accessibility grant, and exactly what is substituted,
is in `ax_fixture.py`. In one line: self-process AX is not gated by TCC, so
the fixture is this process; app discovery is pointed at our own pid; and
synthetic keystrokes are delivered in-process instead of at the HID tap
(which would need a PostEvent grant and would type into the user's frontmost
app).

Opt-in only (`-m macos`). It opens windows, invisibly -- alpha 0, ordered to the
back, parked off-screen -- and closes every one of them in teardown.

Tests marked `xfail(strict=True)` are REAL FINDINGS against the current code:
each one asserts the behaviour the design promises, fails today for the reason
given, and will turn into an XPASS failure the moment someone fixes it, so the
marker has to be removed deliberately.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS AppKit"),
]

pytest.importorskip("AppKit", reason="pyobjc-framework-Cocoa is not installed")
pytest.importorskip("ApplicationServices", reason="pyobjc ApplicationServices missing")

from ax_fixture import (
    APP_NAME,
    MENU_ITEM,
    FixtureApp,
    install,
)
from daa.config import Settings
from daa.contracts import RiskTier
from daa.tools.computer import ax
from daa.tools.computer.tools import UiClick, UiDescribe, UiKey, UiSequence, UiType

LIVE = Settings(dry_run=False)
WINDOW = "Account Settings"
NS_ALERT_FIRST_BUTTON = 1000


def _in(label: str, kind: str, window: str = WINDOW) -> str:
    """The sentence naming.compose_target should build for a fixture control."""
    where = f" in {window}" if window else ""
    return f"the {label} {kind}{where} in {APP_NAME}"


# ---------------------------------------------------------------------------
# Fixtures: one app per session, windows per test, everything closed
# ---------------------------------------------------------------------------


def _screen_is_locked() -> bool:
    try:
        import Quartz

        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:  # noqa: BLE001 -- unknown is not locked; let the suite speak
        return False


@pytest.fixture(scope="session")
def fx():
    # While the screen is locked, macOS does not publish this process's
    # windows to Accessibility: kAXWindowsAttribute answers with the
    # application element itself and nothing beneath it is a window. Every
    # test then fails with a misleading "could not find" -- 21 of them, all
    # pointing away from the real cause. That cost three agent runs before
    # anyone checked. Skip, and say why.
    if _screen_is_locked():
        pytest.skip("the screen is locked, so macOS hides the fixture's windows from "
                    "Accessibility; unlock it and run this suite again")
    app = FixtureApp.shared()
    yield app
    app.close_all()


@pytest.fixture
def live(fx, monkeypatch):
    """daa's real AX layer pointed at this process, plus structural checks.

    The teardown checks run after EVERY live test, so each test also proves:
    no AXValue was ever requested from a real tree, no application element was
    ever created for another process, and no synthetic mouse event was built.
    """
    lv = install(monkeypatch, fx)
    fx.target.hits.clear()
    fx.target.callbacks.clear()
    try:
        yield lv
    finally:
        fx.close_all()
    assert fx.visible_windows() == 0, "a fixture window was left on screen"
    reads = set(lv.recorder.read_attributes)
    assert "AXValue" not in reads, "a live walk requested AXValue"
    assert "AXSelectedText" not in reads
    assert set(lv.recorder.app_pids) <= {os.getpid()}, "touched another process"
    assert lv.transport.refused_mouse == [], "the synthetic-click fallback fired"
    assert lv.recorder.refused_actions == [], "a press was aimed at a non-fixture element"
    assert not fx.is_active, "the fixture app became active and took focus"


@pytest.fixture
def settings_window(fx):
    """The standard window: a button, a field, a password field, two popups."""
    win = fx.window(WINDOW)
    controls = {
        "frobnicate": win.button("Frobnicate"),
        "kick": win.button("Kick", key="k", command=True),
        "nickname": win.text_field("Nickname"),
        "password": win.text_field("Password", secure=True),
        "size": win.popup(["Small", "Medium", "Large"], label="Size"),
        "anonymous_popup": win.popup(["Red", "Green"]),
    }
    win.show()
    return win, controls


# ---------------------------------------------------------------------------
# The walk itself
# ---------------------------------------------------------------------------


def test_self_process_ax_answers_without_a_grant(live, settings_window):
    snap = ax.snapshot(APP_NAME)
    assert snap.ok, snap.degraded
    assert snap.ax_error == 0
    assert snap.pid == os.getpid()
    assert snap.nodes_visited > 0
    # The AX calls were real calls against this pid and nothing else.
    assert live.recorder.app_pids == [os.getpid()]


def test_real_roles_and_role_descriptions_come_back_as_appkit_publishes_them(
    live, settings_window
):
    snap = ax.snapshot(APP_NAME)
    by_label = {el.label: el for el in snap.elements}
    assert by_label["Frobnicate"].role == "AXButton"
    assert by_label["Frobnicate"].role_description == "button"
    assert "AXPress" in by_label["Frobnicate"].actions
    assert by_label["Nickname"].role == "AXTextField"
    assert by_label["Nickname"].placeholder == "Nickname"
    # The recorded trees model a password field as role AXSecureTextField.
    # AppKit actually publishes role AXTextField + SUBROLE AXSecureTextField,
    # so it is the subrole branch of Element.is_secure that protects the user.
    pw = by_label["Password"]
    assert (pw.role, pw.subrole) == ("AXTextField", "AXSecureTextField")
    assert pw.role_description == "secure text field"
    assert pw.is_secure and pw.is_text_entry
    assert by_label["Size"].role == "AXPopUpButton"
    # The menu bar is walked as a separate root.
    assert by_label[MENU_ITEM].role == "AXMenuItem"
    assert by_label[MENU_ITEM].window_title == ""


# ---------------------------------------------------------------------------
# ui_describe
# ---------------------------------------------------------------------------


def test_ui_describe_lists_real_controls_and_never_the_users_content(live, settings_window):
    _win, controls = settings_window
    # User content, set through AppKit. None of it may reach daa.
    controls["nickname"].setStringValue_("private draft text")
    controls["password"].setStringValue_("hunter2")

    tool = UiDescribe(LIVE)
    action = tool.resolve(app=APP_NAME)
    result = tool.run(action)

    assert result.ok, result
    names = {row["name"]: row for row in result.data["controls"]}
    assert {"Frobnicate", "Kick", "Nickname", "Password", "Size", MENU_ITEM} <= set(names)
    assert names["Frobnicate"]["kind"] == "button"
    assert names["Nickname"]["kind"] == "text field"
    assert names["Password"]["kind"] == "secure text field"
    assert names["Size"]["kind"] == "pop up button"
    assert names["Frobnicate"]["window"] == WINDOW
    assert result.data["app"] == APP_NAME
    blob = repr(result)
    assert "private draft text" not in blob
    assert "hunter2" not in blob
    # An unlabelled popup's only text is its AXValue ("Red"), which daa never
    # reads -- so it is invisible, not described by its current selection.
    assert "Red" not in names and "Green" not in names


def test_ui_describe_kind_filters_work_on_a_real_tree(live, settings_window):
    tool = UiDescribe(LIVE)
    fields = tool.run(tool.resolve(app=APP_NAME, kind="fields")).data["controls"]
    assert {row["name"] for row in fields} == {"Nickname", "Password"}
    menus = tool.run(tool.resolve(app=APP_NAME, kind="menus")).data["controls"]
    assert MENU_ITEM in {row["name"] for row in menus}


# ---------------------------------------------------------------------------
# ui_click: AXPress really fires the action
# ---------------------------------------------------------------------------


def test_ui_click_axpress_fires_the_real_button_action(live, fx, settings_window):
    tool = UiClick(LIVE)
    action = tool.resolve(target="frobnicate", app=APP_NAME)

    assert action.targets == (_in("Frobnicate", "button"),)
    assert action.floor_hint is None
    assert action.origin == "dev.daa.ax-fixture"
    assert action.consequences["focus"].endswith("without bringing it to the front")
    assert fx.target.hits == []

    result = tool.run(action)

    assert result.ok, result
    assert fx.target.hits == ["Frobnicate"]          # asserted through AppKit
    assert result.data["path"] == "axpress"
    assert ("AXPress", 0) in live.recorder.actions
    # A real limitation, recorded: the action DID fire, but a push button's
    # title/enabled/focused do not change when it is pressed, so the effect
    # heuristic reports a suspected no-op. `_effect` cannot see an action's
    # consequences, only the pressed element's own attributes.
    assert result.data["effect"] == "suspected_noop"


def test_ui_click_presses_a_real_menu_item(live, fx, settings_window):
    tool = UiClick(LIVE)
    action = tool.resolve(target="reticulate splines", app=APP_NAME)
    assert action.targets == (f"the {MENU_ITEM} menu item in {APP_NAME}",)
    result = tool.run(action)
    assert result.ok, result
    # A menu item's action is delivered asynchronously: AXPress returns first,
    # and the action lands on the next turn of the run loop.
    assert fx.target.hits == []
    fx.pump(0.2)
    assert fx.target.hits == [MENU_ITEM]


def test_a_labelled_popup_is_nameable_and_an_unlabelled_one_is_not(live, fx, settings_window):
    tool = UiClick(LIVE)
    labelled = tool.resolve(target="size", app=APP_NAME, window=WINDOW)
    assert labelled.targets == (_in("Size", "pop up button"),)
    # Not run: pressing a popup opens its menu in a modal tracking loop, which
    # would draw on the user's screen. `window=` keeps the Apple menu (see the
    # Apple-menu tests below) out of the candidate pool.
    anonymous = tool.resolve(target="red", app=APP_NAME, window=WINDOW)
    assert anonymous.targets == ()
    assert tool.run(anonymous).ok is False
    assert fx.target.hits == []


def test_a_disabled_real_button_is_refused(live, fx, settings_window):
    _, controls = settings_window
    controls["frobnicate"].setEnabled_(False)
    tool = UiClick(LIVE)
    action = tool.resolve(target="frobnicate", app=APP_NAME)
    assert action.targets == ()
    assert "greyed out" in action.args["reason"]
    tool.run(action)
    assert fx.target.hits == []


# ---------------------------------------------------------------------------
# The readback from a real alert sheet
# ---------------------------------------------------------------------------


@pytest.fixture
def delete_sheet(fx):
    win = fx.window(WINDOW)
    win.button("Frobnicate")
    win.show()
    win.alert_sheet("Delete your account?", ["Delete Account", "Cancel"])
    return win


def test_delete_account_in_a_real_alert_reads_back_as_destructive(live, fx, delete_sheet):
    tool = UiClick(LIVE)
    action = tool.resolve(target="the OK button", app=APP_NAME, window=WINDOW)
    # The model's phrasing does not match anything, so nothing resolves: the
    # readback is never borrowed from the request.
    assert action.targets == ()

    action = tool.resolve(target="delete account", app=APP_NAME)
    assert action.targets == (_in("Delete Account", "button"),)
    assert action.args["requested_target"] == "delete account"
    assert action.floor_hint == RiskTier.CONFIRM_VISUAL
    assert action.consequences["wording"] == "Delete Account is a destructive label"

    result = tool.run(action)
    fx.pump(0.3)
    assert result.ok, result
    assert delete_sheet.responses == [NS_ALERT_FIRST_BUTTON]   # the sheet really answered
    # The same limitation as a push button: the sheet really closed, but the
    # button's own attributes are unchanged and its handle still answers (the
    # NSAlert keeps its window alive), so the effect reads as a no-op.
    assert result.data["effect"] == "suspected_noop"


def test_buttons_inside_a_real_alert_sheet_are_known_to_be_in_an_alert(live, fx):
    win = fx.window(WINDOW)
    win.show()
    win.alert_sheet("Continue with the migration?", ["Continue", "Cancel"])
    snap = ax.snapshot(APP_NAME, window=WINDOW)
    by_label = {el.label: el for el in snap.elements}
    assert by_label["Continue"].in_alert
    assert by_label["Continue"].is_default_button
    action = UiClick(LIVE).resolve(target="continue", app=APP_NAME)
    assert action.consequences.get("modal") == "this is the default button of an alert"
    assert action.floor_hint == RiskTier.CONFIRM_VISUAL


# ---------------------------------------------------------------------------
# ui_type: the field's contents really change
# ---------------------------------------------------------------------------


def test_ui_type_really_changes_the_field(live, fx, settings_window):
    _, controls = settings_window
    text = "Grace Hopper, née Murray ✓"
    tool = UiType(LIVE)
    action = tool.resolve(text=text, target="nickname", app=APP_NAME)

    assert action.targets == (_in("Nickname", "text field"),)
    assert action.consequences["text"] == f"typing this: {text}"
    assert action.floor_hint is None

    result = tool.run(action)

    assert result.ok, result
    # Read back through AppKit. daa itself never reads AXValue and must not.
    assert controls["nickname"].stringValue() == text
    assert controls["password"].stringValue() == ""
    # The only AX write was focus, and it succeeded on the real element.
    assert live.recorder.set_attributes == [("AXFocused", True, 0)]
    # The exact CGEvents daa built carried exactly the approved characters,
    # in more than one 16-character chunk (the text is 26 characters).
    assert live.transport.typed_text() == text


def test_ui_type_makes_the_named_app_the_one_that_receives_keystrokes(live, fx, settings_window):
    """Keystrokes go to the FRONTMOST app, not to the named element, so daa
    must bring the named app forward -- and it must do so BEFORE the first key.

    The fixture never really becomes active (it would steal the user's focus),
    so activation is modelled and recorded; `test_computer_front.py` covers the
    refusal paths without AppKit at all."""
    tool = UiType(LIVE)
    result = tool.run(tool.resolve(text="x", target="nickname", app=APP_NAME))
    assert result.ok, result
    assert live.activations, "nothing asked for the named app to come forward"
    pid, keys_before = live.activations[0]
    assert pid == os.getpid(), "brought the wrong process forward"
    assert keys_before == 0, "a keystroke was delivered before the app was in front"


# ---------------------------------------------------------------------------
# ui_key
# ---------------------------------------------------------------------------


def test_ui_key_builds_a_real_combination_that_fires_a_key_equivalent(live, fx, settings_window):
    tool = UiKey(LIVE)
    action = tool.resolve(keys="cmd-k", app=APP_NAME)
    assert action.targets == (f"command K in {WINDOW} in {APP_NAME}",)
    result = tool.run(action)
    assert result.ok, result
    assert result.data["effect"] == "unverifiable"
    assert fx.target.hits == ["Kick"]


def test_ui_key_brings_the_app_to_the_front_as_its_readback_says(live, fx, settings_window):
    tool = UiKey(LIVE)
    action = tool.resolve(keys="command k", app=APP_NAME)
    assert action.consequences["focus"] == f"this will bring {APP_NAME} to the front"
    result = tool.run(action)
    # The readback promised it; run() must now actually do it, first.
    assert result.ok, result
    assert live.activations and live.activations[0] == (os.getpid(), 0)


# ---------------------------------------------------------------------------
# run() re-verification against a real change
# ---------------------------------------------------------------------------


def test_run_refuses_when_the_real_button_is_removed_after_resolve(live, fx, settings_window):
    win, controls = settings_window
    tool = UiClick(LIVE)
    action = tool.resolve(target="frobnicate", app=APP_NAME)
    assert action.targets

    win.remove(controls["frobnicate"])
    result = tool.run(action)

    assert result.ok is False
    assert result.summary == "The screen changed, so I stopped."
    assert fx.target.hits == []
    assert live.recorder.actions == []


def test_run_refuses_when_an_identical_twin_appears_after_resolve(live, fx, settings_window):
    win, _ = settings_window
    tool = UiClick(LIVE)
    action = tool.resolve(target="frobnicate", app=APP_NAME)
    assert action.targets

    win.button("Frobnicate")   # same role, same title, same window: same identity
    fx.pump(0.02)
    result = tool.run(action)

    assert result.ok is False
    assert result.summary == "There is now more than one of those, so I stopped."
    assert fx.target.hits == []
    assert live.recorder.actions == []


def test_ui_type_refuses_when_a_password_field_takes_the_fields_place(live, fx, settings_window):
    win, controls = settings_window
    tool = UiType(LIVE)
    action = tool.resolve(text="not a password", target="nickname", app=APP_NAME)
    assert action.targets

    win.remove(controls["nickname"])
    impostor = win.text_field("Nickname", secure=True)   # same placeholder, now secure
    fx.pump(0.02)
    result = tool.run(action)

    assert result.ok is False
    assert impostor.stringValue() == ""
    assert live.transport.delivered == []
    assert live.recorder.set_attributes == []


# ---------------------------------------------------------------------------
# Secure fields
# ---------------------------------------------------------------------------


def test_ui_type_refuses_a_real_secure_text_field(live, fx, settings_window):
    _, controls = settings_window
    tool = UiType(LIVE)
    action = tool.resolve(text="hunter2", target="password", app=APP_NAME)
    assert action.targets == ()
    assert "password field" in action.args["reason"]
    result = tool.run(action)
    assert result.ok is False
    assert controls["password"].stringValue() == ""
    assert live.recorder.set_attributes == []
    assert live.transport.delivered == []


def test_ui_sequence_refuses_a_bound_step_into_a_real_secure_field(live, fx, settings_window):
    tool = UiSequence(LIVE)
    action = tool.resolve(
        app=APP_NAME,
        steps=[{"action": "click", "target": "frobnicate"},
               {"action": "type", "target": "password", "text": "hunter2"}],
    )
    assert action.targets == ()
    assert "password field" in action.args["reason"]
    tool.run(action)
    assert fx.target.hits == []


def test_a_deferred_type_step_refuses_when_a_real_password_field_has_the_cursor(
    live, fx, settings_window
):
    _, controls = settings_window
    secure = next(el for el in ax.snapshot(APP_NAME).elements if el.label == "Password")
    assert ax.focus(secure) == (True, "")
    fx.pump(0.02)
    tool = UiSequence(LIVE)
    action = tool.resolve(app=APP_NAME, steps=[{"action": "type", "text": "hunter2"}])
    assert action.targets and action.args["steps"][0]["bound"] is False
    result = tool.run(action)
    assert result.ok is False
    assert controls["password"].stringValue() == ""


# ---------------------------------------------------------------------------
# ui_sequence end to end
# ---------------------------------------------------------------------------


def test_ui_sequence_types_presses_and_keys_for_real(live, fx, settings_window):
    _, controls = settings_window
    tool = UiSequence(LIVE)
    action = tool.resolve(
        app=APP_NAME,
        steps=[
            {"action": "type", "target": "nickname", "text": "Ada"},
            {"action": "click", "target": "frobnicate"},
            {"action": "key", "keys": "command k"},
        ],
    )
    expected = (
        f"these three steps in {APP_NAME}: "
        f"type 'Ada' into {_in('Nickname', 'text field')}, "
        f"then press {_in('Frobnicate', 'button')}, then press command K"
    )
    assert action.targets == (expected,)
    assert action.floor_hint is None

    result = tool.run(action)

    assert result.ok, result
    assert result.data["steps_done"] == 3
    assert controls["nickname"].stringValue() == "Ada"
    assert fx.target.hits == ["Frobnicate", "Kick"]


def test_ui_sequence_stops_when_a_real_step_removes_the_next_target(live, fx):
    win = fx.window(WINDOW)
    kick = {}
    win.button("Frobnicate", on_hit=lambda: win.remove(kick["view"]))
    kick["view"] = win.button("Kick")
    win.show()
    try:
        tool = UiSequence(LIVE)
        action = tool.resolve(
            app=APP_NAME,
            steps=[{"action": "click", "target": "frobnicate"},
                   {"action": "click", "target": "kick"}],
        )
        assert action.targets
        result = tool.run(action)
    finally:
        fx.target.callbacks.clear()
    assert result.ok is False
    assert (result.data["steps_done"], result.data["stopped_at"]) == (1, 2)
    assert "screen changed" in result.summary
    assert fx.target.hits == ["Frobnicate"]


# ---------------------------------------------------------------------------
# Bounds under a real tree
# ---------------------------------------------------------------------------


def test_node_cap_truncates_a_real_walk(live, fx):
    win = fx.window(WINDOW, size=(600.0, 400.0))
    win.many_buttons(300)
    win.show()
    snap = ax.snapshot(APP_NAME, max_nodes=50, include_menu_bar=False)
    assert snap.ok
    assert snap.truncated
    assert snap.nodes_visited == 50
    assert len(snap.elements) < 50


def test_depth_cap_hides_a_control_nested_too_deep(live, fx):
    win = fx.window(WINDOW)
    win.nested_groups(20, "Deep Button")
    win.show()
    shallow = ax.snapshot(APP_NAME)
    assert shallow.truncated
    assert "Deep Button" not in {el.label for el in shallow.elements}
    assert max(el.depth for el in shallow.elements if el.window_title) == ax.MAX_DEPTH
    refused = UiClick(LIVE).resolve(target="deep button", app=APP_NAME)
    assert refused.targets == ()

    deep = ax.snapshot(APP_NAME, max_depth=40)
    assert "Deep Button" in {el.label for el in deep.elements}


def test_wall_clock_budget_truncates_a_real_walk(live, fx):
    win = fx.window(WINDOW, size=(600.0, 400.0))
    win.many_buttons(1500)
    win.show()
    full = ax.snapshot(APP_NAME, max_nodes=100_000, budget_s=60.0, include_menu_bar=False)
    assert not full.truncated
    snap = ax.snapshot(APP_NAME, max_nodes=100_000, budget_s=0.0, include_menu_bar=False)
    assert snap.truncated
    assert snap.nodes_visited < full.nodes_visited
    # The floor is 50 ms; one extra element read of slack is generous at 450.
    assert snap.elapsed_ms < 500.0


def test_the_messaging_timeout_is_really_applied_to_the_app_element(live, settings_window):
    ax.snapshot(APP_NAME)
    assert live.recorder.messaging_timeouts == [(ax.MESSAGING_TIMEOUT_S, 0)]
    # What this cannot show: the timeout FIRING. A self-process AX request is
    # served on this same thread, so a hung target cannot be simulated here;
    # that needs a second process, and a second process needs the grant.


# ---------------------------------------------------------------------------
# The Apple menu: found by running against a real menu bar
# ---------------------------------------------------------------------------
#
# Every Cocoa app's AXMenuBar has the system Apple menu as its first item, and
# AppKit fills it in lazily -- the first walk sees an empty "Apple" menu bar
# item, every later walk sees Restart…, Shut Down…, Log Out <user's full
# name>…, Lock Screen, Force Quit… and the Recent Items submenu (the names of
# the user's recently opened files, folders and servers). The recorded trees
# never contained any of it. These tests never PRESS any of it: RecordingAX
# refuses a press on anything the fixture did not create, and none of them
# call run(). Nor do they print any label, because on the user's machine those
# labels are the user's own files.


def _apple_menu_items(snap: ax.Snapshot) -> list[ax.Element]:
    return [el for el in snap.elements if el.path[:2] == (-1, 0) and el.role == "AXMenuItem"]


def _warm_apple_menu(fx) -> ax.Snapshot:
    ax.snapshot(APP_NAME)            # the first walk asks AppKit to populate it...
    fx.pump(0.3)                     # ...which it does on the next run-loop turn
    return ax.snapshot(APP_NAME)


def test_the_menu_bar_walk_reaches_the_system_apple_menu(live, fx, settings_window):
    snap = _warm_apple_menu(fx)
    items = _apple_menu_items(snap)
    if not items:
        pytest.skip("this macOS did not populate the Apple menu for the fixture")
    # Counts only: the labels include the user's recent files and full name.
    assert len(items) > 5
    assert all(el.window_title == "" and el.app == APP_NAME for el in items)


def test_system_apple_menu_items_are_not_pressable_candidates(live, fx, settings_window):
    snap = _warm_apple_menu(fx)
    labels = {el.label for el in _apple_menu_items(snap)}
    # Not "Restart…": that one is refused today, but only by luck -- its hidden
    # option-key alternate "Restart" makes the match ambiguous. "Lock Screen"
    # has no twin.
    if "Lock Screen" not in labels:
        pytest.skip("no English 'Lock Screen' item in this Apple menu")
    action = UiClick(LIVE).resolve(target="Lock Screen", app=APP_NAME)   # never run
    assert action.targets == () or action.floor_hint == RiskTier.CONFIRM_VISUAL


def test_ui_describe_does_not_list_the_system_apple_menu(live, fx, settings_window):
    _warm_apple_menu(fx)
    tool = UiDescribe(LIVE)
    result = tool.run(tool.resolve(app=APP_NAME, kind="menus"))
    menu_names = {row["name"] for row in result.data["controls"]}
    fixture_names = {"Apple", "python", "Python", MENU_ITEM, APP_NAME}
    leaked = len(menu_names - fixture_names)
    assert leaked == 0


# ---------------------------------------------------------------------------
# evals/ax_coverage.py against a real tree
# ---------------------------------------------------------------------------


def test_the_coverage_harness_walks_a_real_tree_read_only_and_prints_no_labels(
    live, fx, settings_window
):
    from pathlib import Path

    import ApplicationServices

    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from evals import ax_coverage as cov

    _warm_apple_menu(fx)
    fx.target.hits.clear()
    row = cov.walk_app(live.app_info, cov.ReadOnlyAX(ApplicationServices))

    assert row.reachable and row.ax_error == 0
    assert row.windows == 1
    # Frobnicate, Kick, Nickname, Password and the labelled popup.
    assert row.nameable == 5
    # Plus the unlabelled popup and the window's close/zoom/minimize buttons.
    assert row.actionable == 9
    assert row.unnameable_roles == {"AXButton": 3, "AXPopUpButton": 1}
    assert row.menu_nameable >= 2          # the app's bar item + Reticulate Splines
    assert row.apple_menu_items > 5
    assert fx.target.hits == []            # nothing was pressed by walking
    table = cov.render([row], cov.summarise([row]), trusted=False)
    for text in ("Frobnicate", "Nickname", "Password", WINDOW, APP_NAME, MENU_ITEM):
        assert text not in table
