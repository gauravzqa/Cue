"""Shortcuts: the one Automation grant that buys the rest of Apple's surface.

Every test fakes the `/usr/bin/shortcuts` binary. Running a real shortcut is
the definition of mutating real user state, so the only thing exercised here is
the part that decides WHICH shortcut would run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier
from daa.tools.base import ShellResult
from daa.tools.shortcuts import SHORTCUTS_BIN, ListShortcuts, RunShortcut, list_installed

LIVE = Settings(dry_run=False)
DRY = Settings(dry_run=True)

CATALOG = [
    "Morning Routine",
    "Send Standup",
    "Send Everything To Everyone",
    "Log Weight",
    "Turn Off Lights",
]


class FakeShortcutsCLI:
    """Records argv, answers `list`, and pretends to run anything else."""

    def __init__(self, names=CATALOG, run_output: str = "", returncode: int = 0) -> None:
        self.names = list(names)
        self.run_output = run_output
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []
        self.ran: list[str] = []
        self.inputs: list[str] = []

    def __call__(self, argv, **kwargs):
        argv = tuple(str(a) for a in argv)
        assert isinstance(argv, tuple) and argv[0] == SHORTCUTS_BIN
        assert kwargs.get("timeout")  # a hung shortcut must not wedge the loop
        self.calls.append(argv)
        if argv[1] == "list":
            return ShellResult(argv=argv, stdout="\n".join(self.names) + "\n")
        if kwargs.get("mutating") and kwargs.get("dry_run"):
            return ShellResult(argv=argv, skipped=True)
        self.ran.append(argv[2])
        if "--input-path" in argv:
            self.inputs.append(Path(argv[argv.index("--input-path") + 1]).read_text())
        if "--output-path" in argv and self.run_output:
            Path(argv[argv.index("--output-path") + 1]).write_text(self.run_output)
        return ShellResult(
            argv=argv, returncode=self.returncode,
            stderr="" if self.returncode == 0 else "shortcut failed",
        )


@pytest.fixture
def cli(monkeypatch):
    fake = FakeShortcutsCLI()
    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    return fake


# ---------------------------------------------------------------------------
# list_shortcuts
# ---------------------------------------------------------------------------


def test_list_is_silent_and_read_only():
    assert ListShortcuts.spec.floor is RiskTier.SILENT
    assert ListShortcuts.mutates is False


def test_list_names_a_few_out_loud(cli):
    tool = ListShortcuts(DRY)
    result = tool.run(tool.resolve())
    assert result.ok
    assert result.data["shortcuts"] == CATALOG
    assert "Morning Routine" in result.summary
    assert "2 more" in result.summary


def test_list_scopes_to_a_folder(cli):
    tool = ListShortcuts(DRY)
    tool.run(tool.resolve(folder="Home"))
    assert cli.calls[0] == (SHORTCUTS_BIN, "list", "--folder", "Home")


def test_list_with_no_shortcuts_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        "daa.tools.base.run_argv", lambda argv, **kw: ShellResult(argv=tuple(argv), stdout="")
    )
    tool = ListShortcuts(DRY)
    result = tool.run(tool.resolve())
    assert result.ok and result.data["count"] == 0


def test_list_installed_reports_a_missing_binary(monkeypatch):
    monkeypatch.setattr(
        "daa.tools.base.run_argv",
        lambda argv, **kw: ShellResult(argv=tuple(argv), returncode=-1, not_found=True),
    )
    names, error = list_installed()
    assert names == [] and "not installed" in error


# ---------------------------------------------------------------------------
# run_shortcut -- resolution
# ---------------------------------------------------------------------------


def test_run_floor_is_confirm_voice_and_it_says_it_cannot_be_undone():
    assert RunShortcut.spec.floor is RiskTier.CONFIRM_VOICE
    assert RunShortcut.irreversible is True
    assert "irreversible" in RunShortcut.spec.tags


def test_resolve_matches_the_real_installed_name(cli):
    action = RunShortcut(DRY).resolve(name="morning routine")
    assert action.targets == ("Morning Routine",)
    assert action.args["confident"] is True


def test_resolve_does_not_run_anything(cli):
    RunShortcut(LIVE).resolve(name="morning routine")
    assert cli.ran == []
    assert all(call[1] == "list" for call in cli.calls)


def test_a_partial_phrase_does_not_silently_pick_the_scary_one(cli):
    """'send it' must not become 'Send Everything To Everyone'."""
    action = RunShortcut(DRY).resolve(name="send standup")
    assert action.targets == ("Send Standup",)
    assert "Send Everything To Everyone" in action.args["alternates"]


def test_resolve_on_an_unknown_name_has_no_target(cli):
    action = RunShortcut(DRY).resolve(name="launch the rocket")
    assert action.targets == ()
    assert action.args["resolved_name"] is None


def test_run_refuses_an_unresolved_name(cli):
    tool = RunShortcut(LIVE)
    result = tool.run(tool.resolve(name="launch the rocket"))
    assert result.ok is False and cli.ran == []


# ---------------------------------------------------------------------------
# run_shortcut -- execution
# ---------------------------------------------------------------------------


def test_run_executes_the_resolved_name_not_the_spoken_one(cli):
    tool = RunShortcut(LIVE)
    result = tool.run(tool.resolve(name="morning routine"))
    assert result.ok and cli.ran == ["Morning Routine"]
    assert result.summary == "Morning Routine finished."


def test_input_text_is_passed_as_a_file_never_as_an_argument(cli):
    tool = RunShortcut(LIVE)
    tool.run(tool.resolve(name="log weight", input_text="82.4 kg; rm -rf /"))
    assert cli.inputs == ["82.4 kg; rm -rf /"]
    assert "82.4 kg; rm -rf /" not in cli.calls[-1]


def test_output_is_read_back_and_kept_out_of_the_spoken_line(monkeypatch):
    fake = FakeShortcutsCLI(run_output="/Users/someone/Reports/weekly.pdf")
    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = RunShortcut(LIVE)
    result = tool.run(tool.resolve(name="log weight"))
    assert result.data["output"] == "/Users/someone/Reports/weekly.pdf"
    assert "/" not in result.summary  # a path read aloud is unbearable


def test_short_output_is_spoken(monkeypatch):
    fake = FakeShortcutsCLI(run_output="82.4 kilograms logged")
    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = RunShortcut(LIVE)
    result = tool.run(tool.resolve(name="log weight"))
    assert "82.4 kilograms logged" in result.summary


def test_dry_run_does_not_execute(cli):
    tool = RunShortcut(DRY)
    result = tool.run(tool.resolve(name="turn off lights"))
    assert result.ok and result.data["dry_run"] is True
    assert cli.ran == []


def test_a_failing_shortcut_is_reported_not_raised(monkeypatch):
    fake = FakeShortcutsCLI(returncode=1)
    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = RunShortcut(LIVE)
    result = tool.run(tool.resolve(name="turn off lights"))
    assert result.ok is False and result.undo is None
    assert "Turn Off Lights" in result.summary


@pytest.mark.macos
def test_real_shortcuts_binary_lists():
    """Opt-in: proves /usr/bin/shortcuts answers for this host binary."""
    names, error = list_installed()
    assert error == ""
    assert isinstance(names, list)


# ---------------------------------------------------------------------------
# the confirmation has to contain the message
# ---------------------------------------------------------------------------


def test_the_body_of_the_message_is_read_back_not_just_the_shortcut_name(cli):
    """"Text My Partner - should I go ahead?" is not consent to send THIS."""
    action = RunShortcut(DRY).resolve(
        name="Send Standup", input_text="I'm leaving him. Coming over tonight."
    )
    spoken = action.describe()
    assert "Send Standup" in spoken
    assert "I'm leaving him. Coming over tonight." in spoken


def test_a_very_long_input_is_shortened_but_still_spoken(cli):
    action = RunShortcut(DRY).resolve(name="Send Standup", input_text="word " * 400)
    body = action.consequences["input"]
    assert body.endswith('..."') and len(body) < 300


def test_a_shortcut_with_no_input_says_nothing_extra(cli):
    assert RunShortcut(DRY).resolve(name="Send Standup").consequences == {}
    assert RunShortcut(DRY).resolve(name="Send Standup", input_text="  ").consequences == {}


def test_an_unresolved_shortcut_still_reads_back_its_input(cli):
    """The name did not match, but a yes would still have sent this text."""
    action = RunShortcut(DRY).resolve(name="launch the rocket", input_text="do it")
    assert action.targets == ()
    assert "do it" in action.describe()


def test_an_inexact_shortcut_match_is_not_an_explicitly_named_target(cli):
    assert RunShortcut(DRY).resolve(name="Send Standup").explicit is True
    assert RunShortcut(DRY).resolve(name="standup").explicit is False


def test_running_a_shortcut_reads_back_verb_first(cli):
    assert RunShortcut(DRY).resolve(name="Turn Off Lights").describe().startswith(
        "run the shortcut Turn Off Lights"
    )
