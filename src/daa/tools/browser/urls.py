"""URLs, treated as credentials until proven otherwise.

Two jobs live here, and both are safety properties rather than conveniences.

**Nothing logs a query string.** A URL's query and fragment routinely carry
magic-link tokens, password-reset tokens, OAuth `code`/`state`, session ids and
`?access_token=`. A logged URL is frequently a *working credential*, and
`~/.daa/audit.jsonl` is 0600 but it is not encrypted and it is backed up. So
`log_url()` produces `scheme://host[:port]/path` and nothing else -- the port
because it says *which* server, never a secret -- and it is the ONLY
way a URL is allowed to reach `ResolvedAction.targets`, a `ToolResult.summary`,
an `UndoAction`, or an `AuditEvent`. The full URL stays in `ToolResult.data`,
which the audit builders in `safety/audit.py` never read.

**Nothing navigates somewhere the user could not have meant.** `javascript:`
is code execution, `file:` is the disk, `chrome://` is the browser's own
settings, and a private/loopback/CGNAT host makes daa's browser an SSRF pivot
onto the user's router admin page, their dev servers and 169.254.169.254.
Those are refused; anything merely *surprising* (a different site, an IP
literal, punycode, userinfo) is escalated and spoken rather than blocked,
because the honest response to "this leaves amazon.co.uk" is to say so.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# The only two schemes a page may be fetched over.
SAFE_SCHEMES = frozenset({"http", "https"})

# Refused outright, with the reason spoken. Not escalated -- there is no tier
# at which "run this JavaScript in a logged-in origin" is a thing a voice
# assistant should offer to do.
REFUSED_SCHEMES: dict[str, str] = {
    "javascript": "that is a piece of code, not a web address",
    "data": "that is an inline document, not a web address",
    "blob": "that is an inline document, not a web address",
    "file": "that would open a file off your disk in the browser",
    "chrome": "that is one of Chrome's own settings pages",
    "chrome-extension": "that is a browser extension page",
    "chrome-search": "that is one of Chrome's own pages",
    "devtools": "that is the browser's developer tools",
    "view-source": "that would show a page's source instead of the page",
    "ws": "that is a socket address, not a page",
    "wss": "that is a socket address, not a page",
    "mailto": "that is an email address, not a page",
    "tel": "that is a phone number, not a page",
}

# Suffixes that never leave the machine or the local network.
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".lan")

# Two-label public suffixes, enough for the sites a person actually names out
# loud. LIMIT, stated rather than hidden: this is not the Public Suffix List.
# A miss makes `etld1()` return one label too few, which can only ever make
# `is_cross_origin` MORE likely to fire -- i.e. it fails toward saying
# "this leaves the site" when it did not, never toward silence.
_MULTI_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "net.nz", "org.nz", "govt.nz",
        "com.br", "net.br", "org.br", "gov.br",
        "co.in", "net.in", "org.in", "gov.in", "ac.in",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
        "com.mx", "com.ar", "com.sg", "com.hk", "com.tw", "com.tr", "com.pl",
        "co.za", "org.za", "co.kr", "or.kr", "com.es", "com.ua",
    }
)


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    """What deterministic inspection of a URL string found. No DNS, no fetch."""

    ok: bool
    url: str = ""            # normalised, full (kept OUT of every log)
    safe_url: str = ""       # scheme://host[:port]/path -- the loggable form
    scheme: str = ""
    host: str = ""
    etld1: str = ""
    path: str = "/"
    reason: str = ""         # spoken, when ok is False
    # Escalation signals. None of these blocks; each one is said out loud.
    has_userinfo: bool = False
    is_ip_literal: bool = False
    is_punycode: bool = False
    is_private: bool = False
    has_query: bool = False

    @property
    def surprising(self) -> bool:
        return self.has_userinfo or self.is_ip_literal or self.is_punycode


def normalize_url(raw: str) -> str:
    """'amazon.co.uk/foo' -> 'https://amazon.co.uk/foo'. Scheme-preserving.

    A bare host spoken aloud is the common case, and defaulting it to https
    rather than http means the fallback is the encrypted one.

    WITH ONE EXCEPTION: a PRIVATE host carrying an EXPLICIT non-default port,
    such as `127.0.0.1:8799/reports` or `192.168.1.4:8080`, defaults to http.
    Nothing on the public internet is reached this way, nothing is downgraded
    -- a spoken `https://` is still honoured, this only fills in a scheme
    nobody gave -- and a dev server on a loopback port is essentially never
    TLS. Defaulting it to https produced a TLS handshake against a plain HTTP
    server and the unhelpful failure "navigation failed (Error)". It was
    intermittent in the worst way: whether the task worked depended on whether
    the model happened to type `http://`, so the same instruction succeeded
    and failed minutes apart.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        head = text.split("/", 1)[0].split("?", 1)[0]
        # "javascript:alert(1)" and "mailto:x@y" have a scheme but no "://".
        # `[::1]:8080` has colons everywhere and is a HOST: checked first, or
        # it reads as the scheme "[" and is handed back unchanged -- with no
        # scheme at all, so `classify` then refuses it as "not a web page".
        if not head.startswith("[") and ":" in head and not head.split(":", 1)[1].isdigit():
            return text
        text = ("http://" if _bare_private_with_port(head) else "https://") + text
    return text


