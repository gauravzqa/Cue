"""The sentence the user answers must describe what will actually happen.

Everything here is about `ResolvedAction.describe()` and the two fields that
feed it. A resolver that computes a destructive fact and keeps it in `args`
produces a confirmation that is word-for-word identical to the safe case, which
is not a confirmation at all -- it is a yes collected under false pretences.

Nothing in this file runs a tool. resolve() is pure, which is exactly why the
readback can be tested without a machine.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from daa.config import Settings
from daa.contracts import ResolvedAction
from daa.tools import REGISTRY
from daa.tools.applescript import RunAppleScript
from daa.tools.clipboard import SetClipboard, describe_text

DRY = Settings(dry_run=True)

# One resolving and one non-resolving call for every registered tool. Both
# matter: the path that finds nothing still has to say what it was going to do.
RESOLVE_ARGS: dict[str, tuple[dict, dict]] = {
    "open_app": ({"name": "safari"}, {"name": "definitely-not-an-app-xyz"}),
    "list_running_apps": ({}, {}),
    "spotlight_search": ({"text": "invoice"}, {}),
    "reveal_in_finder": ({"paths": ["/nope/missing-xyz"]}, {"paths": []}),
    "move_to_trash": ({"paths": ["/nope/missing-xyz"]}, {"paths": []}),
    "move_files": ({"sources": ["/nope/missing-xyz"], "destination": "/tmp"}, {}),
    "get_clipboard": ({}, {}),
    "set_clipboard": ({"text": "hello"}, {"text": ""}),
    "list_shortcuts": ({}, {}),
    "run_shortcut": ({"name": "definitely-not-a-shortcut-xyz"}, {"name": ""}),
    "run_applescript": ({"script": 'tell application "Finder" to activate'}, {"script": ""}),
    "list_windows": ({}, {}),
    "focus_window": ({"app": "finder"}, {}),
}


def _tool(name: str):
    return type(REGISTRY.get(name))(DRY)


# ---------------------------------------------------------------------------
# every tool, every path, has a verb
# ---------------------------------------------------------------------------


def test_the_arg_table_covers_the_whole_registry():
    assert set(RESOLVE_ARGS) == set(REGISTRY.names())


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_every_tool_declares_a_spoken_verb(name):
    tool = _tool(name)
    assert tool.verb, f"{name} has no verb: its readback is a bare noun phrase"
    # Spoken, not a label: verb-first and lower case, though "the Trash" is a
    # place with a name.
    assert tool.verb == tool.verb.strip() and tool.verb[0].islower(), (
        f"{name}'s verb is not a spoken verb phrase"
    )


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_every_resolve_path_carries_the_verb(name):
    for kwargs in RESOLVE_ARGS[name]:
        action = _tool(name).resolve(**kwargs)
        assert isinstance(action, ResolvedAction)
        assert action.verb, f"{name}.resolve({kwargs}) produced a verb-less readback"
        assert action.describe().startswith(action.verb)


def test_the_same_target_under_two_tools_does_not_read_back_identically():
    """The headline bug: "report.pdf - should I go ahead?" meant either one."""
    path = "/nope/report.pdf"
    reveal = _tool("reveal_in_finder").resolve(paths=[path])
    trash = _tool("move_to_trash").resolve(paths=[path])
    assert reveal.describe() != trash.describe()


# ---------------------------------------------------------------------------
# targets are identifiers; content lives in data/args
# ---------------------------------------------------------------------------

SECRET = "hunter2-correct-horse-battery-staple"


def test_clipboard_content_never_reaches_the_targets_that_get_logged():
    """`targets` is written verbatim to ~/.daa/audit.jsonl."""
    action = SetClipboard(DRY).resolve(text=SECRET)
    assert SECRET not in " ".join(action.targets)
    assert SECRET not in action.describe()
    assert action.args["text"] == SECRET   # still there for the tool to use


def test_a_long_clipboard_write_is_described_by_its_size():
    action = SetClipboard(DRY).resolve(text="x" * 400)
    assert action.targets == ("400 characters of text",)
    assert describe_text("short one") == "the text you dictated"


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_no_tool_puts_a_content_argument_into_targets(name):
    """Whatever a target is, it is a name the user could say back to you."""
    marker = "zzcontentmarkerzz"
    content_args = {
        "set_clipboard": {"text": marker},
        "run_shortcut": {"name": "definitely-not-a-shortcut-xyz", "input_text": marker},
        "run_applescript": {"script": f'display dialog "{marker}"', "purpose": marker},
    }
    kwargs = content_args.get(name)
    if kwargs is None:
        pytest.skip(f"{name} takes no free-text content")
    action = _tool(name).resolve(**kwargs)
    assert marker not in " ".join(action.targets), f"{name} leaked content into targets"


# ---------------------------------------------------------------------------
# run_applescript: the model must not author its own confirmation
# ---------------------------------------------------------------------------

DESTRUCTIVE = 'tell application "Mail" to delete every message of mailbox "INBOX"'
BENIGN = 'tell application "Calendar" to get the start date of the next event'
COVER_STORY = "check what time my next meeting is"


def test_the_model_s_stated_purpose_is_not_the_readback():
    action = RunAppleScript(DRY).resolve(script=DESTRUCTIVE, purpose=COVER_STORY)
    spoken = action.describe()
    assert COVER_STORY not in spoken, "the script wrote its own confirmation"
    assert "Mail" in spoken
    assert "deletes items" in spoken


def test_two_different_scripts_under_one_purpose_do_not_read_back_the_same():
    gentle = RunAppleScript(DRY).resolve(script=BENIGN, purpose=COVER_STORY)
    nasty = RunAppleScript(DRY).resolve(script=DESTRUCTIVE, purpose=COVER_STORY)
    assert gentle.describe() != nasty.describe()


def test_the_real_script_is_in_the_args_the_visual_card_prints():
    action = RunAppleScript(DRY).resolve(script=DESTRUCTIVE, purpose=COVER_STORY)
    assert action.args["script"] == DESTRUCTIVE
    # Kept, but clearly labelled as a claim rather than a description.
    assert action.args["stated_purpose"] == COVER_STORY


# ---------------------------------------------------------------------------
# inverses: an allowlist for a world-readable journal
# ---------------------------------------------------------------------------


def test_inverses_only_ever_name_registered_tools():
    for tool_spec in REGISTRY.specs():
        for inverse in tool_spec.inverses:
            assert inverse in REGISTRY, f"{tool_spec.name} allows unknown tool {inverse}"


def test_irreversible_tools_declare_no_inverse_at_all():
    for tool in REGISTRY:
        if tool.irreversible:
            assert tool.spec.inverses == (), f"{tool.spec.name} cannot have an inverse"


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_every_undo_the_source_can_build_is_declared_as_an_inverse(name):
    """Static, so it covers tools no probe can exercise without a real machine."""
    tool = REGISTRY.get(name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(type(tool))))
    named = {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "UndoAction"
        for keyword in node.keywords
        if keyword.arg == "tool" and isinstance(keyword.value, ast.Constant)
    }
    assert named <= set(tool.spec.inverses), (
        f"{name} builds an undo calling {sorted(named - set(tool.spec.inverses))}, "
        "which its own spec does not allow as an inverse"
    )
