"""Closing the browser, as a guarantee rather than a hope.

`tests/test_browser_live.py` proves this against a real Chrome, and only when
Playwright and its browsers are installed. This file proves the same promises
with no browser at all, so they are checked on every clone and on CI:

  - `close()` is BOUNDED. `BrowserContext.close()` takes no timeout and is a
    request over a pipe to a process that may never answer, so the deadline
    belongs to daa. The ladder is close -> signal the browser -> kill the
    driver, and each rung is tested here.
  - Nothing daa launched outlives the interpreter, including after an
    exception, a crashed test or a `KeyboardInterrupt`.
  - The kill is keyed on daa's OWN `--user-data-dir` and nothing else. The
    user's real Chrome is the thing that must never be touched, and the tests
    that matter most in this file are the ones that check it is not matched.
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

from daa.config import Settings
from daa.tools.base import ShellResult
from daa.tools.browser import session as session_mod
from daa.tools.browser.session import (
    DEFAULT_PROFILE_DIR,
    BrowserOptions,
    PlaywrightSession,
    processes_using_profile,
    reap_profile_processes,
)

DAA_PROFILE = Path.home() / ".daa" / "browser-profile"
_USER_PROFILE = "/Users/someone/Library/Application Support/Google/Chrome"

# One line per process, in `ps -axo pid=,command=` shape. The first three are
# daa's browser and its helpers; the rest are things that must survive.
_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PS_OUTPUT = "\n".join(
    [
        f"  501 {_CHROME} --user-data-dir={DAA_PROFILE} --remote-debugging-pipe",
        f"  502 {_CHROME} --type=gpu-process --user-data-dir={DAA_PROFILE} --gpu-prefs=x",
        f"  503 {_CHROME} --type=utility --user-data-dir={DAA_PROFILE}",
        # The user's own Chrome, on their own profile. Never ours.
        f"  601 {_CHROME}",
        f"  602 {_CHROME} --user-data-dir={_USER_PROFILE}",
        # A profile whose path merely STARTS with ours.
        f"  701 {_CHROME} --user-data-dir={DAA_PROFILE}-2 --no-first-run",
        "  801 /usr/bin/grep --user-data-dir",
    ]
)


class _Ps:
    """Stands in for the one `ps` call, made through `tools/base.run_argv`."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls = 0

    def __call__(self, argv, **_kwargs):
        assert next(iter(argv)) == "/bin/ps", "only ps is ever spawned from here"
        self.calls += 1
        return self


@pytest.fixture
def ps(monkeypatch):
    fake = _Ps(PS_OUTPUT)
    monkeypatch.setattr(session_mod, "run_argv", fake)
    return fake


# ---------------------------------------------------------------------------
# whose processes these are
# ---------------------------------------------------------------------------


def test_only_the_processes_on_our_own_profile_are_ours(ps):
    assert processes_using_profile(DAA_PROFILE) == [501, 502, 503]


def test_a_profile_path_that_merely_starts_with_ours_is_not_ours(ps):
    """`~/.daa/browser-profile-2` is a different browser and it is not daa's."""
    assert 701 not in processes_using_profile(DAA_PROFILE)


def test_the_users_own_chrome_is_never_matched(ps):
    found = processes_using_profile(DAA_PROFILE)
    assert 601 not in found and 602 not in found
    # And the reverse: daa's kill switch, pointed at the user's profile
    # directory, still only ever finds what carries that exact flag.
    assert processes_using_profile(_USER_PROFILE) == [602]


@pytest.mark.parametrize("profile", ["", "/", ".", str(Path.home())])
def test_a_profile_too_vague_to_match_on_matches_nothing(ps, profile):
    """A misconfigured profile dir must find NOTHING, never everything."""
    assert processes_using_profile(profile) == []


def test_finding_nothing_is_not_an_error_when_ps_is_missing(monkeypatch):
    """`run_argv` reports a missing binary as empty output, not an exception."""
    monkeypatch.setattr(session_mod, "run_argv",
                        lambda argv, **_kw: ShellResult(argv=tuple(argv), returncode=-1,
                                                        not_found=True))
    assert processes_using_profile(DAA_PROFILE) == []


# ---------------------------------------------------------------------------
# reaping
# ---------------------------------------------------------------------------


