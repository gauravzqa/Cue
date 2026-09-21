"""The build-breaking invariant, applied to the browser tools before they land.

`tests/test_undo_coverage.py` iterates the live REGISTRY. These tools are not
registered yet -- registration is the integrator's call once the computer-use
sibling lands -- so the same rules are enforced here against
`BROWSER_TOOL_CLASSES`. When they are registered, this file is what says the
registration will not break that one.

The escape hatch is deliberately expensive. A tool may skip undo only by
  1. declaring `irreversible = True` in code,
  2. carrying the "irreversible" tag in its ToolSpec,
  3. saying so in the activation_hint the router reads,
  4. and sitting at CONFIRM_VOICE or above.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier, Tool, ToolSpec, UndoAction
from daa.tools.browser import BROWSER_TOOL_CLASSES, make_browser_tools
from daa.tools.browser import session as session_mod
from test_browser_fakes import TEST_OPTIONS, FakeBackend, shop_page

LIVE = Settings(dry_run=False)
DRY = Settings(dry_run=True)

BROWSER_DIR = Path(session_mod.__file__).parent
SPECS = [cls.spec for cls in BROWSER_TOOL_CLASSES]
GATED = [spec for spec in SPECS if spec.floor >= RiskTier.CONFIRM_VOICE]
NAMES = {spec.name for spec in SPECS}

# Tools that genuinely have no inverse. Kept as data so the list cannot grow
# without a reviewer seeing it in the diff.
IRREVERSIBLE = {"click_element", "submit_form"}


def kit(page=None, settings=LIVE):
    page = page or shop_page()
    backend = FakeBackend(pages=[page], options=TEST_OPTIONS)
    return page, {
        t.spec.name: t
        for t in make_browser_tools(settings, session=backend, options=TEST_OPTIONS)
    }


# ---------------------------------------------------------------------------
# Probes: one per mutating, reversible tool. Each performs a real mutation
# against the fake page and hands back the ToolResult.
# ---------------------------------------------------------------------------


def _probe_open_tab():
    _, tools = kit()
    tool = tools["open_tab"]
    return tools, tool, tool.run(tool.resolve(url="https://example.com/a"))


def _probe_close_tab():
    _, tools = kit()
    tool = tools["close_tab"]
    return tools, tool, tool.run(tool.resolve(tab_id="t1"))


def _probe_fill_field():
    _, tools = kit()
    tool = tools["fill_field"]
    return tools, tool, tool.run(tool.resolve(text="Search products", value="lamp"))


PROBES = {
    "open_tab": _probe_open_tab,
    "close_tab": _probe_close_tab,
    "fill_field": _probe_fill_field,
}

# Mutating tools with no undo that are NOT gated at CONFIRM_VOICE, and the
# reason each one is honest about it.
MUTATES_WITHOUT_UNDO = {"go_back"}


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def test_there_are_gated_browser_tools_to_check():
    assert GATED, "this file would pass vacuously"


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
def test_gated_tools_are_covered_by_a_probe_or_declared_irreversible(spec):
    assert spec.name in PROBES or spec.name in IRREVERSIBLE


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
def test_gated_tools_declare_that_they_mutate(spec):
    tool = next(c for c in BROWSER_TOOL_CLASSES if c.spec is spec)
    assert tool.mutates is True


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_mutating_tools_return_an_undo(name):
    _, tool, result = PROBES[name]()
    assert result.ok, f"{name} probe did not mutate: {result.error}"
    assert isinstance(result.undo, UndoAction)
    assert result.undo.description
    assert result.undo.tool in NAMES
    assert not tool.irreversible


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_the_undo_names_a_tool_its_own_spec_allows_as_an_inverse(name):
    """The journal is a world-readable file, so the row is not trusted."""
    _, tool, result = PROBES[name]()
    assert tool.spec.inverses
    assert result.undo.tool in tool.spec.inverses


@pytest.mark.parametrize("name", sorted(PROBES), ids=str)
def test_the_undo_is_actually_runnable(name):
    """An UndoAction that cannot be replayed is a comforting lie."""
    tools, _, result = PROBES[name]()
    undo_tool = tools[result.undo.tool]
    replay = undo_tool.run(undo_tool.resolve(**dict(result.undo.args)))
    assert replay.ok, f"replaying {name}'s undo failed: {replay.error}"


def test_every_tool_states_its_inverses_one_way_or_the_other():
    """`()` is a statement ("no inverse exists"), not an omission."""
    for spec in SPECS:
        assert isinstance(spec.inverses, tuple)
        for inverse in spec.inverses:
            assert inverse in NAMES, f"{spec.name} allows unknown tool {inverse}"
        if spec.name in PROBES:
            assert spec.inverses, f"{spec.name} has an undo but no allowlist"
        if spec.name in IRREVERSIBLE:
            assert spec.inverses == (), f"{spec.name} cannot have an inverse"


@pytest.mark.parametrize("name", sorted(IRREVERSIBLE), ids=str)
def test_irreversible_tools_pay_for_it(name):
    tool = next(c for c in BROWSER_TOOL_CLASSES if c.spec.name == name)
    assert tool.irreversible is True
    assert "irreversible" in tool.spec.tags
    assert tool.spec.floor >= RiskTier.CONFIRM_VOICE
    hint = tool.spec.activation_hint.lower()
    assert "undo" in hint or "irreversible" in hint


def test_no_tool_claims_irreversibility_without_the_gate():
    for cls in BROWSER_TOOL_CLASSES:
        if cls.irreversible:
            assert cls.spec.name in IRREVERSIBLE
            assert cls.mutates is True
            assert cls.spec.floor >= RiskTier.CONFIRM_VOICE


def test_read_only_tools_never_claim_to_mutate():
    for cls in BROWSER_TOOL_CLASSES:
        if cls.spec.floor is RiskTier.SILENT:
            assert cls.mutates is False, f"{cls.spec.name} is SILENT but mutates"


def test_a_mutating_tool_below_the_gate_with_no_undo_is_listed_on_purpose():
    """`go_back` is the only one, and its spec says why in reviewed source."""
    _, tools = kit()
    for cls in BROWSER_TOOL_CLASSES:
        if not cls.mutates or cls.spec.name in PROBES or cls.spec.name in IRREVERSIBLE:
            continue
        assert cls.spec.name in MUTATES_WITHOUT_UNDO
    page = shop_page()
    page.history.append("https://shop.example.com/")
    _, tools = kit(page)
    result = tools["go_back"].run(tools["go_back"].resolve())
    assert result.ok and result.undo is None
    assert tools["go_back"].spec.inverses == ()


def test_dry_run_never_hands_back_an_undo():
    page, tools = kit(settings=DRY)
    for name, kwargs in (
        ("open_tab", {"url": "https://example.com/a"}),
        ("close_tab", {"tab_id": "t1"}),
        ("fill_field", {"text": "Search products", "value": "lamp"}),
        ("click_element", {"text": "Show more details"}),
        ("submit_form", {"text": "Search"}),
    ):
        tool = tools[name]
        result = tool.run(tool.resolve(**kwargs))
        assert result.ok and result.undo is None, name
    assert page.clicked == [] and page.filled == [] and page.submitted == []


def test_failed_actions_do_not_invent_an_undo():
    _, tools = kit()
    tool = tools["fill_field"]
    assert tool.run(tool.resolve(text="not-a-field-at-all", value="x")).undo is None
    tool = tools["open_tab"]
    assert tool.run(tool.resolve(url="javascript:alert(1)")).undo is None


# ---------------------------------------------------------------------------
# Static checks, mirroring tests/test_tools_registry.py for the subpackage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(BROWSER_DIR.glob("*.py")), ids=lambda p: p.name)
def test_browser_modules_never_import_jev_or_voice(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for module in imported:
        assert not module.startswith(("daa.jev", "daa.voice")), f"{path.name} imports {module}"


@pytest.mark.parametrize("path", sorted(BROWSER_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_shell_true_anywhere_in_the_browser_package(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "shell", f"{path.name} passes shell="


@pytest.mark.parametrize("cls", BROWSER_TOOL_CLASSES, ids=lambda c: c.spec.name)
def test_every_undo_the_source_can_build_is_declared_as_an_inverse(cls):
    """Static, so it covers paths no probe exercises."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    named = {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "UndoAction"
        for keyword in node.keywords
        if keyword.arg == "tool" and isinstance(keyword.value, ast.Constant)
    }
    assert named <= set(cls.spec.inverses), (
        f"{cls.spec.name} builds an undo calling {sorted(named - set(cls.spec.inverses))}"
    )


