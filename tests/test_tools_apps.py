"""apps.open_app resolution, against a fake /Applications tree.

Picking the wrong app is the headline failure mode for this tool, so most of
these tests are about what `resolve()` decides BEFORE anything launches: the
winner, the runners-up, and its own confidence in the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier
from daa.tools import apps as apps_mod
from daa.tools.apps import ListRunningApps, OpenApp, discover_apps, find_app
from daa.tools.base import ShellResult, fuzzy_score, normalize_spoken

LIVE = Settings(dry_run=False)
DRY = Settings(dry_run=True)

INSTALLED = [
    "Safari.app",
    "Safari Technology Preview.app",
    "Mail.app",
    "Messages.app",
    "Visual Studio Code.app",
    "Google Chrome.app",
    "Music.app",
]
NESTED = ["Terminal.app", "Activity Monitor.app"]


@pytest.fixture
def fake_applications(tmp_path, monkeypatch):
    """A whole /Applications tree under tmp_path, including a Utilities folder."""
    for name in INSTALLED:
        (tmp_path / name).mkdir()
    utilities = tmp_path / "Utilities"
    utilities.mkdir()
    for name in NESTED:
        (utilities / name).mkdir()
    monkeypatch.setattr(apps_mod, "APP_DIRS", (str(tmp_path),))
    discover_apps(refresh=True)
    yield tmp_path
    # Leave the cache clean so a later test never sees this fake tree.
    discover_apps(refresh=True)


@pytest.fixture
def launches(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake(argv, **kwargs):
        # Mirrors run_argv's own contract, so dry_run stays under test even
        # though the real subprocess call is gone.
        if kwargs.get("mutating") and kwargs.get("dry_run"):
            return ShellResult(argv=tuple(argv), skipped=True)
        calls.append(tuple(argv))
        return ShellResult(argv=tuple(argv))

    monkeypatch.setattr("daa.tools.base.run_argv", fake)
    return calls


# ---------------------------------------------------------------------------
# normalisation and scoring
# ---------------------------------------------------------------------------


def test_spoken_noise_is_stripped():
    assert normalize_spoken("the Safari app") == "safari"
    assert normalize_spoken("Visual Studio Code!") == "visual studio code"


def test_exact_beats_prefix():
    assert fuzzy_score("safari", "Safari") > fuzzy_score("safari", "Safari Technology Preview")


def test_unrelated_names_score_low():
    assert fuzzy_score("safari", "Activity Monitor") < 0.5


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_includes_nested_utilities(fake_applications):
    names = {p.stem for p in discover_apps()}
    assert "Terminal" in names and "Activity Monitor" in names
    assert "Utilities" not in names


def test_discovery_survives_an_unreadable_folder(fake_applications, monkeypatch):
    missing = fake_applications / "gone"
    monkeypatch.setattr(apps_mod, "APP_DIRS", (str(fake_applications), str(missing)))
    assert discover_apps(refresh=True)


def test_find_app_returns_none_rather_than_guessing(fake_applications):
    assert find_app("safari") is not None
    assert find_app("quantum spreadsheet") is None


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_picks_the_exact_app_and_names_it(fake_applications):
    action = OpenApp(DRY).resolve(name="the safari app")
    assert action.targets == ("Safari",)
    assert action.args["confident"] is True
    assert Path(action.args["path"]).name == "Safari.app"


def test_resolve_reports_plausible_alternates(fake_applications):
    action = OpenApp(DRY).resolve(name="chrome")
    assert action.targets == ("Google Chrome",)
    names = {alt["name"] for alt in action.args["alternates"]}
    assert "Activity Monitor" not in names  # weak matches are noise, not options


def test_resolve_flags_a_near_tie_as_ambiguous(fake_applications, tmp_path):
    (tmp_path / "Mail Designer.app").mkdir()
    discover_apps(refresh=True)
    action = OpenApp(DRY).resolve(name="mail")
    assert action.targets == ("Mail",)
    assert action.args["alternates"]


def test_resolve_handles_a_speech_mangled_name(fake_applications):
    action = OpenApp(DRY).resolve(name="vs code")
    assert action.targets == ("Visual Studio Code",)


def test_resolve_accepts_a_bundle_identifier(fake_applications):
    action = OpenApp(DRY).resolve(name="com.apple.Safari")
    assert action.args["bundle_id"] == "com.apple.Safari"


def test_resolve_on_an_unknown_name_has_no_target(fake_applications):
    action = OpenApp(DRY).resolve(name="quantum spreadsheet deluxe")
    assert action.targets == ()
    assert action.args["path"] is None


def test_resolve_does_not_launch_anything(fake_applications, launches):
    OpenApp(LIVE).resolve(name="safari")
    assert launches == []


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_shells_out_to_open_with_an_argv_list(fake_applications, launches):
    tool = OpenApp(LIVE)
    result = tool.run(tool.resolve(name="safari"))
    assert result.ok and result.summary == "Opened Safari."
    assert launches[0][:2] == ("/usr/bin/open", "-a")
    assert launches[0][2].endswith("Safari.app")


def test_run_passes_files_through(fake_applications, launches, tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hi", encoding="utf-8")
    tool = OpenApp(LIVE)
    tool.run(tool.resolve(name="safari", files=[str(doc)]))
    assert str(doc) in launches[0]


def test_run_uses_the_bundle_id_flag(fake_applications, launches):
    tool = OpenApp(LIVE)
    tool.run(tool.resolve(name="com.apple.Safari"))
    assert launches[0][:2] == ("/usr/bin/open", "-b")


def test_dry_run_does_not_launch(fake_applications, launches):
    tool = OpenApp(DRY)
    result = tool.run(tool.resolve(name="safari"))
    assert result.ok and result.data["dry_run"] is True
    assert launches == []


def test_run_on_an_unknown_name_fails_softly(fake_applications, launches):
    tool = OpenApp(LIVE)
    result = tool.run(tool.resolve(name="quantum spreadsheet deluxe"))
    assert result.ok is False and result.error
    assert launches == []


def test_open_failure_is_reported_not_raised(fake_applications, monkeypatch):
    monkeypatch.setattr(
        "daa.tools.base.run_argv",
        lambda argv, **kw: ShellResult(argv=tuple(argv), returncode=1, stderr="no such app"),
    )
    tool = OpenApp(LIVE)
    result = tool.run(tool.resolve(name="safari"))
    assert result.ok is False and "no such app" in (result.error or "")


def test_open_app_declares_no_undo_on_purpose():
    # Quitting an app the user has since typed into is worse than a stray window.
    assert OpenApp.spec.floor is RiskTier.ANNOUNCE
    assert OpenApp.irreversible is False


# ---------------------------------------------------------------------------
# list_running_apps
# ---------------------------------------------------------------------------


def test_list_running_apps_is_silent_and_read_only():
    assert ListRunningApps.spec.floor is RiskTier.SILENT
    assert ListRunningApps.mutates is False


def test_list_running_apps_reads_the_workspace():
    tool = ListRunningApps(DRY)
    result = tool.run(tool.resolve())
    assert result.ok
    assert isinstance(result.data["apps"], list)


def test_list_running_apps_degrades_without_appkit(monkeypatch):
    """A missing framework costs this tool, never the process."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "AppKit":
            raise ImportError("no AppKit here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    tool = ListRunningApps(DRY)
    result = tool.run(tool.resolve())
    assert result.ok is False
    assert result.data["degraded"] == "appkit"
    assert result.data["remedy"]


# ---------------------------------------------------------------------------
# readback and inference honesty
# ---------------------------------------------------------------------------


def test_opening_an_app_reads_back_verb_first(fake_applications):
    action = OpenApp(DRY).resolve(name="mail")
    assert action.verb == "open"
    assert action.describe() == "open Mail"


def test_a_fuzzy_match_with_live_rivals_is_not_an_explicitly_named_target(fake_applications):
    """"open safari" is exact. "open saf" is the resolver choosing for you."""
    assert OpenApp(DRY).resolve(name="safari").explicit is True
    ambiguous = OpenApp(DRY).resolve(name="safari tech")
    assert ambiguous.args["alternates"]
    assert ambiguous.explicit is False


def test_a_caller_cannot_talk_a_guess_back_up_into_an_explicit_target(fake_applications):
    assert OpenApp(DRY).resolve(name="safari tech", explicit=True).explicit is False
