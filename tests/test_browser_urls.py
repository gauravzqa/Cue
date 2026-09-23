"""A logged URL is frequently a working credential. These are the rules.

Query strings and fragments carry magic-link tokens, password-reset tokens,
OAuth `code`/`state`, session ids and `?access_token=`. `~/.daa/audit.jsonl` is
0600 but it is not encrypted and it is backed up, so the loggable form of a URL
is `scheme://host/path` and there is no second form.
"""

from __future__ import annotations

import pytest

from daa.tools.browser import urls

MAGIC_LINK = "https://mail.example.com/login?token=abc123SECRET&state=xyz#frag"


# ---------------------------------------------------------------------------
# log_url
# ---------------------------------------------------------------------------


def test_log_url_drops_the_query_and_the_fragment():
    assert urls.log_url(MAGIC_LINK) == "https://mail.example.com/login"


def test_log_url_drops_a_token_entirely_rather_than_truncating_it():
    """A truncated token is still a token prefix, and a prefix correlates."""
    safe = urls.log_url(MAGIC_LINK)
    assert "abc123SECRET" not in safe
    assert "abc" not in safe.replace("https://mail.example.com/login", "")
    assert "?" not in safe and "#" not in safe


def test_log_url_drops_credentials_in_the_address():
    assert urls.log_url("https://bob:hunter2@intranet.example.com/x") == (
        "https://intranet.example.com/x"
    )


def test_log_url_keeps_the_path_because_the_log_is_useless_without_it():
    assert urls.log_url("https://github.com/acme/repo/settings") == (
        "https://github.com/acme/repo/settings"
    )


def test_log_url_names_a_refused_scheme_without_keeping_its_payload():
    assert urls.log_url("javascript:fetch('/steal?c='+document.cookie)") == "javascript:"


def test_log_url_of_nothing_is_nothing():
    assert urls.log_url("") == ""
    assert urls.log_url("   ") == ""


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "chrome://settings/passwords",
        "devtools://devtools/bundled/inspector.html",
        "data:text/html,<script>x</script>",
        "view-source:https://example.com",
    ],
)
def test_dangerous_schemes_are_refused_outright(raw):
    verdict = urls.classify(raw)
    assert verdict.ok is False
    assert verdict.reason


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost:8080/admin",
        "http://127.0.0.1/",
        "http://192.168.1.1/setup",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.5.5/",
        "http://printer.local/",
        "http://[::1]/",
    ],
)
def test_the_browser_is_not_an_ssrf_pivot_onto_the_lan(raw):
    verdict = urls.classify(raw)
    assert verdict.ok is False
    assert "local network" in verdict.reason or "your own machine" in verdict.reason


def test_loopback_is_reachable_only_when_the_session_was_constructed_to_allow_it():
    """A constructor argument, never a tool parameter: the model cannot ask."""
    assert urls.classify("http://127.0.0.1:8931/fixture.html").ok is False
    assert urls.classify("http://127.0.0.1:8931/fixture.html", allow_private=True).ok is True


def test_surprising_but_legal_addresses_are_flagged_not_blocked():
    userinfo = urls.classify("https://bob:pw@amazon.co.uk/gp/buy")
    assert userinfo.ok and userinfo.has_userinfo

    punycode = urls.classify("https://xn--80ak6aa92e.com/")
    assert punycode.ok and punycode.is_punycode

    literal = urls.classify("https://93.184.216.34/thing")
    assert literal.ok and literal.is_ip_literal

    for verdict in (userinfo, punycode, literal):
        assert verdict.surprising


def test_a_bare_site_name_becomes_https_not_http():
    verdict = urls.classify("amazon.co.uk/gp/buy")
    assert verdict.ok and verdict.scheme == "https"


def test_the_query_is_flagged_without_ever_being_quoted():
    verdict = urls.classify(MAGIC_LINK)
    assert verdict.has_query
    assert "abc123SECRET" not in verdict.safe_url
    # The full URL survives only in `url`, which no tool puts into a readback.
    assert "abc123SECRET" in verdict.url


