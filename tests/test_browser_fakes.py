"""Fakes for the browser tools, plus the tests that keep the fakes honest.

The browser tools talk to `page.PageHandle` and `page.BrowserBackend`, never to
Playwright, so the entire safety surface -- readback, fingerprint, escalation,
refusal, never-log -- is exercised here with no browser, no profile, no Chrome
and no network. `test_browser_live.py` runs the same tools against real Chrome
and local fixture files, and skips cleanly when Playwright is not installed.

A fake is only worth something if it cannot drift from the thing it stands in
for. Two defences: the element records below are the SAME schema the vendored
`FACTS_JS` snippet returns (that is asserted against the snippet's source
here), and `test_browser_live.py` asserts the real adapter produces records
this module's builders can consume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from daa.tools.base import Degraded
from daa.tools.browser import page as pagemod
from daa.tools.browser.page import TabInfo
from daa.tools.browser.session import BrowserOptions

TEST_OPTIONS = BrowserOptions(
    headless=True,
    # The test suite serves fixtures from 127.0.0.1; conftest allows loopback
    # and blocks everything else. Nothing here ever reaches a real site.
    allow_private_hosts=True,
    summarize_private_pages=False,
)


# ---------------------------------------------------------------------------
# Element records -- the schema FACTS_JS returns
# ---------------------------------------------------------------------------


def form(
    *,
    action: str = "",
    method: str = "get",
    names: tuple[str, ...] = (),
    types: tuple[str, ...] = (),
    hints: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "action": action,
        "method": method,
        "field_names": list(names),
        "field_types": list(types),
        "autocomplete_hints": list(hints),
        "field_count": max(len(names), len(types)),
    }


def elem(
    selector: str,
    *,
    tag: str = "BUTTON",
    role: str = "button",
    name: str = "",
    input_type: str = "",
    autocomplete: str = "",
    visible: bool = True,
    enabled: bool = True,
    in_dialog: bool = False,
    checked: Any = None,
    value_empty: Any = None,
    box: tuple[int, int, int, int] = (10, 100, 120, 40),
    href: str = "",
    target: str = "",
    download: bool = False,
    submits: bool = False,
    form_facts: dict[str, Any] | None = None,
    nearby_amount: str = "",
) -> tuple[str, dict[str, Any]]:
    return selector, {
        "tag": tag,
        "role": role,
        "name": name,
        "input_type": input_type,
        "autocomplete": autocomplete,
        "visible": visible,
        "enabled": enabled,
        "in_dialog": in_dialog,
        "checked": checked,
        "value_empty": value_empty,
        "box": list(box),
        "href": href,
        "target": target,
        "download": download,
        "submits": submits,
        "form": form_facts,
        "nearby_amount": nearby_amount,
    }


# ---------------------------------------------------------------------------
# The fake page
# ---------------------------------------------------------------------------


@dataclass
class FakePage:
    page_url: str = "https://example.com/"
    page_title: str = "Example"
    elements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""
    headings: tuple[str, ...] = ()
    cookies: list[dict[str, Any]] = field(default_factory=list)
    dialog: str = ""
    id: str = "t1"
    history: list[str] = field(default_factory=list)
    clicked: list[str] = field(default_factory=list)
    filled: list[tuple[str, str]] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    scrolls: list[tuple[str, str]] = field(default_factory=list)

    # -- reading ---------------------------------------------------------

    def url(self) -> str:
        return self.page_url

    def title(self) -> str:
        return self.page_title

    def pending_dialog(self) -> str:
        return self.dialog

    def element_facts(self, selector: str, *, limit: int = 8) -> list[dict[str, Any]]:
        out = []
        for sel, raw in self.elements:
            if sel == selector:
                record = dict(raw)
                record["index"] = len(out)
                out.append(record)
        return out[:limit]

    def controls(self, *, limit: int = 60) -> list[dict[str, Any]]:
        return [
            {"index": i, "name": raw["name"], "role": raw["role"], "selector": sel}
            for i, (sel, raw) in enumerate(self.elements)
            if raw.get("name")
        ][:limit]

    def extract(self) -> dict[str, Any]:
        return {
            "title": self.page_title,
            "text": self.text,
            "headings": list(self.headings),
            "chars": len(self.text),
            "words": len(self.text.split()) if self.text else 0,
            "links": 0,
        }

    def find(self, needle: str, *, limit: int = 10) -> dict[str, Any]:
        hits = [m for m in re.finditer(re.escape(needle.lower()), self.text.lower())]
        return {
            "count": len(hits),
            "matches": [
                {"snippet": self.text[max(0, m.start() - 40): m.end() + 40], "heading": ""}
                for m in hits[:limit]
            ],
        }

    def cookie_names(self) -> list[str]:
        return [str(c.get("name", "")) for c in self.cookies]

    def has_session(self) -> bool:
        return pagemod.looks_signed_in(self.cookies)

    # -- acting ----------------------------------------------------------

    def scroll(self, direction: str, amount: str) -> dict[str, Any]:
        self.scrolls.append((direction, amount))
        return {"y": 400, "fraction": 0.5, "at_top": direction == "top",
                "at_bottom": direction == "bottom"}

    def click(self, selector: str) -> None:
        self.clicked.append(selector)

    def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))
        for i, (sel, raw) in enumerate(self.elements):
            if sel == selector:
                updated = dict(raw)
                updated["value_empty"] = not value
                self.elements[i] = (sel, updated)

    def submit(self, selector: str) -> None:
        self.submitted.append(selector)

    def go_back(self) -> bool:
        if not self.history:
            return False
        self.page_url = self.history.pop()
        return True

    # -- test helpers ----------------------------------------------------

    def mutate(self, selector: str, **changes: Any) -> None:
        """Simulate the DOM moving between resolve() and "yes"."""
        for i, (sel, raw) in enumerate(self.elements):
            if sel == selector:
                updated = dict(raw)
                updated.update(changes)
                self.elements[i] = (sel, updated)


@dataclass
class FakeBackend:
    pages: list[FakePage] = field(default_factory=list)
    options: BrowserOptions = TEST_OPTIONS
    degraded: Degraded | None = None
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    started: bool = False

    def ensure(self) -> Degraded | None:
        if self.degraded is None:
            self.started = True
        return self.degraded

    def tabs(self) -> list[TabInfo]:
        from daa.tools.browser import urls

        return [
            TabInfo(
                id=p.id, title=p.title(), safe_url=urls.log_url(p.url()),
                host=urls.speakable_host(p.url()), active=(i == 0),
            )
            for i, p in enumerate(self.pages)
        ]

    def page(self, tab_id: str | None = None) -> FakePage | None:
        if tab_id:
            return next((p for p in self.pages if p.id == tab_id), None)
        return self.pages[0] if self.pages else None

    def open_tab(self, url: str) -> FakePage:
        self.opened.append(url)
        page = FakePage(page_url=url, page_title="Opened", id=f"t{len(self.pages) + 1}")
        self.pages.insert(0, page)
        return page

    def close_tab(self, tab_id: str) -> bool:
        before = len(self.pages)
        self.pages = [p for p in self.pages if p.id != tab_id]
        if len(self.pages) != before:
            self.closed.append(tab_id)
            return True
        return False


# ---------------------------------------------------------------------------
# Ready-made fixtures
# ---------------------------------------------------------------------------

SESSION_COOKIE = [{"name": "sessionid", "httpOnly": True, "secure": True}]
ANALYTICS_ONLY = [{"name": "_ga", "httpOnly": False, "secure": False}]

PAY_FORM = form(
    action="https://checkout.stripe.com/pay",
    method="post",
    names=("email", "cardnumber", "cvc", ""),
    types=("email", "text", "text", "submit"),
    hints=("", "cc-number", "cc-csc", ""),
)

SEARCH_FORM = form(action="https://shop.example.com/search", method="get",
                   names=("q", ""), types=("search", "submit"), hints=("", ""))


def checkout_page() -> FakePage:
    """The §7.2 fixture: a payment form whose action host is not the page host."""
    return FakePage(
        page_url="https://stripe.com/checkout/order",
        page_title="Order summary",
        text="Order summary\nTotal: $412.00",
        headings=("Order summary",),
        cookies=list(SESSION_COOKIE),
        elements=[
            elem("#pay", name="Pay $412.00", input_type="submit", submits=True,
                 form_facts=PAY_FORM, nearby_amount="$412.00"),
            elem("#email", tag="INPUT", role="textbox", name="Email", input_type="email",
                 value_empty=True, form_facts=PAY_FORM, box=(10, 20, 200, 30)),
            elem("#cardnumber", tag="INPUT", role="textbox", name="Card number",
                 input_type="text", autocomplete="cc-number", value_empty=True,
                 form_facts=PAY_FORM, box=(10, 60, 200, 30)),
            elem("#nameless", name="", box=(10, 300, 20, 20)),
            elem("#offsite", tag="A", role="link", name="Continue",
                 href="https://evil-checkout.example.net/go", box=(10, 400, 80, 20)),
        ],
    )


def shop_page() -> FakePage:
    """An ordinary page: a search box, a harmless button, a GET form."""
    return FakePage(
        page_url="https://shop.example.com/products/42?ref=abc",
        page_title="A nice lamp",
        text="A nice lamp\n" + ("lamp details " * 50),
        headings=("A nice lamp", "Delivery"),
        cookies=list(ANALYTICS_ONLY),
        elements=[
            elem("#q", tag="INPUT", role="searchbox", name="Search products",
                 input_type="search", value_empty=True, form_facts=SEARCH_FORM,
                 box=(10, 10, 200, 30)),
            elem("#go", name="Search", input_type="submit", submits=True,
                 form_facts=SEARCH_FORM, box=(220, 10, 60, 30)),
            elem("#more", name="Show more details", box=(10, 200, 140, 30)),
            elem("#delete", name="Delete this review", box=(10, 260, 140, 30)),
            elem("#nameless", name="", box=(10, 320, 20, 20)),
        ],
    )


# ---------------------------------------------------------------------------
# The fakes must not drift from the snippet they stand in for
# ---------------------------------------------------------------------------

_SCHEMA_KEYS = {
    "tag", "role", "name", "input_type", "autocomplete", "visible", "enabled",
    "in_dialog", "checked", "value_empty", "box", "href", "target", "download",
    "submits", "form", "nearby_amount",
}


def test_the_fake_element_record_matches_the_snippet_schema():
    """Every key the fake produces is a key FACTS_JS actually returns."""
    source = pagemod.FACTS_JS
    _, record = elem("#x")
    for key in record:
        assert re.search(rf"\b{re.escape(key)}\s*:", source), f"FACTS_JS has no {key}"
    assert set(record) == _SCHEMA_KEYS


def test_the_snippet_never_returns_an_input_value():
    """The one review property of the vendored JavaScript."""
    for snippet in (pagemod.FACTS_JS, pagemod.CONTROLS_JS, pagemod.FIND_JS,
                    pagemod.EXTRACT_JS, pagemod.SCROLL_JS):
        assert "document.cookie" not in snippet
        assert "localStorage" not in snippet
        assert "sessionStorage" not in snippet
        assert "innerHTML" not in snippet
    # `el.value` appears exactly twice in FACTS_JS, both times inside the
    # emptiness test, and never as a returned field.
    assert re.findall(r"el\.value", pagemod.FACTS_JS) == ["el.value"]
    assert "value:" not in pagemod.FACTS_JS.replace("value_empty:", "")


def test_the_fake_backend_satisfies_the_protocols():
    backend = FakeBackend(pages=[shop_page()])
    assert isinstance(backend, pagemod.BrowserBackend)
    assert isinstance(backend.page(), pagemod.PageHandle)
    assert isinstance(backend.page(), pagemod.ReadOnlyPage)
