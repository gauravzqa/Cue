"""Stage 3: the tools that read, and the one that sends.

The property these hold up: nothing in this file can press a control. That is
enforced by the type -- a reading tool is handed a `ReadOnlyPage`, which has no
`click`, no `fill` and no `submit` -- rather than by asking a model nicely.
"""

from __future__ import annotations

import os

import pytest

from daa.config import Settings
from daa.contracts import RiskTier
from daa.tools.base import Degraded
from daa.tools.browser import make_browser_tools
from daa.tools.browser.extract import round_words, truncate_structured
from daa.tools.browser.page import ReadOnlyPage, looks_signed_in
from daa.tools.browser.session import BrowserOptions, PageCache
from test_browser_fakes import (
    TEST_OPTIONS,
    FakeBackend,
    checkout_page,
    shop_page,
)

DRY = Settings(dry_run=True)
LIVE = Settings(dry_run=False)


def kit(*pages, settings=LIVE, options=TEST_OPTIONS, degraded=None):
    backend = FakeBackend(pages=list(pages), options=options, degraded=degraded)
    return backend, {
        t.spec.name: t for t in make_browser_tools(settings, session=backend, options=options)
    }


# ---------------------------------------------------------------------------
# list_tabs
# ---------------------------------------------------------------------------


def test_list_tabs_reports_sites_and_never_query_strings():
    _, tools = kit(shop_page())
    result = tools["list_tabs"].run(tools["list_tabs"].resolve())
    assert result.ok
    assert result.data["tabs"][0]["url"] == "https://shop.example.com/products/42"
    assert "ref=abc" not in str(result.data)


def test_with_nothing_open_it_says_so_instead_of_failing():
    _, tools = kit()
    result = tools["list_tabs"].run(tools["list_tabs"].resolve())
    assert result.ok and result.data["tabs"] == []


def test_a_missing_browser_degrades_with_a_spoken_remedy_rather_than_raising():
    degraded = Degraded(
        capability="browser",
        remedy="install the browser extra and I can use a web browser",
        detail="playwright is not installed",
    )
    _, tools = kit(shop_page(), degraded=degraded)
    for name in ("list_tabs", "read_page", "find_on_page", "scroll_page"):
        result = tools[name].run(tools[name].resolve(text="x"))
        assert result.ok is False
        assert result.data["remedy"] == degraded.remedy


# ---------------------------------------------------------------------------
# read_page
# ---------------------------------------------------------------------------


def test_read_page_returns_text_and_a_spoken_shape_rather_than_the_text():
    _, tools = kit(shop_page())
    result = tools["read_page"].run(tools["read_page"].resolve())
    assert result.ok
    assert "lamp details" in result.data["text"]
    assert "lamp details" not in result.summary
    assert result.summary.endswith(".") and len(result.summary) <= 140


def test_the_summary_is_speakable():
    _, tools = kit(shop_page())
    summary = tools["read_page"].run(tools["read_page"].resolve()).summary
    assert "/" not in summary
    assert not any(mark in summary for mark in ("*", "`", "_", "#", "\n", "http"))


def test_a_page_behind_a_login_returns_its_shape_and_not_its_body():
    """The exposure the README did not cover: this text is one step from a model."""
    _, tools = kit(checkout_page())
    result = tools["read_page"].run(tools["read_page"].resolve())
    assert result.ok
    assert result.data["logged_in"] is True
    assert result.data["private"] is True
    assert "text" not in result.data
    assert result.data["headings"] == ["Order summary"]


def test_the_overflow_copy_is_0600_and_is_never_written_for_a_private_page(tmp_path):
    options = BrowserOptions(
        headless=True, allow_private_hosts=True,
        page_cache_dir=tmp_path / "pages", read_page_max_chars=100,
    )
    page = shop_page()
    _, tools = kit(page, options=options)
    result = tools["read_page"].run(tools["read_page"].resolve())
    assert result.data["truncated"] is True
    path = result.data["overflow_path"]
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert oct(os.stat(tmp_path / "pages").st_mode)[-3:] == "700"
    # And the path is nowhere near the spoken sentence.
    assert path not in result.summary

    _, private_tools = kit(checkout_page(), options=options)
    private = private_tools["read_page"].run(private_tools["read_page"].resolve())
    assert "overflow_path" not in private.data


def test_the_overflow_cache_expires(tmp_path):
    cache = PageCache(tmp_path / "pages", ttl_s=0.0)
    path = cache.store("hello")
    assert os.path.exists(path)
    assert cache.sweep() == 1
    assert not os.path.exists(path)