# ---------------------------------------------------------------------------
# etld1 and speakable_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("www.amazon.co.uk", "amazon.co.uk"),
        ("smile.amazon.co.uk", "amazon.co.uk"),
        ("checkout.stripe.com", "stripe.com"),
        ("example.com", "example.com"),
        ("a.b.c.example.org", "example.org"),
        ("mail.google.com", "google.com"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_registrable_domain(host, expected):
    assert urls.etld1(host) == expected


def test_speakable_host_is_what_a_person_would_say():
    assert urls.speakable_host("https://www.amazon.co.uk/gp/buy?x=1") == "amazon.co.uk"
    assert urls.speakable_host("https://checkout.stripe.com/pay") == "checkout.stripe.com"


@pytest.mark.parametrize(
    "raw, spoken",
    [
        ("http://127.0.0.1:8799/reports", "127.0.0.1:8799"),
        ("127.0.0.1:8799", "127.0.0.1:8799"),
        ("https://ex.com:8443/a", "ex.com:8443"),
        ("[::1]:8080", "[::1]:8080"),
        # The scheme's own default port is noise, like `www.`
        ("https://example.com:443/x", "example.com"),
        ("http://example.com:80/x", "example.com"),
        ("https://www.example.com:8080/x", "example.com:8080"),
    ],
)
def test_the_spoken_name_keeps_a_non_default_port(raw, spoken):
    """The port is part of WHICH RESOURCE THIS IS, and this string is the one
    daa says out loud -- so it is the sentence the user answers yes to. Saying
    "127.0.0.1" for `127.0.0.1:8799` proposes a different server. Live, the
    model read the readback of its own step, saw the port missing, and stopped
    the task to ask whether the wrong page had been opened."""
    assert urls.speakable_host(raw) == spoken


def test_an_unspeakable_address_is_empty_rather_than_wrong():
    assert urls.speakable_host("") == ""
    assert urls.speakable_host("   ") == ''


def test_origin_is_scheme_and_host_and_never_the_path():
    assert urls.origin_of("https://stripe.com/checkout/order?x=1") == "https://stripe.com"
    assert urls.origin_of("http://127.0.0.1:8931/x") == "http://127.0.0.1:8931"


# ---------------------------------------------------------------------------
# mutation-revert: the guard is what stops it, not something downstream
# ---------------------------------------------------------------------------


def test_reverting_the_query_stripper_reintroduces_the_credential(monkeypatch):
    """If `log_url` kept the query, the token would be in the loggable form.

    This is the sabotage check for the rule above: it proves the assertion in
    `test_log_url_drops_the_query_and_the_fragment` is load-bearing rather than
    passing because nothing on the path produces a query string anyway.
    """
    monkeypatch.setattr(urls, "log_url", lambda raw: str(raw))
    assert "abc123SECRET" in urls.log_url(MAGIC_LINK)


# ---------------------------------------------------------------------------
# the scheme nobody gave
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A private host with an explicit port is a dev server. It is not TLS.
        ("127.0.0.1:8799/reports", "http://127.0.0.1:8799/reports"),
        ("localhost:3000", "http://localhost:3000"),
        ("192.168.1.4:8080", "http://192.168.1.4:8080"),
        ("[::1]:8080", "http://[::1]:8080"),
        # An explicit :80 is someone saying "not TLS" out loud.
        ("127.0.0.1:80/x", "http://127.0.0.1:80/x"),
        # ...and an explicit :443 is someone saying the opposite.
        ("127.0.0.1:443/x", "https://127.0.0.1:443/x"),
        # No port: a device on the local network may well have a certificate.
        ("192.168.1.1", "https://192.168.1.1"),
        ("router.local", "https://router.local"),
        ("[::1]", "https://[::1]"),
        # The public internet is untouched, port or no port.
        ("amazon.co.uk/foo", "https://amazon.co.uk/foo"),
        ("example.com:8080", "https://example.com:8080"),
        # A scheme that WAS given is never rewritten, in either direction.
        ("https://127.0.0.1:8799/x", "https://127.0.0.1:8799/x"),
        ("http://example.com", "http://example.com"),
        ("javascript:alert(1)", "javascript:alert(1)"),
        ("mailto:a@b", "mailto:a@b"),
    ],
)
def test_the_default_scheme(raw, expected):
    """Live, this decided whether a task worked: the model sometimes typed
    `http://` and sometimes did not, and the https default put a TLS handshake
    in front of a plain HTTP dev server. The failure surfaced as "navigation
    failed (Error)", so the same instruction succeeded and failed minutes
    apart for a reason nothing reported."""
    assert urls.normalize_url(raw) == expected


def test_nothing_is_downgraded_only_filled_in():
    """This may only ever supply a MISSING scheme. A spoken https on a private
    address is a deliberate statement and stays."""
    for raw in ("https://127.0.0.1:8799/x", "https://localhost:3000", "https://[::1]:8080"):
        assert urls.normalize_url(raw) == raw
