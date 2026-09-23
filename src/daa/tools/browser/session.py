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

import atexit
import contextlib
import os
import signal
import threading
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daa.tools.base import Degraded, run_argv
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

# How long `close()` is allowed to wait for Chrome to go quietly before it stops
# asking. `BrowserContext.close()` takes no timeout and is a *request* to a
# process that may be wedged, so "closed" has to be something this code can
# guarantee on its own: past this many seconds the browser is signalled instead.
CLOSE_TIMEOUT_S = 5.0
# After SIGTERM, how long Chrome gets to flush its profile before SIGKILL.
REAP_GRACE_S = 1.5


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
# Processes, owned by their profile directory
# ---------------------------------------------------------------------------
#
# A browser daa launched and did not close is daa's fault, not the user's, and
# it is not a cosmetic one: an orphaned Chrome holds the profile's
# `SingletonLock`, so the NEXT `ensure()` degrades with "the browser profile is
# already in use" and the tools stay broken until someone finds the process by
# hand. `close()` therefore ends with a guarantee rather than a request.
#
# Everything below is keyed on ONE directory: the `--user-data-dir` daa itself
# passed. The user's real Chrome runs on a different one, so nothing here can
# reach it even if daa's own profile path is misconfigured -- and a path that
# is empty, `/`, or the home directory is refused outright rather than matched
# loosely.


def _profile_flag(profile_dir: Path | str) -> str:
    return f"--user-data-dir={Path(profile_dir).expanduser()}"


def _is_reapable(profile_dir: Path | str) -> bool:
    """Is this a directory specific enough to match process command lines on?"""
    try:
        profile = Path(profile_dir).expanduser()
    except (TypeError, ValueError):
        return False
    text = str(profile)
    if not text or text in ("", ".", "/"):
        return False
    return profile != Path.home() and profile != profile.parent


def processes_using_profile(profile_dir: Path | str) -> list[int]:
    """PIDs whose command line names EXACTLY this `--user-data-dir`.

    The browser process and its GPU/network/renderer helpers all carry the
    flag, so this finds the whole family. The match is the flag plus the full
    path plus a word boundary: `--user-data-dir=/x/profile` must never match
    `/x/profile-2`, and a profile path that is not specific enough to match on
    (empty, `/`, `$HOME`) returns nothing rather than everything.
    """
    if not _is_reapable(profile_dir):
        return []
    flag = _profile_flag(profile_dir)
    # Through `tools/base.py::run_argv` like every other spawn in this package:
    # argv only, never a shell, always a timeout, and a missing `ps` comes back
    # as empty output instead of an exception. Read-only, so it still runs
    # under dry_run -- pretending not to look would not stop the leak.
    out = run_argv(["/bin/ps", "-axo", "pid=,command="], timeout=10).stdout
    found: list[int] = []
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        index = command.find(flag)
        if index < 0:
            continue
        tail = command[index + len(flag):]
        # A word boundary, so `.../profile` does not match `.../profile-2`.
        if tail and not tail[0].isspace():
            continue
        found.append(int(pid_text))
    return found


def _driver_pid(playwright: Any) -> int | None:
    """PID of Playwright's own node driver, if this build exposes it.

    Private attributes, deliberately guarded: it is a best-effort handle used
    only as the last step of a forced close, and a Playwright release that
    renames it must cost a slower shutdown, not an exception.
    """
    with contextlib.suppress(Exception):
        impl = getattr(playwright, "_impl_obj", playwright)
        proc = impl._connection._transport._proc
        pid = int(getattr(proc, "pid", 0) or 0)
        return pid or None
    return None


