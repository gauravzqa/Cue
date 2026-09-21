"""The browser daa owns, and nobody else's.

daa drives a dedicated, persistent, daa-owned Chrome profile at
`~/.daa/browser-profile`, launched with
`launch_persistent_context(channel="chrome")`. Not the user's real Chrome, and
not a throwaway clean profile. The user logs in, by hand, once, to the specific
sites they want daa to be able to reach; that enrolled set IS the grant. It is
enumerable, it is what the confirmation reads back ("on amazon.co.uk, where you
are signed in"), and revoking it is `rm -rf` on one directory.

Why not the user's real Chrome: Chrome has ignored `--remote-debugging-port` on
the default user data directory since 136 and still does in 153, and it fails
**silently** -- Chrome starts, the client connects to nothing, the page never
loads. The sanctioned replacement (`chrome://inspect#remote-debugging`) grants
"full control over this Chrome session ... saved data, cookies and site data
... navigate to any URL", in Chrome's own words, as a STANDING grant with no
origin scoping and no per-action consent. That is strictly larger than daa's
entire tool surface, granted in one step, and it is reachable by anything else
running as the user. It is out of scope here and belongs behind its own
`CONFIRM_VISUAL`-floored tool if it is ever built.

Why not a clean profile: "what did that email say", "is my order out for
delivery", "find that flight I was looking at" -- the entire reason a voice
assistant wants a browser rather than an HTTP client is session state. A clean
profile deletes the product.

Nothing here opens a port, and the distinction matters. A correction to the
research this was built from, made by looking at `ps` while this code was
running: Playwright DOES pass `--remote-debugging-pipe`. The plan said the
Chrome process carried no `--remote-debugging-*` flag at all, and that is
simply not true.

What IS true is the thing the claim was standing in for. `--remote-debugging-pipe`
hands Chrome two inherited file descriptors; it binds nothing, `lsof` shows no
listening socket for the process, and nothing else on the machine can reach it
-- unlike `--remote-debugging-port`, whose WebSocket URL is a bearer credential
any local process can pick up and drive the browser with. So daa is still not
the thing that opened an unauthenticated door. The flag is there; the door is
not. `tests/test_browser_live.py` asserts both halves of that against the real
process rather than taking either on trust.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daa.tools.base import Degraded
from daa.tools.browser import urls
from daa.tools.browser.page import (
    CONTROLS_JS,
    EXTRACT_JS,
    FACTS_JS,
    FIND_JS,
    SCROLL_JS,
    TabInfo,
    looks_signed_in,
)
from daa.tools.browser.privacy import parse_aria_line

DEFAULT_PROFILE_DIR = Path.home() / ".daa" / "browser-profile"
DEFAULT_PAGE_CACHE_DIR = Path.home() / ".daa" / "pages"

# hermes's shipped number, taken as-is: a real, load-bearing empirical cap from
# a product that ran into the problem first.
READ_PAGE_MAX_CHARS = 15_000


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    """Everything about the browser that is a decision rather than a fact.

    `allow_private_hosts` and `summarize_private_pages` are constructor-level
    and default to the refusing value. Neither is a tool parameter, so the
    model can never ask for either -- which is the difference between a test
    seam and a hole.
    """

    profile_dir: Path = DEFAULT_PROFILE_DIR
    page_cache_dir: Path = DEFAULT_PAGE_CACHE_DIR
    headless: bool = False
    # Loopback/RFC1918/CGNAT. Off means daa's browser cannot be used as an SSRF
    # pivot onto the router admin page or a dev server. The test suite turns it
    # on to serve static fixtures from 127.0.0.1.
    allow_private_hosts: bool = False
    # Summarising a logged-in page sends private content to a remote model.
    # Default off; see SummarisePage.
    summarize_private_pages: bool = False
    read_page_max_chars: int = READ_PAGE_MAX_CHARS
    page_cache_ttl_s: float = 24 * 3600
    # A browser undo row is a browsing-history entry sitting in a file
    # indefinitely, and there is no journal pruning. Browser undos expire.
    undo_ttl_s: float = 3600.0
    nav_timeout_ms: int = 20_000
    op_timeout_ms: int = 5_000


# ---------------------------------------------------------------------------
# Playwright-backed page
# ---------------------------------------------------------------------------


class PlaywrightPage:
    """One tab. Satisfies `page.PageHandle`."""

    def __init__(self, page: Any, tab_id: str, session: PlaywrightSession) -> None:
        self._page = page
        self._id = tab_id
        self._session = session
        self._dialog = ""
        # A dialog is the SITE asking the USER a question. Answering it on
        # their behalf is answering a confirmation they never heard, so the
        # policy is must-respond: record it, refuse to act while it is up, and
        # leave it on screen in the headed window for the person to answer.
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog: Any) -> None:
        self._dialog = str(getattr(dialog, "type", "") or "dialog")

    # -- identity --------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    def url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:  # noqa: BLE001 - a closed page is a missing page
            return ""

    def title(self) -> str:
        try:
            return self._page.title() or ""
        except Exception:  # noqa: BLE001
            return ""

    def pending_dialog(self) -> str:
        return self._dialog

    # -- reading ---------------------------------------------------------

    def _evaluate(self, script: str, arg: Any = None) -> Any:
        return self._page.evaluate(script, arg)

    def element_facts(self, selector: str, *, limit: int = 8) -> list[dict[str, Any]]:
        raw = self._evaluate(FACTS_JS, {"selector": selector, "limit": limit}) or []
        for record in raw:
            role, name = self._accessible(selector, int(record.get("index", 0)))
            if role:
                # The browser's own accessibility computation is authoritative,
                # INCLUDING when it says there is no name. A missing name is a
                # fact to report, not a gap to fill with a guess.
                record["role"] = role
                record["name"] = name
        return list(raw)

    def _accessible(self, selector: str, index: int) -> tuple[str, str]:
        """(role, name) from Chrome's own accessible-name computation, or ("","")."""
        try:
            locator = self._page.locator(selector).nth(index)
            snapshot = locator.aria_snapshot(timeout=self._session.options.op_timeout_ms)
        except Exception:  # noqa: BLE001 - hidden/detached elements cannot snapshot
            return "", ""
        return parse_aria_line(snapshot)

    def controls(self, *, limit: int = 60) -> list[dict[str, Any]]:
        return list(self._evaluate(CONTROLS_JS, {"limit": limit}) or [])

    def extract(self) -> dict[str, Any]:
        return dict(self._evaluate(EXTRACT_JS) or {})

    def find(self, needle: str, *, limit: int = 10) -> dict[str, Any]:
        return dict(self._evaluate(FIND_JS, {"needle": needle, "limit": limit}) or {})

    def cookies(self) -> list[Mapping[str, Any]]:
        """Cookie NAMES and flags for this page's origin. Values are dropped here."""
        try:
            raw = self._session.context.cookies(self.url())
        except Exception:  # noqa: BLE001
            return []
        return [
            {"name": c.get("name", ""), "httpOnly": c.get("httpOnly"), "secure": c.get("secure")}
            for c in raw
        ]

    def cookie_names(self) -> list[str]:
        return [str(c["name"]) for c in self.cookies()]

    def has_session(self) -> bool:
        return looks_signed_in(self.cookies())

    # -- acting ----------------------------------------------------------

    def scroll(self, direction: str, amount: str) -> dict[str, Any]:
        return dict(self._evaluate(SCROLL_JS, {"direction": direction, "amount": amount}) or {})

    def click(self, selector: str) -> None:
        self._page.locator(selector).first.click(timeout=self._session.options.op_timeout_ms)

    def fill(self, selector: str, value: str) -> None:
        self._page.locator(selector).first.fill(value, timeout=self._session.options.op_timeout_ms)

    def submit(self, selector: str) -> None:
        self._page.locator(selector).first.click(timeout=self._session.options.op_timeout_ms)
        # An XHR submit never changes the load state, which is not a failure.
        with contextlib.suppress(Exception):
            self._page.wait_for_load_state(
                "domcontentloaded", timeout=self._session.options.nav_timeout_ms
            )

    def goto(self, url: str) -> None:
        self._page.goto(url, timeout=self._session.options.nav_timeout_ms,
                        wait_until="domcontentloaded")

    def go_back(self) -> bool:
        response = self._page.go_back(timeout=self._session.options.nav_timeout_ms,
                                      wait_until="domcontentloaded")
        return response is not None

    def close(self) -> None:
        # A tab that is already gone is the state we wanted.
        with contextlib.suppress(Exception):
            self._page.close()


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class PlaywrightSession:
    """Owns the profile's lifetime. Constructing one starts nothing.

    Import-time construction must have no side effects: no Chrome, no profile
    directory, no TCC prompt. `ensure()` is the first thing that touches the
    machine, and it returns `Degraded` rather than raising, so a machine with
    no Playwright installed costs exactly these tools and not the voice loop.
    """

    def __init__(self, options: BrowserOptions | None = None) -> None:
        self.options = options or BrowserOptions()
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._context: Any = None
        self._pages: dict[str, PlaywrightPage] = {}
        self._next_id = 1
        self._degraded: Degraded | None = None

    # -- lifecycle -------------------------------------------------------

    @property
    def context(self) -> Any:
        return self._context

    @property
    def started(self) -> bool:
        return self._context is not None

    def ensure(self) -> Degraded | None:
        """Start the browser if it is not running. Returns why it could not."""
        with self._lock:
            if self._context is not None:
                return None
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return Degraded(
                    capability="browser",
                    remedy="install the browser extra and I can use a web browser",
                    detail="playwright is not installed",
                )
            try:
                profile = self._prepare_profile()
            except OSError as exc:
                return Degraded(
                    capability="browser",
                    remedy="check that I can write to your home folder",
                    detail=f"could not prepare the browser profile: {exc}",
                )
            try:
                self._playwright = sync_playwright().start()
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel="chrome",
                    headless=self.options.headless,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
            except Exception as exc:  # noqa: BLE001 - every failure here is a Degraded
                self._shutdown_playwright()
                return self._launch_degraded(exc)
            self._context.set_default_timeout(self.options.op_timeout_ms)
            for page in list(self._context.pages):
                self._adopt(page)
            return None

    def _prepare_profile(self) -> Path:
        profile = Path(self.options.profile_dir).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        # The profile holds the user's enrolled sessions. 0700, on creation and
        # on every start, so a profile made by an older build gets fixed rather
        # than deprecated. Only this directory: an earlier version also
        # chmod'd the PARENT, which is right for `~/.daa` and wrong for every
        # other location -- it changed the mode of a directory this package
        # does not own, and on a temp dir it failed outright.
        os.chmod(profile, 0o700)
        return profile

    def _launch_degraded(self, exc: Exception) -> Degraded:
        text = str(exc)
        if "ProcessSingleton" in text or "SingletonLock" in text or "already" in text.lower():
            return Degraded(
                capability="browser",
                remedy="close the browser window I opened earlier and ask me again",
                detail="the browser profile is already in use",
            )
        if "channel" in text or "executable" in text.lower() or "not found" in text.lower():
            return Degraded(
                capability="browser",
                remedy="install Google Chrome and I can browse for you",
                detail="Chrome could not be launched",
            )
        return Degraded(
            capability="browser",
            remedy="try again in a moment, or restart me if it keeps happening",
            # The exception text can carry a profile path or a websocket URL.
            # Neither belongs in a log, so only the exception TYPE is kept.
            detail=f"the browser would not start ({type(exc).__name__})",
        )

    def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
        self._playwright = None
        self._context = None

    def close(self) -> None:
        with self._lock:
            if self._context is not None:
                with contextlib.suppress(Exception):
                    self._context.close()
            self._pages.clear()
            self._shutdown_playwright()

    # -- tabs ------------------------------------------------------------

    def _adopt(self, page: Any) -> PlaywrightPage:
        for handle in self._pages.values():
            if handle._page is page:
                return handle
        tab_id = f"t{self._next_id}"
        self._next_id += 1
        handle = PlaywrightPage(page, tab_id, self)
        self._pages[tab_id] = handle
        return handle

    def _prune(self) -> None:
        live = set(self._context.pages) if self._context is not None else set()
        for tab_id, handle in list(self._pages.items()):
            if handle._page not in live:
                self._pages.pop(tab_id, None)

    def tabs(self) -> list[TabInfo]:
        if self._context is None:
            return []
        with self._lock:
            for page in list(self._context.pages):
                self._adopt(page)
            self._prune()
            out: list[TabInfo] = []
            for index, (tab_id, handle) in enumerate(self._pages.items()):
                raw = handle.url()
                out.append(
                    TabInfo(
                        id=tab_id,
                        title=handle.title(),
                        # Normalised HERE, at the boundary, so no caller can
                        # accidentally carry a query string onward.
                        safe_url=urls.log_url(raw),
                        host=urls.speakable_host(raw),
                        active=index == 0,
                    )
                )
            return out

    def page(self, tab_id: str | None = None) -> PlaywrightPage | None:
        if self._context is None:
            return None
        with self._lock:
            for page in list(self._context.pages):
                self._adopt(page)
            self._prune()
            if tab_id:
                return self._pages.get(str(tab_id))
            return next(iter(self._pages.values()), None)

    def open_tab(self, url: str) -> PlaywrightPage:
        """Navigate a NEW tab. The URL guard runs one layer up, in the tool."""
        if self._context is None:
            raise RuntimeError("browser is not started")
        with self._lock:
            page = self._context.new_page()
            handle = self._adopt(page)
        handle.goto(url)
        return handle

    def close_tab(self, tab_id: str) -> bool:
        with self._lock:
            handle = self._pages.pop(str(tab_id), None)
        if handle is None:
            return False
        handle.close()
        return True