def test_reaping_asks_with_sigterm_and_insists_with_sigkill(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(session_mod, "_signal",
                        lambda pids, sig: sent.extend((p, sig) for p in pids))
    monkeypatch.setattr(session_mod, "processes_using_profile", lambda *_a, **_kw: [501])

    killed = reap_profile_processes("/tmp/daa-profile", grace_s=0.1)

    assert killed == [501]
    assert (501, signal.SIGTERM) in sent
    assert (501, signal.SIGKILL) in sent


def test_a_process_that_goes_quietly_is_never_killed(monkeypatch):
    sent: list[tuple[int, int]] = []
    seen = {"n": 0}

    def alive(*_a, **_kw):
        seen["n"] += 1
        return [501] if seen["n"] == 1 else []

    monkeypatch.setattr(session_mod, "_signal",
                        lambda pids, sig: sent.extend((p, sig) for p in pids))
    monkeypatch.setattr(session_mod, "processes_using_profile", alive)

    assert reap_profile_processes("/tmp/daa-profile", grace_s=1.0) == [501]
    assert sent == [(501, signal.SIGTERM)]


def test_reaping_nothing_costs_one_look_and_no_signals(monkeypatch):
    sent: list[int] = []
    monkeypatch.setattr(session_mod, "_signal", lambda pids, sig: sent.extend(pids))
    monkeypatch.setattr(session_mod, "processes_using_profile", lambda *_a, **_kw: [])
    assert reap_profile_processes("/tmp/daa-profile") == []
    assert sent == []


# ---------------------------------------------------------------------------
# close(), rung by rung
# ---------------------------------------------------------------------------


class WedgedContext:
    """A context whose close() returns only once the browser is really gone."""

    def __init__(self, unblocked: threading.Event) -> None:
        self.unblocked = unblocked
        self.closed = False

    def close(self, *_a, **_kw) -> None:
        # The deadline is the test's safety net: if the watchdog never fires,
        # this fails the test rather than hanging the suite.
        self.unblocked.wait(20)
        self.closed = True


def _session_with(context, monkeypatch, profile="/tmp/daa-test-profile"):
    session = PlaywrightSession(BrowserOptions(profile_dir=Path(profile), headless=True))
    session._context = context
    session._profile = Path(profile)
    session._driver_pid = 4242
    session_mod._LIVE_SESSIONS.add(session)
    return session


def test_a_close_that_would_block_forever_is_cut_short(monkeypatch):
    unblocked = threading.Event()
    reaped: list[str] = []

    def fake_reap(profile, **_kw):
        reaped.append(str(profile))
        unblocked.set()          # what killing Chrome does to the blocked pipe
        return [501]

    monkeypatch.setattr(session_mod, "reap_profile_processes", fake_reap)
    context = WedgedContext(unblocked)
    session = _session_with(context, monkeypatch)

    started = time.monotonic()
    session.close(timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert context.closed is True
    assert reaped, "the watchdog never signalled the browser"
    assert elapsed < 5, f"close() took {elapsed:.1f}s"
    assert session.started is False


def test_a_close_that_returns_promptly_signals_nothing(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(session_mod, "reap_profile_processes",
                        lambda profile, **_kw: calls.append("reap") or [])

    class Quiet:
        closed = False

        def close(self, *_a, **_kw):
            self.closed = True

    session = _session_with(Quiet(), monkeypatch)
    session.close(timeout_s=30)
    # One last look after a clean close is the guarantee; the watchdog rung is
    # not reached, so there is exactly one.
    assert calls == ["reap"]


def test_closing_a_session_that_never_started_touches_nothing(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("a session that never started must not shell out")

    monkeypatch.setattr(session_mod, "run_argv", boom)
    session = PlaywrightSession(BrowserOptions(profile_dir=DEFAULT_PROFILE_DIR))
    session.close()
    session.close()
    assert session.started is False


def test_the_last_rung_kills_the_driver_daa_started(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(session_mod, "reap_profile_processes", lambda *_a, **_kw: [])
    monkeypatch.setattr(session_mod, "_signal",
                        lambda pids, sig: sent.extend((p, sig) for p in pids))

    # A close that never returns at all: the browser signal did not help, so
    # Playwright's own node driver is what is wedged.
    never = threading.Event()
    PlaywrightSession._watch_close(never, Path("/tmp/daa-test-profile"), 4242, 0.05)

    assert sent == [(4242, signal.SIGKILL)]


def test_an_interpreter_that_dies_with_a_session_open_still_closes_it():
    closed: list[float] = []

    class Abandoned:
        options = BrowserOptions()
        started = True

        def close(self, **_kw):
            closed.append(1.0)

    session = Abandoned()
    session_mod._LIVE_SESSIONS.add(session)
    try:
        session_mod._close_live_sessions()
    finally:
        session_mod._LIVE_SESSIONS.discard(session)
    assert closed, "the atexit hook did not close an abandoned session"


def test_a_tool_answers_to_the_session_it_will_actually_drive(monkeypatch):
    """`options` resolved BEFORE the session was bound, so a tool built with no
    explicit session reported defaults while driving a session configured
    otherwise. Two sources of truth for the two settings that decide what daa
    refuses -- the same shape as the `dry_run` registry trap."""
    from daa.tools.browser import session as session_mod
    from daa.tools.browser.reading import OpenTab

    session_mod.reset_session()
    configured = session_mod.BrowserOptions(allow_private_hosts=True)
    try:
        session_mod.get_session(configured)
        tool = OpenTab(Settings(dry_run=True, enable_browser=True))
        assert tool.options.allow_private_hosts is True, (
            "the tool answered from a fresh default, not from the session it will drive"
        )
    finally:
        session_mod.reset_session()


def test_explicit_options_still_win_over_the_session():
    """The constructor argument is how a test or an embedder pins a tool to its
    own configuration; consulting the session must not override it."""
    from daa.tools.browser import session as session_mod
    from daa.tools.browser.reading import OpenTab

    session_mod.reset_session()
    try:
        session_mod.get_session(session_mod.BrowserOptions(allow_private_hosts=True))
        tool = OpenTab(
            Settings(dry_run=True, enable_browser=True),
            options=session_mod.BrowserOptions(allow_private_hosts=False),
        )
        assert tool.options.allow_private_hosts is False
    finally:
        session_mod.reset_session()
