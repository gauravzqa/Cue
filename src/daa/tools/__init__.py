"""The action layer: typed args in, ToolResult plus undo out.

Importing this package registers every tool. Registration is the ONLY import
side effect -- constructing a tool reads nothing, opens nothing and asks TCC for
nothing, so `import daa.tools` is safe on a machine with zero permissions
granted and inside a test process that must not touch the user's machine.

Nothing here may import daa.jev or daa.voice. The dependency runs one way: the
loop knows about tools, tools know only about contracts and config.
"""

from __future__ import annotations

from daa.config import Settings
from daa.contracts import Tool
from daa.tools.applescript import RunAppleScript
from daa.tools.apps import ListRunningApps, OpenApp
from daa.tools.base import BaseTool, Degraded, ShellResult, ShellTool, run_argv
from daa.tools.clipboard import GetClipboard, SetClipboard
from daa.tools.files import MoveFiles, MoveToTrash, RevealInFinder, SpotlightSearch
from daa.tools.permissions import check_permissions, guidance, permission_report
from daa.tools.registry import REGISTRY, ToolRegistry, build_registry
from daa.tools.shortcuts import ListShortcuts, RunShortcut
from daa.tools.windows import FocusWindow, ListWindows

# Every tool class in the product. The registry is built from this one list so
# that "did you remember to register it?" is not a thing a reviewer has to ask.
TOOL_CLASSES: tuple[type[BaseTool], ...] = (
    OpenApp,
    ListRunningApps,
    SpotlightSearch,
    RevealInFinder,
    MoveToTrash,
    MoveFiles,
    GetClipboard,
    SetClipboard,
    ListShortcuts,
    RunShortcut,
    RunAppleScript,
    ListWindows,
    FocusWindow,
)


def optional_tool_classes(settings: Settings | None = None) -> tuple[type, ...]:
    """Capability packages, included only when switched on.

    Imported INSIDE this function, never at module scope. `daa.tools.browser`
    pulls Playwright, which is an optional extra and a 557 MB browser download;
    making `import daa.tools` depend on it would break a bare clone and the
    keyless dev loop for everyone who never asked for a browser.

    An enabled-but-unimportable package is reported, not swallowed: silently
    registering nothing would leave the user saying "open a tab" to an
    assistant that has no idea what a tab is and no idea why.
    """
    s = settings if settings is not None else Settings.load()
    extra: list[type] = []
    missing: list[str] = []

    if getattr(s, "enable_computer_use", False):
        try:
            from daa.tools.computer import COMPUTER_TOOL_CLASSES

            extra.extend(COMPUTER_TOOL_CLASSES)
        except ImportError as exc:
            missing.append(f"computer use: {exc}")

    if getattr(s, "enable_browser", False):
        try:
            from daa.tools.browser import BROWSER_TOOL_CLASSES

            extra.extend(BROWSER_TOOL_CLASSES)
        except ImportError as exc:
            missing.append(f"browser: {exc}")

    optional_tool_classes.missing = tuple(missing)  # type: ignore[attr-defined]
    return tuple(extra)


optional_tool_classes.missing = ()  # type: ignore[attr-defined]


def make_tools(settings: Settings | None = None) -> list[Tool]:
    """Fresh instances bound to one Settings -- how tests get a dry_run=False world."""
    classes = TOOL_CLASSES + optional_tool_classes(settings)
    return [cls(settings) for cls in classes]


def install(registry: ToolRegistry, settings: Settings | None = None) -> ToolRegistry:
    for tool in make_tools(settings):
        registry.register(tool)
    return registry


install(REGISTRY)

__all__ = [
    "REGISTRY",
    "TOOL_CLASSES",
    "BaseTool",
    "Degraded",
    "FocusWindow",
    "GetClipboard",
    "ListRunningApps",
    "ListShortcuts",
    "ListWindows",
    "MoveFiles",
    "MoveToTrash",
    "OpenApp",
    "RevealInFinder",
    "RunAppleScript",
    "RunShortcut",
    "SetClipboard",
    "ShellResult",
    "ShellTool",
    "SpotlightSearch",
    "ToolRegistry",
    "build_registry",
    "check_permissions",
    "guidance",
    "install",
    "make_tools",
    "permission_report",
    "run_argv",
]