# ---------------------------------------------------------------------------
# The process-wide session
# ---------------------------------------------------------------------------

_SESSION_LOCK = threading.Lock()
_SESSION: PlaywrightSession | None = None


def get_session(options: BrowserOptions | None = None) -> PlaywrightSession:
    """One browser per process. Constructed lazily, started later still."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = PlaywrightSession(options)
        return _SESSION


def reset_session() -> None:
    """Drop the process-wide session. For tests and for `daa browser stop`."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
        _SESSION = None


@dataclass(frozen=True, slots=True)
class PageCache:
    """Overflow for page text that will not fit in a spoken reply.

    0600, content-addressed, with a TTL. The PATH is never written to the audit
    log: a filename like `browser-page-<hash>.md` next to a timestamp is a
    browsing history, which is precisely the thing the rest of this package
    refuses to keep.
    """

    directory: Path
    ttl_s: float = 24 * 3600
    _extra: Mapping[str, Any] = field(default_factory=dict)

    def store(self, text: str) -> str:
        import hashlib
        import time

        directory = Path(self.directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.sweep()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        path = directory / f"{digest}.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        os.utime(path, (time.time(), time.time()))
        return str(path)

    def sweep(self) -> int:
        import time

        directory = Path(self.directory).expanduser()
        if not directory.is_dir():
            return 0
        cutoff = time.time() - self.ttl_s
        dropped = 0
        for path in directory.glob("*.md"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    dropped += 1
            except OSError:
                continue
        return dropped


def profile_sites(session: PlaywrightSession) -> Sequence[str]:
    """The enrolled set, as registrable domains. Cookie NAMES only, no values.

    This is the enumerable grant: what `daa browser sites` prints, and what
    makes "where you are signed in" an honest clause rather than a guess.
    """
    if session.context is None:
        return ()
    try:
        raw = session.context.cookies()
    except Exception:  # noqa: BLE001
        return ()
    found: set[str] = set()
    by_domain: dict[str, list[Mapping[str, Any]]] = {}
    for cookie in raw:
        domain = str(cookie.get("domain") or "").lstrip(".")
        if not domain:
            continue
        by_domain.setdefault(urls.etld1(domain), []).append(
            {"name": cookie.get("name", ""), "httpOnly": cookie.get("httpOnly"),
             "secure": cookie.get("secure")}
        )
    for domain, cookies in by_domain.items():
        if looks_signed_in(cookies):
            found.add(domain)
    return tuple(sorted(found))


__all__ = [
    "DEFAULT_PAGE_CACHE_DIR",
    "DEFAULT_PROFILE_DIR",
    "READ_PAGE_MAX_CHARS",
    "BrowserOptions",
    "PageCache",
    "PlaywrightPage",
    "PlaywrightSession",
    "get_session",
    "profile_sites",
    "reset_session",
]