def _explicit_port(authority: str) -> str:
    """The port written in an authority, or "". Handles `[::1]:8080`."""
    tail = str(authority or "").rsplit("@", 1)[-1]
    if tail.startswith("["):
        _, _, after = tail.partition("]")
        tail = after
    part = tail.rsplit(":", 1)
    return part[1] if len(part) == 2 and part[1].isdigit() else ""


def _bare_private_with_port(authority: str) -> bool:
    """A scheme-less private host with an explicit port that is not 443.

    Deliberately narrow. A private host WITHOUT a port keeps the https default
    -- `router.local` or a bare `192.168.1.1` is a device someone may well have
    put a certificate on -- and a public host is never affected at all. An
    explicit `:443` is someone saying TLS, so it is left alone; an explicit
    `:80` is someone saying the opposite, which is why this asks for the port
    itself rather than reusing `port_suffix` (which reports ":80" as absent,
    being http's default, and would have sent `127.0.0.1:80` to https).
    """
    host, _ = split_host(authority)
    if not host or not is_private_host(host):
        return False
    port = _explicit_port(authority)
    return bool(port) and port != "443"


def split_host(netloc: str) -> tuple[str, bool]:
    """(hostname, had_userinfo). Port and credentials are dropped."""
    had_userinfo = "@" in netloc
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):                      # [::1]:8080
        host = host.split("]", 1)[0].lstrip("[")
    else:
        host = host.split(":", 1)[0]
    return host.lower().rstrip("."), had_userinfo


def port_suffix(netloc: str, scheme: str) -> str:
    """':8931', or '' when the port is absent or is the scheme's default.

    The port is part of *which resource this is*, and it is not a secret: a
    port is not a token, and no amount of knowing one gets anybody into an
    account. So it survives into the loggable form, while the query, the
    fragment and the userinfo do not. Dropping it was a real bug rather than a
    cosmetic one -- `http://127.0.0.1:8931/x` logged as `http://127.0.0.1/x`
    is an address the user never visited, and `close_tab`'s undo reopens the
    loggable form, so the undo went to port 80 and got somebody else's server.
    """
    authority = str(netloc or "").rsplit("@", 1)[-1]
    if authority.endswith("]"):                   # [::1] -- brackets, no port
        return ""
    tail = authority.rsplit(":", 1)
    if len(tail) != 2 or not tail[1].isdigit():
        return ""
    default = "443" if scheme == "https" else "80" if scheme == "http" else ""
    return "" if tail[1] == default else f":{tail[1]}"


def _authority(host: str, port: str) -> str:
    """host[:port], with an IPv6 literal re-bracketed so the URL reparses."""
    return f"[{host}]{port}" if ":" in host else f"{host}{port}"


def host_of(raw: str) -> str:
    """Hostname of a URL, lowercased, no port and no credentials. "" if absent."""
    text = normalize_url(str(raw or "").strip())
    if not text:
        return ""
    return split_host(urlsplit(text).netloc)[0]


def etld1(host: str) -> str:
    """Registrable domain, on a small table. See `_MULTI_SUFFIXES` for the limit."""
    host = (host or "").lower().strip().rstrip(".")
    if not host or _is_ip_literal(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_private_host(host: str) -> bool:
    """Loopback, RFC1918, link-local, CGNAT, and the local-network suffixes.

    LIMIT, stated: this inspects the STRING. A public name that resolves to
    127.0.0.1 is not caught here, because resolve() may not touch the network.
    """
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True                                # CGNAT, not private per RFC1918
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
    )


