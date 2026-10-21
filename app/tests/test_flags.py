import httpx

from app.flags import FlagClient


def client_with(features, monkeypatch, app_name="production"):
    calls = []

    def fake_get(url, timeout, headers):
        calls.append((url, headers))
        return httpx.Response(200, json={"version": 1, "features": features}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return FlagClient("https://gitlab.com/api/v4/feature_flags/unleash/1", "inst", app_name, refresh_s=0), calls


def test_default_strategy(monkeypatch):
    c, calls = client_with([{"name": "f", "enabled": True, "strategies": [{"name": "default"}]}], monkeypatch)
    assert c.is_enabled("f")
    assert calls[0][1]["UNLEASH-APPNAME"] == "production"


def test_disabled_and_unknown(monkeypatch):
    c, _ = client_with([{"name": "f", "enabled": False, "strategies": [{"name": "default"}]},
                        {"name": "g", "enabled": True, "strategies": [{"name": "mystery"}]}], monkeypatch)
    assert not c.is_enabled("f")
    assert not c.is_enabled("g")  # unknown strategy fails closed
    assert not c.is_enabled("missing")


def test_user_and_rollout(monkeypatch):
    c, _ = client_with([
        {"name": "u", "enabled": True, "strategies": [{"name": "userWithId", "parameters": {"userIds": "a, b"}}]},
        {"name": "all", "enabled": True,
         "strategies": [{"name": "gradualRolloutUserId", "parameters": {"percentage": "100", "groupId": "x"}}]},
        {"name": "none", "enabled": True,
         "strategies": [{"name": "gradualRolloutUserId", "parameters": {"percentage": "0", "groupId": "x"}}]},
    ], monkeypatch)
    assert c.is_enabled("u", "b") and not c.is_enabled("u", "c")
    assert c.is_enabled("all", "anyone") and not c.is_enabled("none", "anyone")


def test_keeps_last_known_state_when_gitlab_is_down(monkeypatch):
    c, _ = client_with([{"name": "f", "enabled": True, "strategies": [{"name": "default"}]}], monkeypatch)
    assert c.is_enabled("f")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert c.is_enabled("f")


def test_unconfigured_client_is_off():
    assert not FlagClient().is_enabled("f")


def test_status_reports_auth_failure(monkeypatch):
    def unauthorized(url, timeout, headers):
        return httpx.Response(401, json={"message": "401 Unauthorized"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", unauthorized)
    c = FlagClient("https://gitlab.com/api/v4/feature_flags/unleash/1", "wrong", "production", refresh_s=0)
    assert c.status() == "error: HTTP 401 from GitLab feature flags (never loaded)"
    assert not c.is_enabled("new_pricing")  # still fails closed for the app...


def test_status_ok_and_unconfigured(monkeypatch):
    c, _ = client_with([{"name": "f", "enabled": True, "strategies": [{"name": "default"}]}], monkeypatch)
    assert c.status() == "ok"
    assert FlagClient().status() == "unconfigured"
