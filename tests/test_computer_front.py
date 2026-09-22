"""Keystrokes go to the app the readback named, or nowhere.

Synthetic keystrokes are delivered to the FRONTMOST app, not to the element
that was named. ui_type used to put the cursor in the named field and then
post keys that landed in whatever the user was looking at; ui_key promised
"this will bring <app> to the front" and never did. Now every keystroke-
sending path brings the target forward and CONFIRMS it before sending
anything -- and sends nothing if it cannot.
"""

from __future__ import annotations

import pytest
from tests.computer_tree import el, field, serve, tree

import daa.tools.computer.tools as tools_mod
from daa.config import Settings
from daa.tools.computer import UiKey, UiSequence, UiType

LIVE = Settings(dry_run=False, enable_computer_use=True)
TARGET_PID = 4242          # computer_tree's fake pid
SOMEONE_ELSE = 999


@pytest.fixture
def sent(monkeypatch):
    """Record every keystroke instead of posting it, and make the snapshot
    return a tree containing an ordinary text field and a button."""
    log: list[tuple[str, object]] = []
    monkeypatch.setattr(tools_mod, "type_text", lambda text, **kw: (log.append(("type", text)), (True, ""))[1])
    monkeypatch.setattr(tools_mod, "post_key", lambda code, flags: (log.append(("key", code)), (True, ""))[1])
    monkeypatch.setattr(tools_mod, "focus", lambda element: (True, ""))
    monkeypatch.setattr(tools_mod, "press", lambda element: (True, ""))
    monkeypatch.setattr(tools_mod, "focused_element", lambda: field("Notes"))
    monkeypatch.setattr(tools_mod, "snapshot", serve(tree(field("Notes"), el("Save"))))
    monkeypatch.setattr(tools_mod, "ACTIVATE_WAIT_S", 0.0)   # the failure path is instant
    return log


def _stuck_behind(monkeypatch, pid: int | None) -> None:
    """Activation is requested but the target never actually comes forward."""
    monkeypatch.setattr(tools_mod, "activate", lambda p: True)
    monkeypatch.setattr(tools_mod, "frontmost_pid", lambda: pid)


# --- the failure path: nothing is sent -----------------------------------------


@pytest.mark.parametrize("front", [SOMEONE_ELSE, None], ids=["another-app", "unknown"])
def test_ui_type_sends_nothing_unless_the_named_app_is_confirmed_in_front(monkeypatch, sent, front):
    _stuck_behind(monkeypatch, front)
    tool = UiType(LIVE)
    result = tool.run(tool.resolve(text="hello", target="notes", app="TextEdit"))
    assert not result.ok
    assert sent == [], f"keystrokes were sent to an app nobody confirmed: {sent}"
    assert "front" in result.summary.lower()


@pytest.mark.parametrize("front", [SOMEONE_ELSE, None], ids=["another-app", "unknown"])
def test_ui_key_sends_nothing_unless_the_named_app_is_confirmed_in_front(monkeypatch, sent, front):
    _stuck_behind(monkeypatch, front)
    tool = UiKey(LIVE)
    result = tool.run(tool.resolve(keys="cmd-s", app="TextEdit"))
    assert not result.ok
    assert sent == []


def test_ui_sequence_stops_before_the_first_keystroke_step(monkeypatch, sent):
    _stuck_behind(monkeypatch, SOMEONE_ELSE)
    tool = UiSequence(LIVE)
    result = tool.run(tool.resolve(
        app="TextEdit",
        steps=[{"action": "click", "target": "save"},
               {"action": "type", "text": "notes", "target": "notes"}],
    ))
    assert not result.ok
    assert result.data.get("steps_done") == 1, "the click may run; the typing may not"
    assert not any(kind == "type" for kind, _ in sent)


# --- the success path: brought forward, then sent ------------------------------


def test_ui_type_brings_the_named_app_forward_and_then_types(monkeypatch, sent):
    asked: list[int] = []
    front = {"pid": SOMEONE_ELSE}

    def _activate(pid):
        asked.append(pid)
        front["pid"] = pid
        return True

    monkeypatch.setattr(tools_mod, "activate", _activate)
    monkeypatch.setattr(tools_mod, "frontmost_pid", lambda: front["pid"])
    tool = UiType(LIVE)
    result = tool.run(tool.resolve(text="hello", target="notes", app="TextEdit"))
    assert result.ok, result
    assert asked == [TARGET_PID]
    assert sent == [("type", "hello")]


def test_an_app_already_in_front_is_not_re_activated(monkeypatch, sent):
    asked: list[int] = []
    monkeypatch.setattr(tools_mod, "activate", lambda pid: (asked.append(pid), True)[1])
    monkeypatch.setattr(tools_mod, "frontmost_pid", lambda: TARGET_PID)
    tool = UiKey(LIVE)
    assert tool.run(tool.resolve(keys="cmd-s", app="TextEdit")).ok
    assert asked == []


def test_the_activation_wait_is_bounded(monkeypatch, sent):
    """An app that never comes forward must not hang the caller. An earlier
    attempt at this fix waited on exactly that and stalled for minutes."""
    ticks = iter(range(10_000))
    monkeypatch.setattr(tools_mod, "_now", lambda: next(ticks) * 0.1)
    monkeypatch.setattr(tools_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(tools_mod, "ACTIVATE_WAIT_S", 0.5)
    _stuck_behind(monkeypatch, SOMEONE_ELSE)
    assert tools_mod._bring_to_front(TARGET_PID, "TextEdit") is not None
    assert next(ticks) < 20, "polled far past the deadline"