def test_shape_redaction_runs_before_the_text_leaves_the_page():
    page = shop_page()
    page.text = "Your key is sk-abcdefghijklmnop and card 4111 1111 1111 1111."
    _, tools = kit(page)
    text = tools["read_page"].run(tools["read_page"].resolve()).data["text"]
    assert "sk-abcdefghijklmnop" not in text
    assert "4111 1111 1111 1111" not in text
    assert "<redacted card number>" in text


def test_truncation_cuts_on_a_boundary_and_never_mid_word():
    body = "alpha beta gamma\n\ndelta epsilon zeta"
    cut, truncated = truncate_structured(body, 20)
    assert truncated and cut == "alpha beta gamma"
    assert truncate_structured("short", 100) == ("short", False)


@pytest.mark.parametrize(
    "words,expected",
    [(0, "no text"), (10, "a couple of sentences"), (120, "a few paragraphs"),
     (460, "about 500 words"), (3200, "about 3.2 thousand words")],
)
def test_word_counts_are_spoken_not_printed(words, expected):
    assert round_words(words) == expected


# ---------------------------------------------------------------------------
# find_on_page
# ---------------------------------------------------------------------------


def test_find_on_page_counts_without_quoting_the_page():
    page = shop_page()
    page.text = "The lamp is nice. The lamp is cheap."
    _, tools = kit(page)
    tool = tools["find_on_page"]
    result = tool.run(tool.resolve(text="lamp"))
    assert result.ok and result.data["count"] == 2
    assert "The lamp is nice" not in result.summary
    assert result.data["matches"][0]["snippet"]


def test_find_on_page_works_on_a_private_page_because_nothing_leaves():
    _, tools = kit(checkout_page())
    tool = tools["find_on_page"]
    result = tool.run(tool.resolve(text="Total"))
    assert result.ok and result.data["count"] == 1


def test_find_on_page_also_reports_controls_so_a_click_has_something_to_aim_at():
    _, tools = kit(shop_page())
    tool = tools["find_on_page"]
    result = tool.run(tool.resolve(text="Delete"))
    assert [c["name"] for c in result.data["controls"]] == ["Delete this review"]


# ---------------------------------------------------------------------------
# scroll_page
# ---------------------------------------------------------------------------


def test_scrolling_is_silent_because_announcing_it_would_be_noise():
    _, tools = kit(shop_page())
    assert tools["scroll_page"].spec.floor is RiskTier.SILENT
    assert tools["scroll_page"].mutates is False


def test_scroll_reports_where_it_landed():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["scroll_page"]
    result = tool.run(tool.resolve(direction="bottom"))
    assert result.ok and result.data["at_bottom"] is True
    assert page.scrolls == [("bottom", "page")]


def test_an_unknown_direction_falls_back_rather_than_guessing_wildly():
    _, tools = kit(shop_page())
    assert tools["scroll_page"].resolve(direction="sideways").args["direction"] == "down"


# ---------------------------------------------------------------------------
# open_tab
# ---------------------------------------------------------------------------


def test_open_tab_refuses_the_schemes_that_are_not_pages():
    _, tools = kit(shop_page())
    tool = tools["open_tab"]
    for raw in ("javascript:alert(1)", "file:///etc/passwd", "chrome://settings"):
        action = tool.resolve(url=raw)
        assert action.args["refused"]
        result = tool.run(action)
        assert result.ok is False
        assert "alert(1)" not in result.summary and "/etc/passwd" not in result.summary


def test_open_tab_refuses_the_local_network_unless_constructed_to_allow_it():
    blocked = BrowserOptions(headless=True, allow_private_hosts=False)
    _, tools = kit(shop_page(), options=blocked)
    action = tools["open_tab"].resolve(url="http://192.168.1.1/setup")
    assert action.args["refused"]
    assert tools["open_tab"].run(action).ok is False


def test_leaving_the_current_site_escalates_and_is_spoken():
    _, tools = kit(shop_page())
    action = tools["open_tab"].resolve(url="https://evil-checkout.example.net/go")
    assert action.floor_hint is RiskTier.CONFIRM_VOICE
    assert "evil-checkout.example.net" in action.describe()


def test_credentials_in_the_address_go_all_the_way_to_a_card():
    _, tools = kit(shop_page())
    action = tools["open_tab"].resolve(url="https://bob:pw@intranet.example.com/x")
    assert action.floor_hint is RiskTier.CONFIRM_VISUAL
    assert "username and password" in action.describe()
    assert "pw" not in action.targets[0]


def test_open_tab_returns_an_undo_that_closes_the_tab_it_opened():
    backend, tools = kit(shop_page())
    tool = tools["open_tab"]
    result = tool.run(tool.resolve(url="https://example.com/a"))
    assert result.ok and result.undo is not None
    assert result.undo.tool == "close_tab"
    assert result.undo.tool in tool.spec.inverses
    close = tools["close_tab"]
    replay = close.run(close.resolve(**dict(result.undo.args)))
    assert replay.ok and backend.closed