# ---------------------------------------------------------------------------
# Registrability: the integrator's step must be a one-liner
# ---------------------------------------------------------------------------


def test_every_browser_tool_satisfies_the_tool_protocol():
    for tool in make_browser_tools(DRY, session=FakeBackend(), options=TEST_OPTIONS):
        assert isinstance(tool, Tool)
        assert isinstance(tool.spec, ToolSpec)


def test_the_specs_are_registrable():
    from daa.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for tool in make_browser_tools(DRY, session=FakeBackend(), options=TEST_OPTIONS):
        registry.register(tool)
    assert len(registry) == len(BROWSER_TOOL_CLASSES)


def test_activation_hints_are_substantial_and_distinct():
    corpus = {spec.name: spec.activation_hint for spec in SPECS}
    for name, hint in corpus.items():
        assert len(hint.split()) >= 15, f"{name} hint is too thin for the router"
        assert "\n" not in hint
    assert len(set(corpus.values())) == len(corpus)


def test_params_are_documented():
    for spec in SPECS:
        for param_name, schema in spec.params.items():
            assert schema.get("type"), f"{spec.name}.{param_name} has no type"
            assert schema.get("description"), f"{spec.name}.{param_name} has no description"


def test_the_expensive_tools_are_never_answered_by_a_grant():
    ungrantable = {spec.name for spec in SPECS if not spec.grantable}
    assert ungrantable == {"submit_form", "summarise_page"}


def test_importing_the_package_registers_nothing_and_starts_nothing():
    """Registration is the integrator's call; import must have no side effects."""
    import daa.tools as tools_pkg
    import daa.tools.browser as browser_pkg

    assert not set(browser_pkg.tool_names()) & set(tools_pkg.REGISTRY.names())
    assert session_mod._SESSION is None or session_mod._SESSION.started is False


def test_importing_the_package_does_not_import_playwright():
    import sys

    assert "daa.tools.browser" in sys.modules
    # Playwright is an optional extra; `import daa` must stay light and the
    # 557 MB browser download must stay opt-in.
    source = (BROWSER_DIR / "session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    for node in top_level:
        names = (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        )
        assert not any(n.startswith("playwright") for n in names)
