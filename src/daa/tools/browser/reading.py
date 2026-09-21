"""Stage 3: reading the web, with no way to click anything.

Eight tools, none of which can press a control. `read_page`, `find_on_page`,
`scroll_page` and `list_tabs` cannot change any state outside the browser's
viewport, cannot send data and cannot navigate, so they run at `SILENT`.
`scroll_page` is `SILENT` rather than `ANNOUNCE` on purpose: announcing "I
scrolled down" to someone who asked you to read them a page is noise, and noise
trains people to stop listening to announcements -- which is the actual safety
cost.

`open_tab`, `close_tab` and `go_back` navigate, which is observable and mostly
reversible, so they announce. `open_tab` additionally escalates per invocation
from the URL alone -- a different site, credentials in the address, a bare IP,
a punycode homograph -- because those are things the user could not have meant
by saying a site's name out loud.

`summarise_page` is the odd one and it is the reason this stage has eight tools
rather than seven. See its docstring.
"""

from __future__ import annotations

from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, UndoAction
from daa.tools.base import as_sentence, param, spoken_snippet
from daa.tools.browser import facts as factlib
from daa.tools.browser import urls
from daa.tools.browser.base import BrowserTool, browser_spec, expired, expiry
from daa.tools.browser.extract import heading_outline, page_shape, truncate_structured
from daa.tools.browser.privacy import redact_shapes
from daa.tools.browser.session import PageCache

NO_BROWSER = "There's no page open."


def _count(n: int, singular: str, plural: str | None = None) -> str:
    plural = plural or singular + "s"
    return f"{n} {singular if n == 1 else plural}"


# ---------------------------------------------------------------------------
# list_tabs
# ---------------------------------------------------------------------------


