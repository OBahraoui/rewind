from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rewind.config import FlowRef, Settings
from rewind.store import FileStore
from service.main import create_app

SECRET, API = "hook-secret", "api-token"


@pytest.fixture
def client(gl, cr, tmp_path):
    settings = Settings(project_id="42", gitlab_token="t", hash_key="k" * 32, webhook_secret=SECRET,
                        api_token=API, flows={s: FlowRef.parse("none") for s in ("plan", "review", "fix")}
                        | {"develop": FlowRef.parse("definition:developer/v1")})
    app = create_app(settings, gitlab=gl, cloudrun=cr, store=FileStore(tmp_path / "s"), run_ticker=False)
    with TestClient(app) as c:
        yield c


def issue_hook(iid: int, labels: list[str], previous: list[str] | None, action: str = "update") -> dict:
    payload = {"object_kind": "issue", "object_attributes": {"iid": iid, "state": "opened", "action": action},
               "labels": [{"title": t} for t in labels]}
    if previous is not None:
        payload["changes"] = {"labels": {"previous": [{"title": t} for t in previous], "current": []}}
    return payload


def test_webhook_rejects_bad_token(client):
    r = client.post("/webhooks/gitlab", json={}, headers={"X-Gitlab-Token": "nope"})
    assert r.status_code == 401


def test_label_starts_a_run_and_dashboard_shows_it(client, gl):
    iid = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    r = client.post("/webhooks/gitlab", json=issue_hook(iid, ["rewind:auto"], []),
                    headers={"X-Gitlab-Token": SECRET, "X-Gitlab-Event": "Issue Hook"})
    assert r.status_code == 200
    runs = client.get("/api/runs").json()
    assert len(runs) == 1 and runs[0]["state"] == "developing"
    html = client.get("/").text
    assert "Promo pricing" in html and "Developing" in html
    page = client.get(f"/runs/{runs[0]['id']}")
    assert page.status_code == 200 and runs[0]["checkpoint_id"] in page.text


def test_unrelated_label_change_is_ignored(client, gl):
    iid = gl.add_issue("x", labels=["rewind:auto", "bug"])
    client.post("/webhooks/gitlab", json=issue_hook(iid, ["rewind:auto", "bug"], ["rewind:auto"]),
                headers={"X-Gitlab-Token": SECRET, "X-Gitlab-Event": "Issue Hook"})
    assert client.get("/api/runs").json() == []


def test_manual_rewind_requires_token(client, gl):
    cp = client.app.state.engine.checkpoint()
    gl.update_feature_flag("new_pricing", {"active": True})
    assert client.post("/api/rewind", json={"checkpoint_id": cp.id}).status_code == 401
    r = client.post("/api/rewind", json={"checkpoint_id": cp.id, "dry_run": True},
                    headers={"Authorization": f"Bearer {API}"})
    assert r.status_code == 200 and r.json()["steps"][0]["outcome"] == "planned"
    r = client.post("/api/rewind", json={"checkpoint_id": cp.id}, headers={"Authorization": f"Bearer {API}"})
    assert r.json()["verification"]["ok"] is True
    assert gl.flags["new_pricing"]["active"] is False


def test_mcp_tools(client):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}
    assert client.post("/mcp/", json=init, headers=headers).status_code == 200
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "classify_action", "arguments": {"kind": "ci_variable", "op": "updated",
                                                                "fields": ["value_fp"]}}}
    r = client.post("/mcp/", json=call, headers=headers)
    assert r.status_code == 200, r.text
    assert "irreversible" in r.text
    listing = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=headers)
    names = {t["name"] for t in listing.json()["result"]["tools"]}
    assert {"rewind_policy", "classify_action", "check_paths", "get_ledger", "explain_checkpoint"} <= names
