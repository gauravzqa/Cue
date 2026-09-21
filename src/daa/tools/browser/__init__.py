"""Browser use: eleven typed tools, and never an autonomous browse loop.

Importing this package registers nothing and starts nothing. It does not import
Playwright, does not create the profile directory, does not launch Chrome and
does not read `.env`. `import daa.tools.browser` on a machine with no browser
extra installed is inert, which is what lets `daa.tools` stay importable in a
test process that must not touch the user's machine.

**The line this package does not cross:** daa never runs a loop that chooses
its own next browser action against a page it has already read. The reason is
arithmetic rather than taste. daa's guarantee is that the sentence the user
answers describes what will happen; an agent loop's next action is a function
of the page it just read, and the page is untrusted input. So the sentence at
confirmation time would have to be either a *plan* the agent may deviate from,
or a *category* ("I'll browse around on amazon"). Neither is a confirmation.

So the conversational model proposes one typed call at a time, and each one
goes resolve() -> risk gate -> policy.decide() -> confirm -> run() like every
other daa tool.

Registration is the integrator's call, not this package's: `BROWSER_TOOL_CLASSES`
is the list, `make_browser_tools()` builds instances bound to one `Settings` and
one `BrowserOptions`, and `install(registry)` registers them.

Shipped here (stages 3 and 4 of docs/roadmap.md):

    list_tabs        SILENT          read-only
    read_page        SILENT          read-only
    find_on_page     SILENT          read-only
    scroll_page      SILENT          read-only
    open_tab         ANNOUNCE        undo: close_tab
    close_tab        ANNOUNCE        undo: open_tab
    go_back          ANNOUNCE        no inverse, stated
    summarise_page   ANNOUNCE        egress, not grantable
    click_element    CONFIRM_VOICE   irreversible, refuses submitting controls
    fill_field       CONFIRM_VOICE   undo only when the field was empty
    submit_form      CONFIRM_VISUAL  irreversible, not grantable, no inverse

Deliberately NOT shipped:

  - `run_page_js`. Model-authored JavaScript in a logged-in origin is
    `run_applescript` with cookies. If it ever ships it takes CONFIRM_VISUAL,
    `irreversible=True`, and the same derive-the-readback-from-the-code
    treatment -- not a keyword denylist, which is a losing game on a
    Turing-complete language.
  - `fill_credential`, or any password or payment field. Refused in code by
    `fill_field`, not merely confirmed harder.
  - `download_file`. Needs a design pass with `files.py` so the saved file gets
    a real `UndoAction`.
  - `browse_read`, the read-only multi-page capability, and `attach_to_chrome`.
    Both are later stages; `page.ReadOnlyPage` already exists as the type
    `browse_read` would be handed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from daa.config import Settings
from daa.tools.browser.acting import ACTING_TOOL_CLASSES, ClickElement, FillField, SubmitForm
from daa.tools.browser.base import BrowserTool
from daa.tools.browser.facts import PageFacts, dom_fingerprint, facts_from_raw
from daa.tools.browser.reading import (
    READING_TOOL_CLASSES,
    CloseTab,
    FindOnPage,
    GoBack,
    ListTabs,
    OpenTab,
    ReadPage,
    ScrollPage,
    SummarisePage,
)
from daa.tools.browser.session import BrowserOptions, get_session, reset_session

BROWSER_TOOL_CLASSES: tuple[type[BrowserTool], ...] = (
    *READING_TOOL_CLASSES,
    *ACTING_TOOL_CLASSES,
)


def make_browser_tools(
    settings: Settings | None = None,
    *,
    session: Any = None,
    options: BrowserOptions | None = None,
) -> list[BrowserTool]:
    """Fresh instances sharing one browser. Nothing is started."""
    return [cls(settings, session=session, options=options) for cls in BROWSER_TOOL_CLASSES]


def install(
    registry: Any,
    settings: Settings | None = None,
    *,
    session: Any = None,
    options: BrowserOptions | None = None,
) -> Any:
    for tool in make_browser_tools(settings, session=session, options=options):
        registry.register(tool)
    return registry


def tool_names() -> Sequence[str]:
    return tuple(cls.spec.name for cls in BROWSER_TOOL_CLASSES)


__all__ = [
    "BROWSER_TOOL_CLASSES",
    "BrowserOptions",
    "BrowserTool",
    "ClickElement",
    "CloseTab",
    "FillField",
    "FindOnPage",
    "GoBack",
    "ListTabs",
    "OpenTab",
    "PageFacts",
    "ReadPage",
    "ScrollPage",
    "SubmitForm",
    "SummarisePage",
    "dom_fingerprint",
    "facts_from_raw",
    "get_session",
    "install",
    "make_browser_tools",
    "reset_session",
    "tool_names",
]
