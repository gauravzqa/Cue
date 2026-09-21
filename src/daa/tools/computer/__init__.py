"""Computer use: press a NAMED element, never a coordinate.

`ui_click(target="the Save button", app="TextEdit")`. The spoken confirmation
is composed from accessibility attributes only, so a press the model calls
"the OK button" reads back as *"press the Delete Account button in Account
Settings in Safari"*. That is the whole capability; everything else here is in
service of it.

Nothing in this package captures the screen. There is no screenshot, no vision
grounding and no `CGWindowListCreateImage` call, which means the monthly
Screen Recording re-consent never breaks it and the "a screenshot can contain a
TCC Allow dialog with an Allow button in it" escalation is unreachable rather
than mitigated. Window titles come from `kAXTitleAttribute`, served by the
target app over the accessibility channel.

Registration is deliberately NOT done here: importing this package must have no
side effects, and the tool table is assembled in `daa.tools.__init__`.
"""

from __future__ import annotations

from daa.tools.computer.ax import AppInfo, Element, Snapshot, snapshot
from daa.tools.computer.keys import KeyCombo, parse_combo
from daa.tools.computer.naming import assess, compose_target, destructive_words
from daa.tools.computer.tools import (
    COMPUTER_TOOL_CLASSES,
    UiClick,
    UiDescribe,
    UiKey,
    UiSequence,
    UiType,
)

__all__ = [
    "COMPUTER_TOOL_CLASSES",
    "AppInfo",
    "Element",
    "KeyCombo",
    "Snapshot",
    "UiClick",
    "UiDescribe",
    "UiKey",
    "UiSequence",
    "UiType",
    "assess",
    "compose_target",
    "destructive_words",
    "parse_combo",
    "snapshot",
]
