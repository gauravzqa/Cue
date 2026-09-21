import pytest


def test_keys_are_hidden_even_though_dotenv_has_them():
    from daa.config import Settings
    s = Settings.load()          # reads .env, which DOES have all five keys
    assert s.typesafe_api_key is None, "a real key leaked into the test session"
    assert s.jev_live is False, "build_provider would have returned RealJev"


def test_the_network_is_actually_blocked():
    import socket
    with pytest.raises(Exception) as e:
        socket.create_connection(("api.deepseek.com", 443), timeout=3)
    assert "tried to reach" in str(e.value), f"blocked for the wrong reason: {e.value}"


def test_dry_run_cannot_be_turned_off_by_local_env():
    from daa.config import Settings
    assert Settings.load().dry_run is True
