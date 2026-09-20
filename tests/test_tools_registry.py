"""Registry invariants, plus the cross-cutting rules every tool must obey.

These are the tests that catch a NEW tool doing the wrong thing, which is the
only kind of regression a growing tool table actually suffers from.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from daa import tools
from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier, Tool, ToolSpec
from daa.tools import clipboard as clipboard_mod
from daa.tools.registry import REGISTRY, ToolRegistry, build_registry

TOOLS_DIR = Path(tools.__file__).parent

EXPECTED_FLOORS = {
    "open_app": RiskTier.ANNOUNCE,
    "list_running_apps": RiskTier.SILENT,
    "spotlight_search": RiskTier.SILENT,
    "reveal_in_finder": RiskTier.SILENT,
    "move_to_trash": RiskTier.CONFIRM_VOICE,
    "move_files": RiskTier.CONFIRM_VOICE,
    "get_clipboard": RiskTier.SILENT,
    "set_clipboard": RiskTier.ANNOUNCE,
    "list_shortcuts": RiskTier.SILENT,
    "run_shortcut": RiskTier.CONFIRM_VOICE,
    "run_applescript": RiskTier.CONFIRM_VISUAL,
    "list_windows": RiskTier.SILENT,
    "focus_window": RiskTier.ANNOUNCE,
}


def dry() -> Settings:
    return Settings(dry_run=True)


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class _Fake:
    def __init__(self, name: str, hint: str = "do a thing") -> None:
        self.spec = ToolSpec(name=name, description="d", params={}, activation_hint=hint)

    def resolve(self, **kwargs):
        return ResolvedAction(tool=self.spec.name, args=kwargs)

    def run(self, action):
        raise AssertionError("not called")


def test_register_get_and_contains():
    reg = ToolRegistry()
    fake = _Fake("alpha")
    assert reg.register(fake) is fake
    assert reg.get("alpha") is fake
    assert "alpha" in reg
    assert fake in reg
    assert "beta" not in reg
    assert len(reg) == 1
    assert list(reg) == [fake]


def test_get_unknown_names_what_exists():
    reg = build_registry([_Fake("alpha")])
    with pytest.raises(KeyError) as excinfo:
        reg.get("nope")
    assert "alpha" in str(excinfo.value)


def test_duplicate_registration_is_rejected():
    reg = build_registry([_Fake("alpha")])
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(_Fake("alpha"))


def test_tool_without_activation_hint_is_rejected():
    # A tool the semantic router cannot see is dead code that looks alive.
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="activation_hint"):
        reg.register(_Fake("mute", hint=""))


def test_non_tool_is_rejected():
    reg = ToolRegistry()
    with pytest.raises(TypeError):
        reg.register(object())  # type: ignore[arg-type]


def test_specs_are_sorted_and_complete():
    reg = build_registry([_Fake("zulu"), _Fake("alpha")])
    assert [s.name for s in reg.specs()] == ["alpha", "zulu"]


# ---------------------------------------------------------------------------
# The populated REGISTRY
# ---------------------------------------------------------------------------


def test_importing_daa_tools_registers_everything():
    assert len(REGISTRY) == len(tools.TOOL_CLASSES)
    assert set(REGISTRY.names()) == set(EXPECTED_FLOORS)


def test_every_tool_satisfies_the_protocol():
    for tool in REGISTRY:
        assert isinstance(tool, Tool)


@pytest.mark.parametrize("name,floor", sorted((k, v) for k, v in EXPECTED_FLOORS.items()))
def test_declared_floors(name, floor):
    assert REGISTRY.get(name).spec.floor is floor


def test_activation_hints_are_substantial_and_distinct():
    corpus = REGISTRY.activation_corpus()
    for name, hint in corpus.items():
        assert len(hint.split()) >= 15, f"{name} hint is too thin for the router"
        assert "\n" not in hint
    assert len(set(corpus.values())) == len(corpus)


def test_params_are_documented():
    for spec in REGISTRY.specs():
        for param_name, schema in spec.params.items():
            assert schema.get("type"), f"{spec.name}.{param_name} has no type"
            assert schema.get("description"), f"{spec.name}.{param_name} has no description"


def test_by_floor_and_by_tag():
    risky = {s.name for s in REGISTRY.by_floor(RiskTier.CONFIRM_VOICE)}
    assert risky == {"move_to_trash", "move_files", "run_shortcut", "run_applescript"}
    assert "move_to_trash" in {s.name for s in REGISTRY.by_tag("destructive")}


# ---------------------------------------------------------------------------
# Layering and safety rules that apply to the whole package
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(TOOLS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_tools_never_import_jev_or_voice(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for module in imported:
        assert not module.startswith(("daa.jev", "daa.voice")), f"{path.name} imports {module}"


@pytest.mark.parametrize("path", sorted(TOOLS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_shell_true_and_no_rm(path):
    """A spoken filename must never be able to become shell syntax."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "shell", f"{path.name} passes shell="
        # `rm` must never be how a file leaves the disk -- see files.MoveToTrash.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.split() != ["rm"], f"{path.name} mentions rm as a command"
            assert not node.value.startswith(("rm ", "/bin/rm")), f"{path.name} shells out to rm"


