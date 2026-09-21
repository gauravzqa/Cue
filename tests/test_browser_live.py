"""The same tools, against a real browser and static files on 127.0.0.1.

Everything else in `test_browser_*.py` runs on fakes. This file is what keeps
the fakes honest: it drives real Chrome through the real Playwright adapter and
asserts that the accessible names, form facts and fingerprints come out the way
the rest of the suite assumes.

Three rules it obeys, and they are not negotiable:

  - **Local fixtures only.** A temporary directory of static HTML, served over
    loopback by `http.server`. No test here ever navigates to a real website,
    not even a harmless one. `conftest.py` allows loopback and blocks
    everything else.
  - **A scratch profile.** `profile_dir` is under `tmp_path`, so the user's
    real Chrome profile and their browsing data are never touched, and no
    login of theirs is ever used.
  - **It skips cleanly.** Playwright is an optional extra and the browsers are
    a 557 MB opt-in download, so on a fresh clone this file skips with a
    sentence that says what to install. `daa` must stay importable and the
    suite must stay green without any of it.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from daa.config import Settings
from daa.contracts import RiskTier
from daa.tools.browser import facts as factlib
from daa.tools.browser import make_browser_tools
from daa.tools.browser.acting import ROUTE_TO_SUBMIT
from daa.tools.browser.base import STALE_SUMMARY
from daa.tools.browser.session import BrowserOptions, PlaywrightSession

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed: `pip install 'daa[browser]'` to run the live browser tests",
)

LIVE = Settings(dry_run=False)

CHECKOUT_HTML = """<!doctype html>
<html><head><title>Order summary</title></head><body>
<h1>Order summary</h1>
<form id="payform" method="post" action="https://checkout.stripe.example/pay">
  <label for="email">Email</label><input id="email" name="email" type="email" value="a@b.c">
  <label for="cardnumber">Card number</label>
  <input id="cardnumber" name="cardnumber" type="text" autocomplete="cc-number">
  <label for="cvc">Security code</label>
  <input id="cvc" name="cvc" type="text" autocomplete="cc-csc">
  <p>Total: $412.00</p>
  <button id="pay" type="submit">Pay $412.00</button>
</form>
<form id="searchform" method="get" action="/search">
  <label for="q">Search products</label><input id="q" name="q" type="search">
  <button id="go" type="submit">Search</button>
</form>
<button id="more">Show more details</button>
<a id="offsite" href="https://evil-checkout.example.net/go">Continue</a>
<button id="nameless">   </button>
<script>
  document.getElementById('more').addEventListener('click', function () {
    var p = document.createElement('p');
    p.id = 'expanded';
    p.textContent = 'Extra detail about the order.';
    document.body.appendChild(p);
  });
</script>
</body></html>
"""

ARTICLE_HTML = """<!doctype html>
<html><head><title>A nice lamp</title></head><body>
<nav>Home Products Basket Account</nav>
<header>Shop chrome that should not be read aloud</header>
<main>
  <h1>A nice lamp</h1>
  <p>The lamp is brass. The lamp is nice.</p>
  <p>Card 4012 8888 8888 1881 should never come back.</p>
</main>
<footer>Cookie banner, terms, privacy, careers</footer>
</body></html>
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        return


