"""The build-breaking invariant: nothing mutates at CONFIRM_VOICE or above
without either returning an UndoAction or explicitly paying for irreversibility.

This test iterates the live REGISTRY rather than a hand-written list, so adding
a new high-risk tool and forgetting its undo fails here instead of failing in
front of a user who just said "no, put it back".

The escape hatch is deliberately expensive. A tool may skip undo only by:
  1. declaring `irreversible = True` in code,
  2. carrying the "irreversible" tag in its ToolSpec, and
  3. saying so in the activation_hint the router reads,
  4. and sitting at CONFIRM_VOICE or above.
Anything short of that is a missing undo, not a design decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier, UndoAction
from daa.tools import REGISTRY
from daa.tools import files as files_mod

LIVE = Settings(dry_run=False)
GATED = [spec for spec in REGISTRY.specs() if spec.floor >= RiskTier.CONFIRM_VOICE]


def _fresh(name: str):
    """A tool bound to dry_run=False -- undo only exists for a real mutation."""
    return type(REGISTRY.get(name))(LIVE)


# ---------------------------------------------------------------------------
# Probes: one per mutating, reversible tool. Each performs a REAL mutation
# inside tmp_path and hands back the ToolResult.
# ---------------------------------------------------------------------------


def _probe_move_files(tmp_path: Path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("keep me", encoding="utf-8")
    tool = _fresh("move_files")
    return tool, tool.run(tool.resolve(sources=[str(source)], destination=str(tmp_path / "away")))


def _probe_move_to_trash(tmp_path: Path, monkeypatch):
    trash = tmp_path / "Trash"
    trash.mkdir()

    def fake_trash(path: Path):
        target = trash / path.name
        path.rename(target)
        return True, str(target), ""

    # Only the NSFileManager call is faked; the undo under test is the real one.
    monkeypatch.setattr(files_mod, "_trash_via_appkit", fake_trash)
    victim = tmp_path / "junk.txt"
    victim.write_text("junk", encoding="utf-8")
    tool = _fresh("move_to_trash")
    return tool, tool.run(tool.resolve(paths=[str(victim)]))


def _probe_set_clipboard(tmp_path: Path, monkeypatch):
    class FakeBoard:
        def __init__(self) -> None:
            self.value = "whatever was there before"

        def clearContents(self):
            self.value = ""

        def setString_forType_(self, text, _type):
            self.value = text
            return True

    board = FakeBoard()
    monkeypatch.setattr("daa.tools.clipboard._pasteboard", lambda: board)
    monkeypatch.setattr(
        "daa.tools.clipboard.read_clipboard_text", lambda: (board.value, "")
    )
    tool = _fresh("set_clipboard")
    return tool, tool.run(tool.resolve(text="something new"))


# name -> probe. A mutating, reversible tool with no entry here fails the
# coverage test below, which is the point.
PROBES = {
    "move_files": _probe_move_files,
    "move_to_trash": _probe_move_to_trash,
    "set_clipboard": _probe_set_clipboard,
}

# Tools that genuinely have no inverse. Kept as data so the list cannot grow
# without a reviewer seeing it in the diff.
IRREVERSIBLE = {"run_shortcut", "run_applescript"}


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_there_are_gated_tools_to_check():
    assert GATED, "the registry lost its high-risk tools; this test would pass vacuously"


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
def test_gated_tools_are_covered_by_a_probe_or_declared_irreversible(spec):
    assert spec.name in PROBES or spec.name in IRREVERSIBLE, (
        f"{spec.name} runs at {spec.floor.name} but has neither an undo probe nor an "
        "explicit irreversible declaration"
    )


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
def test_gated_tool_declares_whether_it_mutates(spec):
    tool = REGISTRY.get(spec.name)
    assert tool.mutates is True, f"{spec.name} is gated but claims not to mutate"


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_mutating_tools_return_an_undo(name, tmp_path, monkeypatch):
    tool, result = PROBES[name](tmp_path, monkeypatch)
    assert result.ok, f"{name} probe did not mutate: {result.error}"
    assert isinstance(result.undo, UndoAction), f"{name} mutated without an undo"
    assert result.undo.description, f"{name} undo cannot be spoken"
    assert result.undo.tool in REGISTRY, f"{name} undo names an unregistered tool"
    assert not tool.irreversible, f"{name} has an undo but claims to be irreversible"


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_the_undo_names_a_tool_its_own_spec_allows_as_an_inverse(name, tmp_path, monkeypatch):
    """The journal is a world-readable file, so the row is not trusted.

    Anything running as the user can append an entry naming any tool it likes.
    What makes a row safe to replay is that the tool which PRODUCED it said, in
    reviewed source, that this tool may act as its inverse -- so an undo naming
    a tool outside that allowlist is a bug here, not a surprise at replay time.
    """
    tool, result = PROBES[name](tmp_path, monkeypatch)
    inverses = tool.spec.inverses
    assert inverses, f"{name} returns an undo but declares no inverse"
    assert result.undo.tool in inverses, (
        f"{name} hands back an undo calling {result.undo.tool!r}, which its own "
        f"spec does not allow; declared inverses are {inverses}"
    )


def test_every_tool_states_its_inverses_one_way_or_the_other():
    """`()` is a statement ("no inverse exists"), not an omission."""
    for tool in REGISTRY:
        assert isinstance(tool.spec.inverses, tuple)
        for inverse in tool.spec.inverses:
            assert inverse in REGISTRY, f"{tool.spec.name} allows unknown tool {inverse}"
        if tool.spec.name in PROBES:
            assert tool.spec.inverses, f"{tool.spec.name} has an undo but no allowlist"
        if tool.spec.name in IRREVERSIBLE:
            assert tool.spec.inverses == (), f"{tool.spec.name} cannot have an inverse"


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_the_undo_is_actually_runnable(name, tmp_path, monkeypatch):
    """An UndoAction that cannot be replayed is a comforting lie."""
    _, result = PROBES[name](tmp_path, monkeypatch)
    undo = result.undo
    assert undo is not None
    undo_tool = _fresh(undo.tool)
    replay = undo_tool.run(undo_tool.resolve(**dict(undo.args)))
    assert replay.ok, f"replaying {name}'s undo failed: {replay.error}"


def test_move_files_undo_restores_the_original_state(tmp_path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("keep me", encoding="utf-8")
    _, result = _probe_move_files(tmp_path, monkeypatch)
    assert not source.exists()
    undo_tool = _fresh(result.undo.tool)
    undo_tool.run(undo_tool.resolve(**dict(result.undo.args)))
    assert source.read_text(encoding="utf-8") == "keep me"


# ---------------------------------------------------------------------------
# The escape hatch, and what it costs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(IRREVERSIBLE), ids=str)
def test_irreversible_tools_pay_for_it(name):
    tool = REGISTRY.get(name)
    spec = tool.spec
    assert tool.irreversible is True, f"{name} is listed irreversible but does not declare it"
    assert "irreversible" in spec.tags, f"{name} does not carry the irreversible tag"
    assert spec.floor >= RiskTier.CONFIRM_VOICE, f"{name} skips undo without a confirmation gate"
    hint = spec.activation_hint.lower()
    assert "undo" in hint or "irreversible" in hint, (
        f"{name} does not warn the router that it cannot be taken back"
    )


def test_no_tool_claims_irreversibility_without_the_gate():
    for tool in REGISTRY:
        if tool.irreversible:
            assert tool.spec.name in IRREVERSIBLE
            assert tool.mutates is True
            assert tool.spec.floor >= RiskTier.CONFIRM_VOICE


def test_generated_code_can_never_be_authorised_by_voice_alone():
    assert REGISTRY.get("run_applescript").spec.floor is RiskTier.CONFIRM_VISUAL


def test_read_only_tools_never_claim_to_mutate():
    for tool in REGISTRY:
        if tool.spec.floor is RiskTier.SILENT:
            assert tool.mutates is False, f"{tool.spec.name} is SILENT but mutates"


def test_dry_run_never_hands_back_an_undo(tmp_path):
    """Nothing changed, so there is nothing to offer to reverse."""
    dry = Settings(dry_run=True)
    victim = tmp_path / "junk.txt"
    victim.write_text("junk", encoding="utf-8")
    for name, kwargs in (
        ("move_to_trash", {"paths": [str(victim)]}),
        ("move_files", {"sources": [str(victim)], "destination": str(tmp_path / "away")}),
        ("set_clipboard", {"text": "hello"}),
    ):
        tool = type(REGISTRY.get(name))(dry)
        result = tool.run(tool.resolve(**kwargs))
        assert result.ok and result.undo is None
    assert victim.exists()


def test_failed_mutations_do_not_invent_an_undo(tmp_path):
    tool = _fresh("move_files")
    result = tool.run(tool.resolve(sources=[str(tmp_path / "ghost")], destination=str(tmp_path)))
    assert result.ok is False and result.undo is None