def test_resolve_is_pure_for_every_tool(tmp_path, monkeypatch):
    """resolve() runs before the risk gate, so it may not touch anything."""
    monkeypatch.chdir(tmp_path)
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    args_by_tool = {
        "open_app": {"name": "safari"},
        "list_running_apps": {},
        "spotlight_search": {"text": "invoice", "when": "today"},
        "reveal_in_finder": {"paths": [str(sample)]},
        "move_to_trash": {"paths": [str(sample)]},
        "move_files": {"sources": [str(sample)], "destination": str(tmp_path / "out")},
        "get_clipboard": {},
        "set_clipboard": {"text": "hello"},
        "list_shortcuts": {},
        "run_shortcut": {"name": "morning"},
        "run_applescript": {"script": 'tell application "Finder" to activate'},
        "list_windows": {},
        "focus_window": {"app": "finder"},
    }
    for name, kwargs in args_by_tool.items():
        tool = type(REGISTRY.get(name))(dry())
        action = tool.resolve(**kwargs)
        assert action.tool == name
        assert isinstance(action, ResolvedAction)
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert after == before


def test_summaries_are_speakable(monkeypatch):
    """ToolResult.summary is read aloud: no paths, no ids, no markdown."""
    monkeypatch.setattr(clipboard_mod, "read_clipboard_text", lambda: ("a note", ""))
    results = []
    for name, kwargs in (
        ("get_clipboard", {}),
        ("set_clipboard", {"text": "hello"}),
        ("open_app", {"name": "definitely-not-an-app-xyz"}),
        ("move_to_trash", {"paths": ["/nope/definitely-missing-xyz"]}),
    ):
        tool = type(REGISTRY.get(name))(dry())
        results.append(tool.run(tool.resolve(**kwargs)))
    for result in results:
        summary = result.summary
        assert summary and summary[0].isupper() and summary.endswith((".", "?", "!"))
        assert len(summary) <= 140
        assert "/" not in summary
        assert not any(mark in summary for mark in ("*", "`", "_", "#", "\n"))


def test_focus_window_refuses_to_guess_a_target():
    """No app, no title, no id -- resolving to "the first window" would be a guess."""
    tool = type(REGISTRY.get("focus_window"))(dry())
    action = tool.resolve()
    assert action.targets == ()
    assert tool.run(action).ok is False


def test_run_argv_refuses_a_command_string():
    from daa.tools.base import run_argv

    with pytest.raises(TypeError, match="never a command string"):
        run_argv("ls -la /")  # type: ignore[arg-type]


def test_run_argv_skips_mutating_commands_in_dry_run():
    from daa.tools.base import run_argv

    result = run_argv(["/bin/mkdir", "/tmp/daa-should-never-exist"], mutating=True, dry_run=True)
    assert result.skipped and result.ok
    assert not Path("/tmp/daa-should-never-exist").exists()


def test_run_argv_reports_a_missing_binary_without_raising():
    from daa.tools.base import run_argv

    result = run_argv(["/usr/bin/definitely-not-a-binary-xyz"], timeout=5)
    assert result.ok is False and "not installed" in result.failure_reason


def test_permissions_never_raise_and_always_answer():
    from daa.tools.permissions import check_permissions, guidance, permission_report

    flags = check_permissions()
    assert set(flags) == {"accessibility", "automation", "screen_recording"}
    assert all(isinstance(v, bool) for v in flags.values())
    for state in permission_report().values():
        assert state.status in {"granted", "denied", "not_determined", "unknown"}
        assert state.granted or state.remedy  # anything missing comes with a fix
    assert all(isinstance(line, str) for line in guidance())


def test_dry_run_never_claims_completion():
    tool = type(REGISTRY.get("set_clipboard"))(dry())
    result = tool.run(tool.resolve(text="hello"))
    assert result.ok and result.data["dry_run"] is True
    assert result.undo is None  # a dry run has nothing to undo


def test_settings_are_injectable_and_not_read_at_import(monkeypatch):
    monkeypatch.setenv("DAA_DRY_RUN", "0")
    tool = type(REGISTRY.get("move_files"))(Settings(dry_run=True))
    assert tool.dry_run is True  # injected settings win over the environment
