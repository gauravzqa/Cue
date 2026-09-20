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


def make_tools(settings: Settings | None = None) -> list[Tool]:
    """Fresh instances bound to one Settings -- how tests get a dry_run=False world."""
    return [cls(settings) for cls in TOOL_CLASSES]


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