class ListTabs(BrowserTool):
    verb = "list"

    spec = browser_spec(
        "list_tabs",
        "List the pages open in the assistant's browser.",
        {},
        floor=RiskTier.SILENT,
        activation_hint="""
            what have you got open, which tabs are open, what pages are you looking at,
            list the browser tabs, what site am I on, which window were you using, show
            me what is open in the browser. Read-only inventory of the assistant's own
            browser -- never the user's personal Chrome. Returns one line per tab with
            the site and the page title, and the tab handles the other browser tools
            take. Changes nothing at all.
        """,
        tags=("browser", "read"),
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return self.browser_action(targets=["the pages I have open"], verb=self.verb)

    def run(self, action: ResolvedAction) -> ToolResult:
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        tabs = list(self.backend.tabs())
        rows = [
            {"id": t.id, "title": t.title, "url": t.safe_url, "host": t.host, "active": t.active}
            for t in tabs
        ]
        if not rows:
            return ToolResult(ok=True, summary="I don't have any pages open.", data={"tabs": []})
        first = spoken_snippet(tabs[0].host) or "a page"
        if len(rows) == 1:
            summary = f"I have one page open, on {first}."
        else:
            summary = f"I have {_count(len(rows), 'page')} open, the first on {first}."
        return ToolResult(ok=True, summary=as_sentence(summary), data={"tabs": rows})


# ---------------------------------------------------------------------------
# read_page
# ---------------------------------------------------------------------------


class ReadPage(BrowserTool):
    verb = "read"

    spec = browser_spec(
        "read_page",
        "Read the main text of a page that is already open in the browser.",
        {"tab_id": param("string", "Which open tab to read; the active one by default")},
        floor=RiskTier.SILENT,
        activation_hint="""
            read me this page, what does it say, what is on this page, read that article
            out, what is the gist, tell me what this says, read the rest of it. Extracts
            the main body text of a page already open in the assistant's browser, with
            the navigation, cookie banners, sidebars and footers stripped out. Does not
            open, click, type or navigate. On a page you are signed into it returns the
            headings and a word count rather than the body, because sending private page
            content anywhere is a separate decision.
        """,
        tags=("browser", "read"),
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        return self.browser_action(
            targets=["the page you have open"], verb=self.verb, tab_id=tab_id
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        extracted = page.extract()
        site = urls.speakable_host(page.url())
        logged_in = page.has_session()
        # Shape redaction runs on the way OUT of the page, not only on the way
        # into a log: a page displaying API keys or a card number would
        # otherwise reach the conversational layer intact.
        text = redact_shapes(str(extracted.get("text") or ""))
        capped, truncated = truncate_structured(text, self.options.read_page_max_chars)

        data: dict[str, Any] = {
            "url": urls.log_url(page.url()),
            "site": site,
            "title": extracted.get("title") or "",
            "words": int(extracted.get("words") or 0),
            "chars": len(text),
            "headings": heading_outline(extracted),
            "logged_in": logged_in,
            "truncated": truncated,
        }
        if logged_in:
            # The body of a page behind a login is the user's private data -- a
            # bank statement, a DM thread, a medical portal. It is not withheld
            # because reading it is dangerous; it is withheld because whatever
            # this tool returns is one step away from being sent to a model,
            # and that decision belongs to summarise_page, which announces it.
            data["private"] = True
            data["egress_required"] = True
            return ToolResult(
                ok=True,
                summary=page_shape(extracted, site=site, logged_in=True),
                data=data,
            )
        data["text"] = capped
        if truncated:
            # The overflow copy never exists for a signed-in page, and its PATH
            # is never logged: a filename next to a timestamp is a browsing
            # history, which is the thing this package refuses to keep.
            cache = PageCache(self.options.page_cache_dir, ttl_s=self.options.page_cache_ttl_s)
            try:
                data["overflow_path"] = cache.store(text)
            except OSError:
                data["overflow_path"] = ""
        return ToolResult(
            ok=True, summary=page_shape(extracted, site=site, logged_in=False), data=data
        )


# ---------------------------------------------------------------------------
# find_on_page
# ---------------------------------------------------------------------------


class FindOnPage(BrowserTool):
    verb = "look for"

    spec = browser_spec(
        "find_on_page",
        "Count and locate a phrase on the page that is open, without reading the whole thing.",
        {
            "text": param("string", "The words to look for", required=True),
            "tab_id": param("string", "Which open tab to search; the active one by default"),
        },
        floor=RiskTier.SILENT,
        activation_hint="""
            does it mention, find on this page, search within the page, is there anything
            about, where does it say, ctrl-f this page for, how many times does it say.
            Searches only the page already open in the assistant's browser and reports
            how many times the phrase appears, plus the surrounding words and any
            controls with that name. Works on pages you are signed into, because nothing
            leaves the machine. Not a web search and not a file search.
        """,
        tags=("browser", "read"),
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        needle = str(kwargs.get("text") or kwargs.get("query") or "").strip()
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        # The needle is the user's own words, not page content, so it is
        # speakable -- but it is still capped, because a paragraph pasted into
        # `targets` would be a paragraph in the audit log.
        label = spoken_snippet(needle, limit=60) or "that phrase"
        return self.browser_action(
            targets=[label], verb=self.verb, explicit=bool(needle),
            text=needle, tab_id=tab_id,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        needle = str(action.args.get("text") or "")
        if not needle:
            return self.failed("I didn't catch what to look for.", "empty needle")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        found = page.find(needle, limit=8)
        controls = [
            {"name": c.get("name", ""), "role": c.get("role", ""), "selector": c.get("selector", "")}
            for c in page.controls()
            if needle.lower() in str(c.get("name", "")).lower()
        ][:8]
        count = int(found.get("count") or 0)
        label = spoken_snippet(needle, limit=40) or "that"
        if not count and not controls:
            summary = f"I couldn't find {label} on the page."
        elif count:
            summary = f"I found {label} {_count(count, 'time')} on the page."
        else:
            summary = f"There's a control called {label} on the page."
        return ToolResult(
            ok=True,
            summary=as_sentence(summary),
            # Snippets are page content, so they live in `data` and nowhere
            # else: `data` is the one field the audit event builders never read.
            data={
                "count": count,
                "matches": [
                    {"snippet": redact_shapes(str(m.get("snippet", "")))}
                    for m in (found.get("matches") or [])
                ],
                "controls": controls,
            },
        )


# ---------------------------------------------------------------------------
# scroll_page
# ---------------------------------------------------------------------------


_DIRECTIONS = {"down", "up", "top", "bottom"}


class ScrollPage(BrowserTool):
    verb = "scroll"

    spec = browser_spec(
        "scroll_page",
        "Scroll the open page up, down, to the top or to the bottom.",
        {
            "direction": param("string", "down, up, top or bottom"),
            "amount": param("string", "line, page or all"),
            "tab_id": param("string", "Which open tab to scroll; the active one by default"),
        },
        floor=RiskTier.SILENT,
        activation_hint="""
            scroll down, keep going, further down the page, go back up, take me to the
            top, jump to the bottom, next screenful, show me the rest. Moves the viewport
            of a page already open in the assistant's browser. Changes nothing on the
            page, sends nothing, clicks nothing -- it only decides which part of the page
            is in view for the next read. Use between reads of a long article.
        """,
        tags=("browser", "read"),
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        direction = str(kwargs.get("direction") or "down").strip().lower()
        if direction not in _DIRECTIONS:
            direction = "down"
        amount = str(kwargs.get("amount") or "page").strip().lower()
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        return self.browser_action(
            targets=[f"{direction} the page"], verb=self.verb,
            direction=direction, amount=amount, tab_id=tab_id,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        direction = str(action.args.get("direction") or "down")
        where = page.scroll(direction, str(action.args.get("amount") or "page"))
        if where.get("at_bottom"):
            summary = "That's the end of the page."
        elif where.get("at_top"):
            summary = "We're back at the top."
        else:
            summary = f"Scrolled {direction}."
        return ToolResult(ok=True, summary=as_sentence(summary), data=dict(where))


# ---------------------------------------------------------------------------
# open_tab
# ---------------------------------------------------------------------------


class OpenTab(BrowserTool):
    mutates = True
    verb = "open"

    spec = browser_spec(
        "open_tab",
        "Open a web address in a new tab in the assistant's browser.",
        {"url": param("string", "The address or site to open", required=True)},
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            open, go to, pull up, bring up that site, load the page, visit, take me to,
            open a tab for, have a look at that website. Opens one address in the
            assistant's own browser -- never the user's personal Chrome window. Refuses
            anything that is not an ordinary http or https page, and refuses addresses on
            your own machine or local network. Says out loud when the address leaves the
            site you are on, carries a username and password, or is a bare IP address.
        """,
        tags=("browser", "navigate"),
        inverses=("close_tab",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        raw = str(kwargs.get("url") or kwargs.get("site") or "").strip()
        verdict = urls.classify(raw, allow_private=self.options.allow_private_hosts)
        if not verdict.ok:
            return self.refusal(
                targets=[spoken_snippet(raw, limit=40) or "that address"],
                verb=self.verb,
                reason=verdict.reason,
                url="",
            )
        # Where we are NOW is part of what "open that" means: the same address
        # is unremarkable on the site you are on and worth saying out loud when
        # it takes you somewhere else.
        from_etld1 = ""
        try:
            page = self.page_for(None) if self.backend is not None else None
            from_etld1 = urls.etld1(urls.host_of(page.url())) if page is not None else ""
        except Exception:  # noqa: BLE001 - resolve() never fails because of the browser
            from_etld1 = ""
        consequences = factlib.navigation_consequences(verdict, from_etld1=from_etld1)
        return self.browser_action(
            targets=[verdict.etld1 or verdict.host],
            verb=self.verb,
            explicit=bool(kwargs.get("explicit", True)),
            consequences=consequences,
            floor_hint=factlib.navigation_hint(verdict, from_etld1=from_etld1),
            origin=urls.origin_of(verdict.url),
            # The FULL url is what we navigate to and stays in args; only the
            # safe form is ever read back or logged.
            url=verdict.url,
            safe_url=verdict.safe_url,
            host=verdict.host,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        refused = action.args.get("refused")
        if refused:
            return self.failed(as_sentence(f"I won't open that — {refused}"), str(refused))
        url = str(action.args.get("url") or "")
        if not url:
            return self.failed("I didn't catch which page to open.", "no url")
        site = urls.speakable_host(url)
        if self.dry_run:
            return self.dry(f"I would open {site}.", site=site)
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        try:
            page = self.backend.open_tab(url)
        except Exception as exc:  # noqa: BLE001
            return self.failed(
                as_sentence(f"I couldn't open {site}"), f"navigation failed ({type(exc).__name__})"
            )
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Opened {site}"),
            data={"tab_id": page.id, "url": urls.log_url(page.url()), "site": site},
            undo=UndoAction(
                description=f"close the tab I opened on {site}",
                tool="close_tab",
                # A tab id is meaningless after the session ends, which is
                # correct: a stale undo row should be inert, not dangerous.
                args={"tab_id": page.id, "expires_at": expiry(self.options)},
            ),
        )


# ---------------------------------------------------------------------------
# close_tab
# ---------------------------------------------------------------------------


class CloseTab(BrowserTool):
    mutates = True
    verb = "close"

    spec = browser_spec(
        "close_tab",
        "Close one of the tabs open in the assistant's browser.",
        {"tab_id": param("string", "Which tab to close; the active one by default")},
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            close that tab, shut that page, get rid of that window, close the browser
            page, I am done with that one, dismiss it. Closes a single tab in the
            assistant's own browser. Can be taken back by opening the page again, but the
            address it reopens keeps only the site and the path -- anything after a
            question mark is deliberately not remembered, because that part of an address
            is frequently a login token.
        """,
        tags=("browser", "navigate"),
        inverses=("open_tab",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        site = ""
        safe_url = ""
        try:
            page = self.page_for(tab_id)
            if page is not None:
                site = urls.speakable_host(page.url())
                safe_url = urls.log_url(page.url())
        except Exception:  # noqa: BLE001
            page = None
        return self.browser_action(
            targets=[f"the tab on {site}" if site else "that tab"],
            verb=self.verb,
            tab_id=tab_id,
            site=site,
            safe_url=safe_url,
            expires_at=kwargs.get("expires_at"),
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if expired(action.args):
            return self.failed(
                "That was too long ago for me to take back.", "undo entry expired"
            )
        if self.dry_run:
            return self.dry("I would close that tab.")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        tab_id = action.args.get("tab_id")
        if not tab_id:
            page = self.page_for(None)
            tab_id = page.id if page is not None else None
        if not tab_id:
            return self.failed(NO_BROWSER, "no tab")
        site = str(action.args.get("site") or "")
        safe_url = str(action.args.get("safe_url") or "")
        if not self.backend.close_tab(str(tab_id)):
            return self.failed("That tab isn't open any more.", "unknown tab")
        undo = None
        if safe_url:
            undo = UndoAction(
                # Honest about what it will and will not restore: the query
                # string is dropped on purpose, so this reopens the page and
                # not the exact session-scoped URL.
                description=f"open the page on {site} again",
                tool="open_tab",
                args={"url": safe_url, "expires_at": expiry(self.options)},
            )
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Closed the tab on {site}" if site else "Closed that tab"),
            data={"tab_id": tab_id, "site": site},
            undo=undo,
        )


# ---------------------------------------------------------------------------
# go_back
# ---------------------------------------------------------------------------


class GoBack(BrowserTool):
    mutates = True
    verb = "go back on"

    spec = browser_spec(
        "go_back",
        "Go back to the previous page in the assistant's browser.",
        {"tab_id": param("string", "Which open tab; the active one by default")},
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            go back, back a page, previous page, undo that navigation, return to where we
            were, back to the search results, that was the wrong link. Steps one entry
            back in the browser history of a page already open in the assistant's
            browser. There is deliberately no forward tool: on a modern site forward is
            not a reliable inverse of back, and offering one that sometimes re-submits a
            form would be worse than offering none.
        """,
        tags=("browser", "navigate"),
        # NOT ("go_forward",). Forward is another navigation whose meaning the
        # site defines -- on a single-page app it can replay a state the user
        # never chose, and after a POST it can re-submit. An inverse that only
        # sometimes inverts is worse than an honest "there is none".
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        site = ""
        try:
            page = self.page_for(tab_id)
            site = urls.speakable_host(page.url()) if page is not None else ""
        except Exception:  # noqa: BLE001
            site = ""
        return self.browser_action(
            targets=[f"the page on {site}" if site else "that page"],
            verb=self.verb, tab_id=tab_id,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if self.dry_run:
            return self.dry("I would go back a page.")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        try:
            moved = page.go_back()
        except Exception as exc:  # noqa: BLE001
            return self.failed("I couldn't go back.", f"history move failed ({type(exc).__name__})")
        if not moved:
            return self.failed("There's nothing to go back to.", "no history")
        site = urls.speakable_host(page.url())
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Went back to {site}" if site else "Went back"),
            data={"url": urls.log_url(page.url()), "site": site},
        )


# ---------------------------------------------------------------------------
# summarise_page
# ---------------------------------------------------------------------------


class SummarisePage(BrowserTool):
    """The tool that exists because of an exposure the README does not cover.

    Summarising a page means **sending its text to a remote model**. If the page
    is behind a login, that is the user's private data leaving the machine -- a
    bank statement, a DM thread, a medical portal. Nothing in daa's model
    covered that, because until now no tool returned page content.

    So the egress is its own tool rather than a mode of `read_page`, and it sits
    at `ANNOUNCE` rather than `SILENT`. The ordering works out exactly right:
    this tool gathers the text LOCALLY and announces what is about to happen,
    and the send happens afterwards, in the layer that owns models. The user
    hears "I'll send what's on this page to the model" before anything leaves.

    `summarize_private_pages` defaults to **False**, and with it off a page
    behind a login is refused rather than truncated -- `read_page`,
    `find_on_page` and the heading list still work, entirely on this machine.
    """

    verb = "send to the model to summarise"

    spec = browser_spec(
        "summarise_page",
        "Send the text of the open page to the language model so it can be summarised.",
        {"tab_id": param("string", "Which open tab to summarise; the active one by default")},
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            summarise this page, give me the gist, what is this about in one line, sum up
            the article, tldr this, condense what is on screen. Hands the page's text to
            the language model, which means the contents of the page leave this machine.
            Announced before it happens for exactly that reason, and refused outright on
            a page you are signed into unless that has been turned on by hand.
        """,
        tags=("browser", "read", "egress"),
        inverses=(),
        # Never answered in advance by a scoped grant: a grant given for "find
        # that flight" must not silently authorise shipping a logged-in page
        # somewhere. This is the decision that has to be made while awake.
        grantable=False,
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        site = ""
        logged_in = False
        try:
            page = self.page_for(tab_id)
            if page is not None:
                site = urls.speakable_host(page.url())
                logged_in = page.has_session()
        except Exception:  # noqa: BLE001
            page = None
        consequences = {"egress": "which sends what is on this page to the language model"}
        if logged_in:
            consequences["logged_in"] = "and you are signed in on that page"
        return self.browser_action(
            targets=[f"the page on {site}" if site else "the page you have open"],
            verb=self.verb,
            consequences=consequences,
            floor_hint=RiskTier.CONFIRM_VOICE if logged_in else None,
            origin=None,
            tab_id=tab_id,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        logged_in = page.has_session()
        if logged_in and not self.options.summarize_private_pages:
            return self.failed(
                "I won't send a page you're signed into to the model.",
                "summarising private pages is turned off",
                logged_in=True,
            )
        extracted = page.extract()
        text = redact_shapes(str(extracted.get("text") or ""))
        capped, truncated = truncate_structured(text, self.options.read_page_max_chars)
        site = urls.speakable_host(page.url())
        return ToolResult(
            ok=True,
            summary=as_sentence(
                f"Sending what's on {site} to the model to summarise" if site
                else "Sending what's on this page to the model to summarise"
            ),
            data={
                "text": capped,
                "egress": "model",
                "truncated": truncated,
                "words": int(extracted.get("words") or 0),
                "site": site,
                "logged_in": logged_in,
            },
        )


READING_TOOL_CLASSES = (
    ListTabs,
    ReadPage,
    FindOnPage,
    ScrollPage,
    OpenTab,
    CloseTab,
    GoBack,
    SummarisePage,
)

__all__ = [
    "READING_TOOL_CLASSES",
    "CloseTab",
    "FindOnPage",
    "GoBack",
    "ListTabs",
    "OpenTab",
    "ReadPage",
    "ScrollPage",
    "SummarisePage",
]
