"""File tools. Everything happens inside tmp_path; nothing touches real state.

The trash path is faked at exactly one seam -- `_trash_via_appkit` -- because
the real one is the only part that needs a machine. Everything above it, and in
particular the undo it produces, is the same code that runs for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier, UndoAction
from daa.tools import files as files_mod
from daa.tools.base import ShellResult
from daa.tools.files import (
    MoveFiles,
    MoveToTrash,
    RevealInFinder,
    SpotlightSearch,
    build_mdfind_query,
    expand_inputs,
)

LIVE = Settings(dry_run=False)
DRY = Settings(dry_run=True)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    for name in ("one.txt", "two.txt", "photo.png"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    return tmp_path


class FakeTrash:
    """Stands in for NSFileManager.trashItemAtURL, including its return value."""

    def __init__(self, trash_dir: Path) -> None:
        self.trash_dir = trash_dir
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []

    def __call__(self, path: Path) -> tuple[bool, str | None, str]:
        self.calls.append(str(path))
        target = self.trash_dir / path.name
        # The real Trash renames on collision; undo must survive that.
        if target.exists():
            target = self.trash_dir / f"{path.stem} 2{path.suffix}"
        path.rename(target)
        return True, str(target), ""


# ---------------------------------------------------------------------------
# path expansion
# ---------------------------------------------------------------------------


def test_expand_inputs_resolves_globs_and_reports_misses(sandbox):
    found, missing = expand_inputs([str(sandbox / "*.txt"), str(sandbox / "nope.txt")])
    assert sorted(p.name for p in found) == ["one.txt", "two.txt"]
    assert missing == [str(sandbox / "nope.txt")]


def test_expand_inputs_deduplicates(sandbox):
    found, _ = expand_inputs([str(sandbox / "one.txt"), str(sandbox / "one.txt")])
    assert len(found) == 1


def test_expand_inputs_reports_a_glob_that_matched_nothing(sandbox):
    found, missing = expand_inputs([str(sandbox / "*.heic")])
    assert found == [] and missing


# ---------------------------------------------------------------------------
# spotlight_search
# ---------------------------------------------------------------------------


def test_query_is_date_scoped_by_phrase():
    query = build_mdfind_query("invoice", when="yesterday")
    assert "$time.yesterday" in query and "$time.today" in query
    assert "invoice" in query


def test_query_is_date_scoped_by_day_count():
    assert "$time.today(-7)" in build_mdfind_query(None, within_days=7)
    assert "$time.today(-3)" in build_mdfind_query(None, when="last 3 days")


def test_query_uses_the_screenshot_metadata_flag():
    assert "kMDItemIsScreenCapture == 1" in build_mdfind_query(None, kind="screenshot")


def test_query_escapes_quotes_so_a_spoken_name_cannot_break_out():
    query = build_mdfind_query('say "hi"')
    assert '\\"hi\\"' in query


def test_empty_query_does_not_ask_for_the_whole_index():
    assert build_mdfind_query(None) == 'kMDItemFSName == "*"cd'


def test_resolve_builds_a_scoped_argv(sandbox):
    action = SpotlightSearch(DRY).resolve(
        text="report", kind="pdf", within_days=2, scope=[str(sandbox)]
    )
    argv = action.args["argv"]
    assert argv[0] == "/usr/bin/mdfind"
    assert "-onlyin" in argv and str(sandbox) in argv
    assert action.targets and "last 2 days" in action.targets[0]


def test_run_reports_newest_first(sandbox, monkeypatch):
    newest = sandbox / "two.txt"
    newest.touch()

    def fake(argv, **kwargs):
        assert kwargs["mutating"] is False  # search must stay read-only
        return ShellResult(argv=tuple(argv), stdout=f"{sandbox / 'one.txt'}\n{newest}\n")

    monkeypatch.setattr(files_mod, "run_argv", fake, raising=False)
    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = SpotlightSearch(DRY)
    result = tool.run(tool.resolve(text="txt", scope=[str(sandbox)]))
    assert result.ok
    assert result.data["results"][0]["name"] == "two.txt"
    assert result.data["count"] == 2


def test_search_runs_even_in_dry_run(monkeypatch):
    seen: list[bool] = []

    def fake(argv, **kwargs):
        seen.append(kwargs["mutating"])
        return ShellResult(argv=tuple(argv), stdout="")

    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = SpotlightSearch(DRY)
    tool.run(tool.resolve(text="anything"))
    assert seen == [False]


# ---------------------------------------------------------------------------
# reveal_in_finder
# ---------------------------------------------------------------------------


def test_reveal_resolves_to_real_paths_only(sandbox, monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake(argv, **kwargs):
        calls.append(tuple(argv))
        return ShellResult(argv=tuple(argv))

    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    tool = RevealInFinder(DRY)
    action = tool.resolve(paths=[str(sandbox / "one.txt"), str(sandbox / "ghost.txt")])
    assert action.targets == ("one.txt",)
    assert action.args["missing"]
    assert tool.run(action).ok
    assert calls[0][:2] == ("/usr/bin/open", "-R")


def test_reveal_with_nothing_found_fails_softly():
    tool = RevealInFinder(DRY)
    result = tool.run(tool.resolve(paths=["/definitely/not/here"]))
    assert result.ok is False and result.error


# ---------------------------------------------------------------------------
# move_to_trash
# ---------------------------------------------------------------------------


def test_trash_floor_is_confirm_voice():
    assert MoveToTrash.spec.floor is RiskTier.CONFIRM_VOICE


def test_trash_resolve_names_files_and_refuses_protected_roots(sandbox):
    action = MoveToTrash(DRY).resolve(paths=[str(sandbox / "one.txt"), str(Path.home())])
    assert action.targets == ("one.txt",)
    assert action.args["protected"] == [str(Path.home())]


def test_trash_run_refuses_when_a_protected_path_was_named():
    tool = MoveToTrash(LIVE)
    result = tool.run(tool.resolve(paths=[str(Path.home() / "Documents")]))
    assert result.ok is False and result.undo is None


def test_trash_moves_and_returns_an_undo_that_restores(sandbox, monkeypatch):
    fake = FakeTrash(sandbox / "trash")
    monkeypatch.setattr(files_mod, "_trash_via_appkit", fake)
    tool = MoveToTrash(LIVE)
    target = sandbox / "one.txt"

    result = tool.run(tool.resolve(paths=[str(target)]))
    assert result.ok and not target.exists()
    assert isinstance(result.undo, UndoAction)
    assert result.undo.tool == "move_files"

    MoveFiles(LIVE).run(MoveFiles(LIVE).resolve(**result.undo.args))
    assert target.exists() and target.read_text(encoding="utf-8") == "one.txt"


def test_trash_undo_survives_a_rename_in_the_trash(sandbox, monkeypatch):
    """The Trash renames on collision, so undo must use the RESULTING url."""
    fake = FakeTrash(sandbox / "trash")
    (sandbox / "trash" / "one.txt").write_text("an older one.txt", encoding="utf-8")
    monkeypatch.setattr(files_mod, "_trash_via_appkit", fake)

    tool = MoveToTrash(LIVE)
    result = tool.run(tool.resolve(paths=[str(sandbox / "one.txt")]))
    pairs = result.undo.args["pairs"]
    assert Path(pairs[0][0]).name == "one 2.txt"
    MoveFiles(LIVE).run(MoveFiles(LIVE).resolve(**result.undo.args))
    assert (sandbox / "one.txt").read_text(encoding="utf-8") == "one.txt"


def test_trash_dry_run_changes_nothing(sandbox, monkeypatch):
    fake = FakeTrash(sandbox / "trash")
    monkeypatch.setattr(files_mod, "_trash_via_appkit", fake)
    tool = MoveToTrash(DRY)
    result = tool.run(tool.resolve(paths=[str(sandbox / "one.txt")]))
    assert result.ok and result.data["dry_run"] is True
    assert fake.calls == [] and (sandbox / "one.txt").exists()


def test_trash_reports_partial_failure_but_still_offers_undo(sandbox, monkeypatch):
    fake = FakeTrash(sandbox / "trash")

    def flaky(path):
        if path.name == "two.txt":
            return False, None, "permission denied"
        return fake(path)

    monkeypatch.setattr(files_mod, "_trash_via_appkit", flaky)
    tool = MoveToTrash(LIVE)
    result = tool.run(tool.resolve(paths=[str(sandbox / "one.txt"), str(sandbox / "two.txt")]))
    assert result.ok is False
    assert result.undo is not None and len(result.undo.args["pairs"]) == 1


# ---------------------------------------------------------------------------
# move_files
# ---------------------------------------------------------------------------


def test_move_into_a_folder_and_undo_puts_it_back(sandbox):
    dest = sandbox / "sorted"
    tool = MoveFiles(LIVE)
    action = tool.resolve(sources=[str(sandbox / "*.txt")], destination=str(dest))
    assert len(action.args["pairs"]) == 2

    result = tool.run(action)
    assert result.ok and (dest / "one.txt").exists() and not (sandbox / "one.txt").exists()

    undo = result.undo
    assert undo is not None and undo.tool == "move_files"
    tool.run(tool.resolve(**undo.args))
    assert (sandbox / "one.txt").exists() and not (dest / "one.txt").exists()


def test_single_file_with_a_new_name_is_a_rename(sandbox):
    tool = MoveFiles(LIVE)
    action = tool.resolve(sources=[str(sandbox / "one.txt")], destination=str(sandbox / "renamed.md"))
    assert tool.run(action).ok
    assert (sandbox / "renamed.md").exists()


def test_move_refuses_to_overwrite_unless_told(sandbox):
    (sandbox / "sub" / "one.txt").write_text("existing", encoding="utf-8")
    tool = MoveFiles(LIVE)
    action = tool.resolve(sources=[str(sandbox / "one.txt")], destination=str(sandbox / "sub"))
    assert action.args["collisions"]
    result = tool.run(action)
    assert result.ok is False and result.undo is None
    assert (sandbox / "sub" / "one.txt").read_text(encoding="utf-8") == "existing"


def test_move_dry_run_changes_nothing(sandbox):
    tool = MoveFiles(DRY)
    result = tool.run(tool.resolve(sources=[str(sandbox / "one.txt")], destination=str(sandbox / "sub")))
    assert result.ok and result.data["dry_run"] is True
    assert (sandbox / "one.txt").exists()


def test_move_with_no_sources_fails_softly(sandbox):
    tool = MoveFiles(LIVE)
    result = tool.run(tool.resolve(sources=[str(sandbox / "ghost.txt")], destination=str(sandbox / "sub")))
    assert result.ok is False and result.undo is None


def test_resolve_never_creates_the_destination(sandbox):
    dest = sandbox / "not-yet"
    MoveFiles(LIVE).resolve(sources=[str(sandbox / "one.txt")], destination=str(dest))
    assert not dest.exists()


@pytest.mark.macos
def test_real_trash_round_trip(tmp_path):
    """Opt-in: exercises NSFileManager.trashItemAtURL against the real Trash."""
    victim = tmp_path / "daa-real-trash-probe.txt"
    victim.write_text("probe", encoding="utf-8")
    tool = MoveToTrash(LIVE)
    result = tool.run(tool.resolve(paths=[str(victim)]))
    assert result.ok and result.undo is not None
    MoveFiles(LIVE).run(MoveFiles(LIVE).resolve(**result.undo.args))
    assert victim.exists()


# ---------------------------------------------------------------------------
# what the user actually hears
#
# A confirmation the user answers has to describe what will happen. These are
# the cases where resolve() already KNEW something destructive and said nothing.
# ---------------------------------------------------------------------------


def test_revealing_and_trashing_do_not_read_back_the_same(sandbox):
    """"report.pdf - should I go ahead?" used to mean both of these."""
    path = str(sandbox / "one.txt")
    reveal = RevealInFinder(DRY).resolve(paths=[path])
    trash = MoveToTrash(DRY).resolve(paths=[path])
    assert reveal.verb and trash.verb
    assert reveal.describe() != trash.describe()
    assert trash.describe().startswith("move to the Trash")


def test_trashing_a_folder_says_how_many_items_go_with_it(sandbox):
    folder = sandbox / "myapp"
    (folder / "nested").mkdir(parents=True)
    for index in range(7):
        (folder / f"f{index}.txt").write_text("x", encoding="utf-8")
    (folder / "nested" / "deep.txt").write_text("x", encoding="utf-8")

    action = MoveToTrash(DRY).resolve(paths=[str(folder)])
    # One spoken name, nine things inside it.
    assert action.targets == ("myapp",)
    assert "9 items" in action.describe()
    assert "myapp" in action.consequences["contents"]
    # ...and Jev scores blast radius against the real number, not len(targets).
    assert action.args["item_count"] == 10


def test_trashing_plain_files_does_not_invent_a_contents_count(sandbox):
    action = MoveToTrash(DRY).resolve(paths=[str(sandbox / "one.txt")])
    assert "contents" not in action.consequences
    assert action.args["item_count"] == 1


def test_a_protected_path_says_the_trash_will_not_happen(sandbox):
    """resolve() drops it and run() then refuses everything, so say so first."""
    action = MoveToTrash(DRY).resolve(paths=[str(sandbox / "one.txt"), str(Path.home())])
    assert action.args["protected"]
    spoken = action.describe()
    assert "protected" in spoken and "nothing will be moved" in spoken
    # The sentence is true because run() really does refuse the whole thing.
    assert MoveToTrash(DRY).run(action).ok is False


def test_overwriting_does_not_read_back_like_the_safe_case(sandbox):
    """Same files, same destination: only the flag that destroys data differs."""
    (sandbox / "sub" / "one.txt").write_text("the only copy", encoding="utf-8")
    tool = MoveFiles(DRY)
    args = {"sources": [str(sandbox / "one.txt")], "destination": str(sandbox / "sub")}
    clobbering = tool.resolve(**args, overwrite=True)
    refusing = tool.resolve(**args)
    assert clobbering.describe() != refusing.describe()
    assert "replacing 1 file" in clobbering.describe()
    assert "nothing will move" in refusing.describe()


def test_overwrite_trashes_what_it_replaces_so_undo_can_restore_it(sandbox, monkeypatch):
    fake = FakeTrash(sandbox / "Trash")
    monkeypatch.setattr(files_mod, "_trash_via_appkit", fake)
    displaced = sandbox / "sub" / "one.txt"
    displaced.write_text("the only copy", encoding="utf-8")

    tool = MoveFiles(LIVE)
    result = tool.run(
        tool.resolve(
            sources=[str(sandbox / "one.txt")],
            destination=str(sandbox / "sub"),
            overwrite=True,
        )
    )
    assert result.ok
    assert displaced.read_text(encoding="utf-8") == "one.txt"   # the move landed
    assert fake.calls == [str(displaced)]                       # the old copy survived
    assert result.data["replaced"]

    replay = MoveFiles(LIVE)
    assert replay.run(replay.resolve(**dict(result.undo.args))).ok
    assert (sandbox / "one.txt").read_text(encoding="utf-8") == "one.txt"
    assert displaced.read_text(encoding="utf-8") == "the only copy"


def test_overwrite_refuses_rather_than_destroy_when_nothing_can_be_trashed(sandbox, monkeypatch):
    monkeypatch.setattr(files_mod, "_trash_via_appkit", lambda p: (False, None, "no pyobjc"))
    monkeypatch.setattr(
        files_mod, "_trash_via_finder", lambda p, dry_run=False: (False, None, "no Finder")
    )
    displaced = sandbox / "sub" / "one.txt"
    displaced.write_text("the only copy", encoding="utf-8")

    tool = MoveFiles(LIVE)
    result = tool.run(
        tool.resolve(
            sources=[str(sandbox / "one.txt")],
            destination=str(sandbox / "sub"),
            overwrite=True,
        )
    )
    assert result.ok is False
    assert displaced.read_text(encoding="utf-8") == "the only copy"
    assert (sandbox / "one.txt").exists()


def test_move_files_no_longer_claims_to_be_fully_reversible():
    hint = MoveFiles.spec.activation_hint.lower()
    assert "fully reversible" not in hint
    assert "trash" in hint   # it now says WHY it can be taken back


# ---------------------------------------------------------------------------
# explicit is a signal, not a constant
# ---------------------------------------------------------------------------


def test_a_glob_is_not_an_explicitly_named_target(sandbox):
    assert MoveToTrash(DRY).resolve(paths=[str(sandbox / "one.txt")]).explicit is True
    assert MoveToTrash(DRY).resolve(paths=[str(sandbox / "*.txt")]).explicit is False
    assert MoveFiles(DRY).resolve(
        sources=[str(sandbox / "*.txt")], destination=str(sandbox / "sub")
    ).explicit is False


def test_a_date_scoped_search_is_not_an_explicitly_named_target():
    assert SpotlightSearch(DRY).resolve(text="invoice").explicit is True
    assert SpotlightSearch(DRY).resolve(kind="screenshot", when="today").explicit is False


def test_a_caller_cannot_talk_a_guess_back_up_into_an_explicit_target(sandbox):
    """explicit=True from the model may lower the signal, never raise it."""
    action = MoveToTrash(DRY).resolve(paths=[str(sandbox / "*.txt")], explicit=True)
    assert action.explicit is False
