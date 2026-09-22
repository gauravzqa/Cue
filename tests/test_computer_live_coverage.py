"""`evals/ax_coverage.py`: read-only, label-free, and grant-less-safe.

Runs in the DEFAULT suite, on any platform: nothing here touches macOS. The
harness is exercised against a fake `ApplicationServices` module, the way
`computer_tree.py` stands in for a real tree. (Its walk against a REAL tree is
in `test_computer_live.py`, which is `-m macos`.)

The three properties, and how each is pinned:

* **Read-only** -- by the source (no write API is ever named in the file) and
  by behaviour (the `ReadOnlyAX` facade raises before a write reaches macOS).
* **No labels, no window titles** -- a fake tree full of private-looking
  strings is walked, rendered as a table and as JSON, and none of those strings
  come out.
* **No grant, no traceback** -- every app answering -25211 produces a row per
  app and one instruction line, and exit status 0.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import ax_coverage as cov

from daa.tools.computer import ax

SOURCE = Path(inspect.getfile(cov)).read_text(encoding="utf-8")

PRIVATE = (
    "Re: layoffs — Mail",           # a window title
    "Send to Board",                # a button label
    "Q3 termination list.xlsx",     # a recent item
    "Log Out Jane Doe…",            # the user's name, via the Apple menu
    "jane@example.com",             # a field placeholder someone personalised
)


# ---------------------------------------------------------------------------
# Read-only, by the source
# ---------------------------------------------------------------------------

WRITE_APIS = (
    "AXUIElementPerformAction",
    "AXUIElementSetAttributeValue",
    "AXUIElementPostKeyboardEvent",
    "CGEventPost",
    "CGEventCreateMouseEvent",
    "CGEventCreateKeyboardEvent",
    "activateWithOptions",
    "activateIgnoringOtherApps",
    "set_manual_accessibility",
    "enable_manual_accessibility",
    "AXManualAccessibility",
    "AXEnhancedUserInterface",
    "AXRaise",
    "kAXRaiseAction",
    "kAXPressAction",
    "kAXValueAttribute",
    "kAXFocusedAttribute",
)
# daa's own input primitives. The harness may import the reader (`_element`)
# and constants from ax, never these.
AX_ACTORS = ("press", "click_center", "focus", "type_text", "post_key", "snapshot")


def test_the_harness_never_names_a_write_api():
    for name in WRITE_APIS:
        assert name not in SOURCE, name


def test_the_harness_never_calls_an_ax_input_primitive():
    tree = ast.parse(SOURCE)
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ax"
    }
    assert not used & set(AX_ACTORS), used & set(AX_ACTORS)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "daa.tools.computer.ax"
        for alias in node.names
    }
    assert not imported & set(AX_ACTORS)


def test_the_only_ax_functions_it_allows_are_reads():
    assert cov.READ_FUNCTIONS == {
        "AXIsProcessTrusted",
        "AXUIElementCreateApplication",
        "AXUIElementSetMessagingTimeout",
        "AXUIElementCopyAttributeValue",
        "AXUIElementCopyActionNames",
        "AXValueGetValue",
    }


# ---------------------------------------------------------------------------
# Read-only, by behaviour
# ---------------------------------------------------------------------------


class _Module(SimpleNamespace):
    """Records any call that gets through, so a leak is visible, not silent."""


def _module_that_would_write():
    calls: list[str] = []
    module = _Module(
        AXUIElementPerformAction=lambda *a: calls.append("perform") or 0,
        AXUIElementSetAttributeValue=lambda *a: calls.append("set") or 0,
        AXUIElementCopyAttributeValue=lambda *a: calls.append("copy") or (0, None),
        kAXValueAttribute="AXValue",
        kAXTitleAttribute="AXTitle",
    )
    return module, calls


@pytest.mark.parametrize(
    "name",
    ["AXUIElementPerformAction", "AXUIElementSetAttributeValue", "kAXValueAttribute",
     "AXUIElementCreateSystemWide", "CGEventPost"],
)
def test_the_facade_refuses_everything_that_is_not_a_read(name):
    module, calls = _module_that_would_write()
    facade = cov.ReadOnlyAX(module)
    with pytest.raises(cov.ReadOnlyViolation):
        getattr(facade, name)
    assert calls == []


@pytest.mark.parametrize("attribute", sorted(cov.FORBIDDEN_ATTRIBUTES))
def test_the_facade_refuses_to_copy_user_content(attribute):
    module, calls = _module_that_would_write()
    facade = cov.ReadOnlyAX(module)
    with pytest.raises(cov.ReadOnlyViolation):
        facade.AXUIElementCopyAttributeValue(object(), attribute, None)
    assert calls == []
    # A label attribute is fine.
    facade.AXUIElementCopyAttributeValue(object(), "AXTitle", None)
    assert calls == ["copy"]


# ---------------------------------------------------------------------------
# A fake ApplicationServices with a tree full of private strings
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, **attrs):
        self.children = attrs.pop("children", [])
        self.actions = attrs.pop("actions", [])
        self.attrs = attrs


def _fake_api(root_by_pid: dict[int, object], *, error: int = 0):
    """An ApplicationServices stand-in serving `_Node` trees per pid."""

    def create(pid):
        return ("app", pid)

    def copy(ref, attribute, _out):
        if isinstance(ref, tuple):
            if error:
                return error, None
            app = root_by_pid[ref[1]]
            if attribute == "AXWindows":
                return 0, app["windows"]
            if attribute == "AXMenuBar":
                return (0, app["menu"]) if app.get("menu") is not None else (-25212, None)
            return -25205, None
        if attribute == "AXChildren":
            return (0, ref.children) if ref.children else (-25212, None)
        if attribute in ref.attrs:
            return 0, ref.attrs[attribute]
        return -25205, None

    names = [
        "Role", "Subrole", "RoleDescription", "Title", "Description", "Help",
        "PlaceholderValue", "Identifier", "Enabled", "Focused", "Position", "Size",
        "Children", "Windows", "MenuBar",
    ]
    module = _Module(
        AXIsProcessTrusted=lambda: not error,
        AXUIElementCreateApplication=create,
        AXUIElementSetMessagingTimeout=lambda ref, s: 0,
        AXUIElementCopyAttributeValue=copy,
        AXUIElementCopyActionNames=lambda ref, _o: (0, list(ref.actions)),
        AXValueGetValue=lambda *a: (False, None),
        kAXValueCGPointType=1,
        kAXValueCGSizeType=2,
        **{f"kAX{n}Attribute": f"AX{n}" for n in names},
    )
    return cov.ReadOnlyAX(module)


def _private_app():
    window = _Node(
        AXRole="AXWindow", AXTitle=PRIVATE[0],
        children=[
            _Node(AXRole="AXButton", AXRoleDescription="button", AXTitle=PRIVATE[1],
                  actions=["AXPress"]),
            _Node(AXRole="AXTextField", AXRoleDescription="text field",
                  AXPlaceholderValue=PRIVATE[4]),
            _Node(AXRole="AXButton", AXSubrole="AXCloseButton", actions=["AXPress"]),
            _Node(AXRole="AXGroup", children=[
                _Node(AXRole="AXCheckBox", AXTitle="Remember me", actions=["AXPress"]),
            ]),
            _Node(AXRole="AXStaticText"),
        ],
    )
    apple = _Node(AXRole="AXMenuBarItem", AXTitle="Apple", actions=["AXPress"], children=[
        _Node(AXRole="AXMenu", children=[
            _Node(AXRole="AXMenuItem", AXTitle=PRIVATE[2], actions=["AXPress"]),
            _Node(AXRole="AXMenuItem", AXTitle=PRIVATE[3], actions=["AXPress"]),
        ]),
    ])
    own = _Node(AXRole="AXMenuBarItem", AXTitle="File", actions=["AXPress"], children=[
        _Node(AXRole="AXMenu", children=[
            _Node(AXRole="AXMenuItem", AXTitle="New Message", actions=["AXPress"]),
        ]),
    ])
    menu = _Node(AXRole="AXMenuBar", children=[apple, own])
    return {"windows": [window], "menu": menu}


APP = ax.AppInfo("Mail", "com.apple.mail", 4242)


def test_the_walk_counts_actionable_and_nameable_elements():
    row = cov.walk_app(APP, _fake_api({4242: _private_app()}))
    assert row.reachable and row.ax_error == 0
    assert row.windows == 1
    # window, button, field, close button, group, checkbox, static text
    assert row.nodes == 7
    # Button, field, close button, checkbox (static text has no action).
    assert row.actionable == 4
    # The close button has no label, so it is not nameable.
    assert row.nameable == 3
    assert row.unnameable_roles == {"AXButton": 1}
    assert row.nameable_roles == {"AXButton": 1, "AXTextField": 1, "AXCheckBox": 1}
    # "Apple" and "File" bar items, and "New Message"; the Apple menu's items
    # counted apart.
    assert row.apple_menu_items == 3
    assert row.menu_nameable == 2


def test_no_label_or_window_title_reaches_the_report():
    rows = cov.survey([APP], _fake_api({4242: _private_app()}), chromium_of=lambda a: False)
    summary = cov.summarise(rows)
    table = cov.render(rows, summary, trusted=True)
    blob = json.dumps({"summary": summary, "apps": [cov.asdict(r) for r in rows]},
                      ensure_ascii=False)
    for text in (*PRIVATE, "Remember me", "New Message", "Mail"):
        assert text not in table, text
        assert text not in blob.replace("com.apple.mail", ""), text
    assert "com.apple.mail" in table


def test_main_prints_json_without_labels(capsys):
    code = cov.main(
        ["--json"], apps=[APP], api=_fake_api({4242: _private_app()}),
        trusted=True, chromium_of=lambda a: False,
    )
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["summary"]["nameable_elements"] == 3
    for text in PRIVATE:
        assert text not in out


def test_the_depth_and_node_caps_apply():
    deep = _Node(AXRole="AXButton", AXTitle="Deep", actions=["AXPress"])
    for _ in range(20):
        deep = _Node(AXRole="AXGroup", children=[deep])
    tree = {"windows": [_Node(AXRole="AXWindow", children=[deep])], "menu": None}
    row = cov.walk_app(APP, _fake_api({4242: tree}))
    assert row.truncated and row.nameable == 0
    row = cov.walk_app(APP, _fake_api({4242: tree}), max_depth=40)
    assert not row.truncated and row.nameable == 1
    row = cov.walk_app(APP, _fake_api({4242: tree}), max_depth=40, max_nodes=5)
    assert row.truncated and row.nodes == 5


# ---------------------------------------------------------------------------
# No grant
# ---------------------------------------------------------------------------


def test_without_a_grant_every_app_reports_minus_25211_and_one_instruction(capsys):
    apps = [APP, ax.AppInfo("Notes", "com.apple.Notes", 4343), ax.AppInfo("x", "", 99)]
    code = cov.main(
        [], apps=apps, api=_fake_api({}, error=ax.AX_API_DISABLED),
        trusted=False, chromium_of=lambda a: False,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Traceback" not in out
    rows = [line for line in out.splitlines() if "-25211" in line]
    assert len(rows) == 3
    assert "pid:99" in out
    grant_lines = [line for line in out.splitlines() if line.startswith("Accessibility is not granted")]
    assert len(grant_lines) == 1
    assert "0 reachable" in out


def test_the_harness_skips_its_own_process():
    import os

    me = ax.AppInfo("python", "", os.getpid())
    rows = cov.survey([me], _fake_api({}), chromium_of=lambda a: False)
    assert rows == []


def test_chromium_detection_is_by_bundle_content(tmp_path):
    electron = tmp_path / "Thing.app" / "Contents" / "Frameworks"
    (electron / "Electron Framework.framework").mkdir(parents=True)
    renamed = tmp_path / "Codex.app" / "Contents" / "Frameworks" / "Codex Framework.framework"
    (renamed / "Resources").mkdir(parents=True)
    (renamed / "Resources" / "icudtl.dat").write_bytes(b"")
    native = tmp_path / "Native.app" / "Contents" / "Frameworks" / "Sparkle.framework"
    native.mkdir(parents=True)
    assert cov.is_chromium_family(str(tmp_path / "Thing.app"))
    assert cov.is_chromium_family(str(tmp_path / "Codex.app"))
    assert not cov.is_chromium_family(str(tmp_path / "Native.app"))
    assert not cov.is_chromium_family("")
