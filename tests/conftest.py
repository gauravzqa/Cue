"""Test-suite guardrails.

Two things are enforced here rather than left to reviewer discipline, because
both failure modes are silent and expensive:

1. **No test may open a network socket.** A test that quietly reaches a real
   provider passes on the author's machine, bills someone, and is
   nondeterministic forever after. `live`-marked tests opt back in explicitly.
2. **No test may see a real API key.** Even a test that never connects can
   change behaviour based on key presence -- `build_provider` returns RealJev
   when `TYPESAFE_API_KEY` is set, so a developer with a populated `.env` runs
   a *different code path* than CI does, and the fakes stop being exercised.
   That is exactly how the FakeJev paths would rot without anyone noticing.

Both are lifted from an explicit instruction: implementation and verification
must not use live keys.
"""

from __future__ import annotations

import socket

import pytest

# Every key daa reads anywhere. Cleared for the whole session unless a test is
# marked `live`. Keep in sync with .env.example.
_PROVIDER_KEYS = (
    "TYPESAFE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "INWORLD_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


class NetworkUsedInTest(RuntimeError):
    """Raised instead of connecting. The message names the offender."""


@pytest.fixture(autouse=True, scope="session")
def _no_live_keys(request):
    """Hide provider keys from the whole session.

    Session-scoped and autouse: a fixture that only ran for some tests would
    leave the door open for the import-time reads (`Settings.load()`,
    `build_provider`) that decide fake-vs-real.
    """
    import os

    import daa.config as _cfg

    # Popping the vars is not enough on its own: `Settings.load()` calls
    # `load_dotenv(".env")`, which reads the developer's real .env off disk and
    # puts every key straight back. Neutralise the loader for the session too,
    # or this fixture silently does nothing -- which is how it first shipped.
    real_load_dotenv = _cfg.load_dotenv
    _cfg.load_dotenv = lambda *a, **kw: False

    saved = {k: os.environ.pop(k, None) for k in _PROVIDER_KEYS}
    # DAA_* flags too: a developer's .env sets dry_run=0, and a test suite that
    # mutates the real filesystem because of someone's local config is worse
    # than a failing one.
    saved["DAA_DRY_RUN"] = os.environ.pop("DAA_DRY_RUN", None)
    yield
    _cfg.load_dotenv = real_load_dotenv
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Block outbound sockets unless the test is marked `live`.

    Patches `socket.socket.connect`/`connect_ex` rather than the whole module,
    so loopback still works for anything that genuinely needs a local server,
    and the error names the address so the offending call is obvious.
    """
    if request.node.get_closest_marker("live"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address, *a, **kw)
        raise NetworkUsedInTest(
            f"{request.node.nodeid} tried to reach {address!r}. "
            "Tests run offline: use a fake provider, or mark the test `live`."
        )

    def _guard_ex(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect_ex(self, address, *a, **kw)
        raise NetworkUsedInTest(f"{request.node.nodeid} tried to reach {address!r}.")

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_ex)