def log_url(raw: str) -> str:
    """scheme://host[:port]/path. The ONLY form allowed into a log or a readback.

    Query and fragment are dropped, not shortened: a truncated token is still a
    token prefix, and a prefix is enough to correlate a log line with a session.
    Userinfo is dropped for the same reason -- `https://user:pw@host/` in a log
    is a password in a log. A non-default PORT is kept; see `port_suffix`.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    parts = urlsplit(normalize_url(text))
    if parts.scheme and parts.scheme not in SAFE_SCHEMES:
        # Name the scheme, keep nothing else: "javascript:" is worth having in
        # a log, and the expression after it is code that may embed a cookie.
        return f"{parts.scheme}:"
    host, _ = split_host(parts.netloc)
    if not parts.scheme or not host:
        return ""
    path = parts.path or "/"
    authority = _authority(host, port_suffix(parts.netloc, parts.scheme))
    return urlunsplit((parts.scheme, authority, path, "", ""))


def speakable_host(raw_or_host: str) -> str:
    """'https://www.amazon.co.uk/gp/buy?x=1' -> 'amazon.co.uk'.

    What a person would call the site. `www.` is noise nobody says out loud,
    and the path is not part of the site's identity.

    A NON-DEFAULT PORT IS. It survives here for the same reason `port_suffix`
    keeps it in the loggable form, and the case for it is stronger: this string
    is what daa SAYS, so it is the sentence in "should I open 127.0.0.1?" that
    the user answers yes to. Speaking `127.0.0.1` for `127.0.0.1:8799` names a
    different server -- and it does not just mislead the user. Measured, on a
    live run: the model read its own readback back, saw the port it had asked
    for was missing, concluded the wrong page had been opened, and stopped to
    ask. A readback that does not name the resource is the one failure this
    codebase treats as unacceptable, whoever is reading it.
    """
    text = str(raw_or_host or "").strip()
    parts = urlsplit(normalize_url(text))
    netloc = parts.netloc if parts.netloc else text
    host = split_host(netloc)[0]
    if not host:
        return ""
    return _authority(host.removeprefix("www."), port_suffix(netloc, parts.scheme or "https"))


def origin_of(raw: str) -> str:
    """scheme://host -- `ResolvedAction.origin`, a machine key, never spoken."""
    parts = urlsplit(normalize_url(str(raw or "").strip()))
    host, _ = split_host(parts.netloc)
    if not parts.scheme or not host:
        return ""
    return f"{parts.scheme}://{_authority(host, port_suffix(parts.netloc, parts.scheme))}"


def classify(raw: str, *, allow_private: bool = False) -> UrlVerdict:
    """Everything deterministic inspection can say about a URL, in one record.

    `allow_private` exists so the test-suite can serve fixtures from 127.0.0.1.
    It is a constructor-level decision on the session, NOT a tool parameter:
    the model can never ask for it, which is the difference between a test seam
    and a hole.
    """
    text = normalize_url(raw)
    if not text:
        return UrlVerdict(ok=False, reason="I did not get a web address")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme in REFUSED_SCHEMES:
        return UrlVerdict(ok=False, scheme=scheme, reason=REFUSED_SCHEMES[scheme])
    if scheme not in SAFE_SCHEMES:
        return UrlVerdict(
            ok=False, scheme=scheme, reason="I only open ordinary web pages, and that is not one"
        )
    host, had_userinfo = split_host(parts.netloc)
    if not host:
        return UrlVerdict(ok=False, scheme=scheme, reason="that address has no site in it")
    private = is_private_host(host)
    if private and not allow_private:
        return UrlVerdict(
            ok=False,
            scheme=scheme,
            host=host,
            is_private=True,
            reason="that address is on your own machine or local network, which I do not open",
        )
    return UrlVerdict(
        ok=True,
        url=text,
        safe_url=log_url(text),
        scheme=scheme,
        host=host,
        etld1=etld1(host),
        path=parts.path or "/",
        has_userinfo=had_userinfo,
        is_ip_literal=_is_ip_literal(host),
        is_punycode="xn--" in host,
        is_private=private,
        has_query=bool(parts.query or parts.fragment),
    )


__all__ = [
    "REFUSED_SCHEMES",
    "SAFE_SCHEMES",
    "UrlVerdict",
    "classify",
    "etld1",
    "host_of",
    "is_private_host",
    "log_url",
    "normalize_url",
    "origin_of",
    "port_suffix",
    "speakable_host",
    "split_host",
]
