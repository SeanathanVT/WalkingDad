import pytest

import app


@pytest.fixture
def client(app_state):
    return app.app.test_client()


@pytest.fixture
def side_effects(monkeypatch, app_state):
    """Every guarded route's effect, recorded: BLE coroutines, history wipe, config write, process exit."""
    calls = []
    monkeypatch.setattr(app, "_clear_session_history", lambda: calls.append("clear_history"))
    monkeypatch.setattr(app, "_write_config", lambda updates: calls.append("write_config"))
    monkeypatch.setattr(app.os, "_exit", lambda code: calls.append("exit"))
    monkeypatch.setattr(app, "_start_ble_thread", lambda: calls.append("start_ble_thread"))
    return lambda: calls + [c.__name__ for c in app_state]


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:9999"},
    {"Origin": "https://localhost"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
])
@pytest.mark.parametrize("path", ["/start", "/shutdown", "/clear_history", "/settings", "/reconnect"])
def test_cross_site_post_blocked(client, side_effects, monkeypatch, headers, path):
    # Disconnected only for /reconnect: it's a no-op when connected, /start a no-op when not.
    monkeypatch.setattr(app, "connected", path != "/reconnect")
    assert client.post(path, headers=headers).status_code == 403
    assert side_effects() == []
    assert app._server_stopping is False


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://localhost"},
    {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
])
def test_same_origin_or_unmarked_post_allowed(client, side_effects, headers):
    assert client.post("/clear_history", headers=headers).status_code == 200
    assert side_effects() == ["clear_history"]


def test_cross_site_get_allowed(client):
    headers = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.get("/stats", headers=headers).status_code == 200