@pytest.fixture(scope="module")
def fixture_site(tmp_path_factory):
    """Static HTML on loopback. Nothing here reaches the internet."""
    root = tmp_path_factory.mktemp("site")
    (root / "checkout.html").write_text(CHECKOUT_HTML, encoding="utf-8")
    (root / "article.html").write_text(ARTICLE_HTML, encoding="utf-8")

    handler = type("Bound", (_Handler,), {"directory": str(root)})

    def factory(*args, **kwargs):
        return handler(*args, directory=str(root), **kwargs)

    server = socketserver.TCPServer(("127.0.0.1", 0), factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    profile = tmp_path_factory.mktemp("profile")
    options = BrowserOptions(
        profile_dir=Path(profile),
        page_cache_dir=Path(profile) / "pages",
        headless=True,
        allow_private_hosts=True,
        summarize_private_pages=False,
        nav_timeout_ms=20_000,
        op_timeout_ms=5_000,
    )
    session = PlaywrightSession(options)
    degraded = session.ensure()
    if degraded is not None:
        pytest.skip(f"no usable browser: {degraded.detail} -- {degraded.remedy}")
    yield session, options
    session.close()


@pytest.fixture
def tools(browser):
    session, options = browser
    return {
        t.spec.name: t for t in make_browser_tools(LIVE, session=session, options=options)
    }


@pytest.fixture
def checkout(tools, fixture_site):
    tool = tools["open_tab"]
    result = tool.run(tool.resolve(url=f"{fixture_site}/checkout.html"))
    assert result.ok, result.error
    return result.data["tab_id"]


# ---------------------------------------------------------------------------
# the browser really is a separate, daa-owned profile
# ---------------------------------------------------------------------------


def test_the_profile_is_ours_and_is_locked_down(browser):
    import os

    session, options = browser
    assert session.started
    assert oct(os.stat(options.profile_dir).st_mode)[-3:] == "700"
    # Never the user's real Chrome data directory.
    assert "Application Support/Google/Chrome" not in str(options.profile_dir)


def test_nothing_opens_a_debugging_port(browser):
    """The flag is there; the door is not, and the difference is the point.

    Playwright passes `--remote-debugging-pipe`, which hands Chrome two
    inherited file descriptors. It binds nothing and nothing else on the
    machine can reach it. `--remote-debugging-port` would be a different
    animal entirely: its WebSocket URL is a bearer credential any local
    process can pick up, and daa would have been the thing that created it.
    """
    import subprocess

    _session, options = browser
    out = subprocess.run(
        ["/bin/ps", "-axo", "pid,command"], capture_output=True, text=True, check=False
    ).stdout
    ours = [line for line in out.splitlines() if str(options.profile_dir) in line]
    assert ours, "the browser process should be findable by its profile dir"
    for line in ours:
        assert "--remote-debugging-port" not in line
    assert any("--remote-debugging-pipe" in line for line in ours), (
        "if Playwright stops using a pipe, this test must be re-examined rather "
        "than deleted -- the replacement may well be a port"
    )

    pids = [line.split(None, 1)[0] for line in ours]
    listeners = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(pids), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert not listeners, f"the browser is listening on a socket: {listeners}"


# ---------------------------------------------------------------------------
# the facts come out of Chrome the way the fakes assume
# ---------------------------------------------------------------------------


def test_the_accessible_name_is_computed_by_chrome(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    raw = page.element_facts("#pay")
    assert len(raw) == 1
    assert raw[0]["name"] == "Pay $412.00"
    assert raw[0]["role"] == "button"
    assert raw[0]["submits"] is True


def test_the_form_facts_are_names_and_types_and_never_values(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    raw = page.element_facts("#pay")[0]
    assert raw["form"]["method"] == "post"
    assert raw["form"]["action"] == "https://checkout.stripe.example/pay"
    assert raw["form"]["field_names"] == ["email", "cardnumber", "cvc", ""]
    assert raw["form"]["autocomplete_hints"] == ["", "cc-number", "cc-csc", ""]
    # The email field has a value in the fixture. It is nowhere in the record.
    assert "a@b.c" not in str(raw)


def test_a_prefilled_field_reports_emptiness_and_not_its_contents(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    assert page.element_facts("#email")[0]["value_empty"] is False
    assert page.element_facts("#cvc")[0]["value_empty"] is True
    assert "a@b.c" not in str(page.element_facts("#email")[0])


def test_an_unnamed_button_really_has_no_name(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    assert page.element_facts("#nameless")[0]["name"] == ""


def test_the_currency_amount_is_found_inside_the_form(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    assert page.element_facts("#pay")[0]["nearby_amount"] == "$412.00"


def test_the_fake_and_the_real_adapter_agree_on_the_schema(tools, checkout):
    from test_browser_fakes import elem

    page = tools["read_page"].page_for(checkout)
    real = page.element_facts("#pay")[0]
    _, fake = elem("#pay")
    assert set(fake) <= set(real)


# ---------------------------------------------------------------------------
# the readback, end to end
# ---------------------------------------------------------------------------


def test_the_real_page_produces_the_sentence_the_design_promised(tools, checkout):
    action = tools["submit_form"].resolve(text="Pay $412.00", tab_id=checkout)
    spoken = action.describe()
    assert spoken.startswith("submit the payment form on 127.0.0.1")
    assert "by pressing 'Pay $412.00'" in spoken
    assert "sending 4 fields to checkout.stripe.example" in spoken
    assert "including a card number" in spoken
    assert "for $412.00" in spoken
    assert "I will not be able to undo this" in spoken
    assert action.floor_hint is RiskTier.CONFIRM_VISUAL


def test_clicking_the_real_pay_button_is_refused_and_routed(tools, checkout):
    tool = tools["click_element"]
    action = tool.resolve(text="Pay $412.00", tab_id=checkout)
    assert ROUTE_TO_SUBMIT in action.describe()
    result = tool.run(action)
    assert result.ok is False and result.data["route_to"] == "submit_form"


def test_a_real_click_on_a_harmless_button_works(tools, checkout):
    tool = tools["click_element"]
    result = tool.run(tool.resolve(text="Show more details", tab_id=checkout))
    assert result.ok, result.error
    page = tool.page_for(checkout)
    assert page.element_facts("#expanded")


def test_the_real_cross_origin_link_names_the_destination(tools, checkout):
    action = tools["click_element"].resolve(text="Continue", tab_id=checkout)
    assert action.describe().startswith("leave this site and open")
    assert "evil-checkout.example.net" in action.describe()


def test_the_unnamed_button_escalates_instead_of_being_described(tools, checkout):
    action = tools["click_element"].resolve(selector="#nameless", tab_id=checkout)
    assert "unlabelled" in action.describe()
    assert action.floor_hint is RiskTier.CONFIRM_VISUAL


# ---------------------------------------------------------------------------
# the fingerprint, against a page that really moves
# ---------------------------------------------------------------------------


def test_the_fingerprint_survives_a_reread_of_an_unchanged_page(tools, checkout):
    page = tools["read_page"].page_for(checkout)
    first = factlib.dom_fingerprint(
        factlib.facts_from_raw(page.element_facts("#more")[0], page_url=page.url())
    )
    second = factlib.dom_fingerprint(
        factlib.facts_from_raw(page.element_facts("#more")[0], page_url=page.url())
    )
    assert first == second


def test_a_real_dom_swap_between_resolve_and_run_aborts(tools, checkout):
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details", tab_id=checkout)
    page = tool.page_for(checkout)
    # Exactly what an SPA does while the readback is being spoken.
    page._page.evaluate(
        "() => { document.getElementById('more').textContent = 'Delete my account'; }"
    )
    result = tool.run(action)
    assert result.ok is False
    assert result.summary == STALE_SUMMARY


def test_real_navigation_between_resolve_and_run_aborts(tools, checkout, fixture_site):
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details", tab_id=checkout)
    page = tool.page_for(checkout)
    page.goto(f"{fixture_site}/article.html")
    assert tool.run(action).ok is False
    page.goto(f"{fixture_site}/checkout.html")


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_reading_a_real_page_drops_the_chrome_and_redacts_the_shapes(
    tools, fixture_site
):
    opened = tools["open_tab"].run(
        tools["open_tab"].resolve(url=f"{fixture_site}/article.html")
    )
    assert opened.ok
    tool = tools["read_page"]
    result = tool.run(tool.resolve(tab_id=opened.data["tab_id"]))
    assert result.ok
    text = result.data["text"]
    assert "The lamp is brass." in text
    assert "Cookie banner" not in text
    assert "Home Products Basket" not in text
    assert "4012 8888 8888 1881" not in text
    assert "<redacted card number>" in text
    assert result.data["url"] == f"{fixture_site}/article.html"


def test_finding_on_a_real_page_counts_without_reading_it_out(tools, fixture_site):
    opened = tools["open_tab"].run(
        tools["open_tab"].resolve(url=f"{fixture_site}/article.html")
    )
    tool = tools["find_on_page"]
    result = tool.run(tool.resolve(text="lamp", tab_id=opened.data["tab_id"]))
    assert result.ok and result.data["count"] >= 2
    assert "brass" not in result.summary


def test_a_real_fill_and_its_undo(tools, checkout):
    tool = tools["fill_field"]
    result = tool.run(tool.resolve(text="Search products", value="lamp", tab_id=checkout))
    assert result.ok, result.error
    page = tool.page_for(checkout)
    assert page.element_facts("#q")[0]["value_empty"] is False
    assert "lamp" not in str(result.undo.args)
    replay = tool.run(tool.resolve(**dict(result.undo.args)))
    assert replay.ok
    assert page.element_facts("#q")[0]["value_empty"] is True


def test_a_real_card_field_is_refused(tools, checkout):
    tool = tools["fill_field"]
    action = tool.resolve(selector="#cardnumber", value="4012888888881881", tab_id=checkout)
    assert "never type into one" in action.describe()
    result = tool.run(action)
    assert result.ok is False
    page = tool.page_for(checkout)
    assert page.element_facts("#cardnumber")[0]["value_empty"] is True


def test_the_tab_list_never_carries_a_query_string(tools, fixture_site):
    opened = tools["open_tab"].run(
        tools["open_tab"].resolve(url=f"{fixture_site}/article.html?token=SECRETVALUE")
    )
    assert opened.ok
    rows = tools["list_tabs"].run(tools["list_tabs"].resolve()).data["tabs"]
    assert any(r["url"] == f"{fixture_site}/article.html" for r in rows)
    assert "SECRETVALUE" not in str(rows)