def _signal(pids: Sequence[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(OSError, ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def reap_profile_processes(
    profile_dir: Path | str, *, grace_s: float = REAP_GRACE_S
) -> list[int]:
    """Leave no process running on this profile. Returns the PIDs it killed.

    SIGTERM first so Chrome writes its profile out cleanly, SIGKILL after the
    grace period for whatever ignored it. Bounded by construction: one `ps`
    when nothing is running, `grace_s` plus a little when something is. The
    PID list is re-read from `ps` between the two signals, so a PID that was
    recycled in the meantime is simply not in it.
    """
    import time

    alive = processes_using_profile(profile_dir)
    if not alive:
        return []
    killed = list(alive)
    _signal(alive, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        time.sleep(0.05)
        alive = processes_using_profile(profile_dir)
        if not alive:
            return killed
    _signal(alive, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if not processes_using_profile(profile_dir):
            break
    return killed


# Every session that has actually started a browser. Weak, so a session that is
# garbage collected does not keep itself alive; the atexit hook exists for the
# interpreter that dies with one still open -- an unhandled exception, a
# crashed test, a `KeyboardInterrupt` in the voice loop.
_LIVE_SESSIONS: weakref.WeakSet[PlaywrightSession] = weakref.WeakSet()
_ATEXIT_REGISTERED = False


def _close_live_sessions() -> None:
    for session in list(_LIVE_SESSIONS):
        with contextlib.suppress(Exception):
            session.close(timeout_s=2.0)


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(_close_live_sessions)
        _ATEXIT_REGISTERED = True


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
        # The tab a tool means when the model does not name one. Tracked
        # rather than inferred: the pages dict is in ADOPTION order, whose
        # first entry is Playwright's initial about:blank, so "the active
        # one" was really "the oldest one".
        self._active: str | None = None
        self._next_id = 1
        self._degraded: Degraded | None = None
        # Set once a browser is actually running, and the only thing `close()`
        # needs in order to guarantee it is not running any more.
        self._profile: Path | None = None
        self._driver_pid: int | None = None

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
            # Recorded BEFORE the launch: if `launch_persistent_context` raises
            # half-way, a Chrome may already be running on this directory, and
            # the failure path has to be able to find it.
            self._profile = profile
            _register_atexit()
            _LIVE_SESSIONS.add(self)
            try:
                self._playwright = sync_playwright().start()
                self._driver_pid = _driver_pid(self._playwright)
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel="chrome",
                    headless=self.options.headless,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
            except Exception as exc:  # noqa: BLE001 - every failure here is a Degraded
                self._shutdown_playwright()
                # A launch that failed after Chrome started still leaves Chrome
                # holding the profile's SingletonLock, and the next ensure()
                # would degrade with "already in use" forever.
                reap_profile_processes(profile)
                self._profile = None
                self._driver_pid = None
                _LIVE_SESSIONS.discard(self)
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

    def close(self, *, timeout_s: float = CLOSE_TIMEOUT_S) -> None:
        """Stop the browser. Bounded, idempotent, and it leaves nothing behind.

        `BrowserContext.close()` is a polite request over a pipe to a process
        that may never answer -- it takes no timeout, and a hung Chrome hangs
        the caller forever. So the polite request is made with a deadline: a
        watchdog thread signals the browser processes running on THIS profile
        once `timeout_s` has passed (which also unblocks the call), the driver
        goes next if even that did not return, and the last thing `close()`
        does is check `ps` and kill whatever is somehow still there. "Closed"
        ends up meaning closed rather than asked-to-close.
        """
        with self._lock:
            profile = self._profile
            driver_pid = self._driver_pid
            done = threading.Event()
            watchdog: threading.Thread | None = None
            if profile is not None:
                watchdog = threading.Thread(
                    target=self._watch_close,
                    args=(done, profile, driver_pid, timeout_s),
                    name="daa-browser-close-watchdog",
                    daemon=True,
                )
                watchdog.start()
            try:
                if self._context is not None:
                    with contextlib.suppress(Exception):
                        self._context.close()
                self._pages.clear()
                self._shutdown_playwright()
            finally:
                done.set()
                if watchdog is not None:
                    watchdog.join(timeout=1.0)
                self._context = None
                self._playwright = None
                self._pages.clear()
                if profile is not None:
                    reap_profile_processes(profile)
                self._profile = None
                self._driver_pid = None
                _LIVE_SESSIONS.discard(self)

    @staticmethod
    def _watch_close(
        done: threading.Event, profile: Path, driver_pid: int | None, timeout_s: float
    ) -> None:
        if done.wait(max(0.0, timeout_s)):
            return
        # Killing Chrome drops the pipe the sync call is blocked on, so the
        # close() in the other thread returns (or raises, which it suppresses).
        reap_profile_processes(profile)
        if done.wait(1.0) or driver_pid is None:
            return
        # Still stuck: the node driver itself is wedged. It is a process daa
        # started too, and nothing of the user's is behind it.
        _signal([driver_pid], signal.SIGKILL)

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
                if self._active == tab_id:
                    self._active = None

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
            active = self._pages.get(self._active or "")
            if active is not None:
                return active
            # No active tab recorded: prefer the newest page that is actually
            # showing something. `open_tab` then `read_page` used to read
            # about:blank and report "no text" while the page the user asked
            # for sat in the next tab.
            real = [h for h in self._pages.values() if not _is_blank(h)]
            return (real or list(self._pages.values()) or [None])[-1]

    def open_tab(self, url: str) -> PlaywrightPage:
        """Navigate a NEW tab. The URL guard runs one layer up, in the tool."""
        if self._context is None:
            raise RuntimeError("browser is not started")
        with self._lock:
            page = self._context.new_page()
            handle = self._adopt(page)
            self._active = handle.id
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
    "CLOSE_TIMEOUT_S",
    "DEFAULT_PAGE_CACHE_DIR",
    "DEFAULT_PROFILE_DIR",
    "READ_PAGE_MAX_CHARS",
    "REAP_GRACE_S",
    "BrowserOptions",
    "PageCache",
    "PlaywrightPage",
    "PlaywrightSession",
    "get_session",
    "processes_using_profile",
    "profile_sites",
    "reap_profile_processes",
    "reset_session",
]


def _is_blank(handle: Any) -> bool:
    """A tab showing nothing. Playwright's context always opens one, and it is
    the first thing adopted -- which is why "the active tab" could not be the
    first entry in the pages dict."""
    try:
        # `url` is a METHOD on PlaywrightPage, not a property: reading it
        # without calling it stringifies a bound method, which is never
        # "about:blank", so every tab looked non-blank.
        url = str(handle.url() or "")
    except Exception:  # noqa: BLE001 -- a page mid-close answers nothing
        return True
    return url in ("", "about:blank") or url.startswith("chrome://newtab")