def test_a_dry_run_opens_nothing_and_offers_no_undo():
    backend, tools = kit(shop_page(), settings=DRY)
    tool = tools["open_tab"]
    result = tool.run(tool.resolve(url="https://example.com/a"))
    assert result.ok and result.undo is None and result.data["dry_run"] is True
    assert backend.opened == []
    assert backend.started is False       # a dry run does not even start Chrome


# ---------------------------------------------------------------------------
# close_tab / go_back
# ---------------------------------------------------------------------------


def test_closing_a_tab_offers_to_reopen_the_page_without_the_query_string():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["close_tab"]
    result = tool.run(tool.resolve(tab_id="t1"))
    assert result.ok and result.undo.tool == "open_tab"
    assert result.undo.args["url"] == "https://shop.example.com/products/42"
    assert "ref=abc" not in str(result.undo.args)


def test_a_browser_undo_row_stops_being_replayable():
    page = shop_page()
    _, tools = kit(page)
    close = tools["close_tab"]
    stale = close.resolve(tab_id="t1", expires_at=1.0)
    result = close.run(stale)
    assert result.ok is False and "too long ago" in result.summary


def test_go_back_declares_no_inverse_rather_than_pretending_forward_is_one():
    _, tools = kit(shop_page())
    assert tools["go_back"].spec.inverses == ()
    hint = tools["go_back"].spec.activation_hint
    assert "no forward tool" in hint


def test_go_back_with_no_history_says_so():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["go_back"]
    assert tool.run(tool.resolve()).ok is False
    page.history.append("https://shop.example.com/")
    assert tool.run(tool.resolve()).ok is True


# ---------------------------------------------------------------------------
# summarise_page: the egress
# ---------------------------------------------------------------------------


def test_summarise_page_is_its_own_tool_at_announce_and_is_never_grantable():
    _, tools = kit(shop_page())
    spec = tools["summarise_page"].spec
    assert spec.floor is RiskTier.ANNOUNCE
    assert spec.grantable is False


def test_the_egress_is_announced_before_the_text_leaves():
    """ANNOUNCE means "do it, then say". The tool only GATHERS; the send
    happens in the layer that owns models, after this sentence is spoken."""
    _, tools = kit(shop_page())
    tool = tools["summarise_page"]
    action = tool.resolve()
    assert "sends what is on this page to the language model" in action.describe()
    result = tool.run(action)
    assert result.ok and result.data["egress"] == "model"
    assert "Sending what's on shop.example.com to the model" in result.summary


def test_a_private_page_is_refused_by_default_rather_than_truncated():
    _, tools = kit(checkout_page())
    tool = tools["summarise_page"]
    result = tool.run(tool.resolve())
    assert result.ok is False
    assert "won't send a page you're signed into" in result.summary
    assert "text" not in result.data


def test_turning_the_setting_on_is_a_constructor_decision_not_a_tool_argument():
    permissive = BrowserOptions(headless=True, allow_private_hosts=True,
                                summarize_private_pages=True)
    _, tools = kit(checkout_page(), options=permissive)
    tool = tools["summarise_page"]
    assert "summarize_private_pages" not in tool.spec.params
    result = tool.run(tool.resolve())
    assert result.ok and result.data["logged_in"] is True
    # Escalated for the user to hear, even with the setting on.
    assert tool.resolve().floor_hint is RiskTier.CONFIRM_VOICE


# ---------------------------------------------------------------------------
# session detection
# ---------------------------------------------------------------------------


def test_signed_in_detection_reads_names_and_flags_and_never_a_value():
    assert looks_signed_in([{"name": "sessionid", "httpOnly": True, "secure": True}])
    assert looks_signed_in([{"name": "whatever", "httpOnly": True, "secure": True}])
    assert not looks_signed_in([{"name": "_ga", "httpOnly": True, "secure": True}])
    assert not looks_signed_in([{"name": "theme", "httpOnly": False, "secure": False}])
    assert not looks_signed_in([])


# ---------------------------------------------------------------------------
# the read-only capability is enforced by the type
# ---------------------------------------------------------------------------


def test_a_read_only_page_has_no_way_to_act():
    """A prompt injection saying "now click Pay" reaches code with no click."""
    assert not hasattr(ReadOnlyPage, "click")
    assert not hasattr(ReadOnlyPage, "fill")
    assert not hasattr(ReadOnlyPage, "submit")
    assert hasattr(ReadOnlyPage, "extract")
