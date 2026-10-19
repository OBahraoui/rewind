"""The real clients against a mock transport: pagination, retries, errors, Cloud Run, GCS."""

from __future__ import annotations

import json

import httpx
import pytest

from rewind.cloudrun import CloudRunClient, serving_revisions
from rewind.gitlab import GitLabClient
from rewind.http import ApiError
from rewind.store import AlreadyExists, GCSStore


def gitlab(handler) -> GitLabClient:
    return GitLabClient("https://gl.example", "tok", "group/proj", transport=httpx.MockTransport(handler),
                        sleep=lambda _s: None)


def test_pagination_follows_next_page_and_encodes_project():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        assert req.headers["PRIVATE-TOKEN"] == "tok"
        page = int(req.url.params["page"])
        nxt = {"x-next-page": str(page + 1)} if page < 3 else {}
        return httpx.Response(200, json=[{"name": f"l{page}"}], headers=nxt)

    labels = gitlab(handler).list_labels()
    assert [x["name"] for x in labels] == ["l1", "l2", "l3"]
    assert seen[0].raw_path.startswith(b"/api/v4/projects/group%2Fproj/labels")


def test_retries_transient_errors_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503 if calls["n"] == 1 else 429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"default_branch": "main"})

    assert gitlab(handler).project()["default_branch"] == "main"
    assert calls["n"] == 3


def test_not_found_is_none_and_errors_raise():
    def handler(req):
        if "branches" in req.url.path:
            return httpx.Response(404, json={"message": "404 Branch Not Found"})
        return httpx.Response(403, json={"message": "403 Forbidden"})

    gl = gitlab(handler)
    assert gl.get_branch("nope") is None
    with pytest.raises(ApiError) as exc:
        gl.project()
    assert exc.value.status == 403


def test_workflow_status_via_graphql():
    def handler(req):
        assert req.url.path == "/api/graphql"
        body = json.loads(req.content)
        assert body["variables"]["id"] == "gid://gitlab/Ai::DuoWorkflows::Workflow/77"
        nodes = [{"id": "x", "status": "FINISHED"}]
        return httpx.Response(200, json={"data": {"duoWorkflowWorkflows": {"nodes": nodes}}})

    assert gitlab(handler).workflow_status(77)["status"] == "finished"


def test_merge_sends_sha_guard():
    def handler(req):
        body = json.loads(req.content)
        assert body["sha"] == "abc" and "Rewind-Run" in body["merge_commit_message"]
        return httpx.Response(200, json={"iid": 5, "state": "merged"})

    gitlab(handler).merge_merge_request(5, sha="abc", merge_commit_message="Merge\n\nRewind-Run: r")


SERVICE = {
    "name": "projects/p/locations/r/services/shop",
    "etag": '"e1"',
    "latestReadyRevision": "projects/p/locations/r/services/shop/revisions/shop-00002",
    "traffic": [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}],
    "trafficStatuses": [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}],
}


def test_serving_resolves_latest():
    assert serving_revisions(SERVICE) == [["shop-00002", 100]]


def test_pin_traffic_sends_etag_and_waits_for_operation():
    patched = {}

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/services/shop"):
            return httpx.Response(200, json=SERVICE)
        if req.method == "PATCH":
            patched.update(json.loads(req.content))
            return httpx.Response(200, json={"name": "projects/p/locations/r/operations/op1", "done": False})
        if req.url.path.endswith("operations/op1"):
            return httpx.Response(200, json={"name": "projects/p/locations/r/operations/op1", "done": True})
        return httpx.Response(404)

    cr = CloudRunClient("p", "r", {"production": "shop"}, transport=httpx.MockTransport(handler),
                        token=lambda: "t", sleep=lambda _s: None)
    cr.pin_traffic("production", [["shop-00001", 100]])
    assert patched["etag"] == '"e1"'
    assert patched["traffic"] == [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": "shop-00001",
                                   "percent": 100}]


def test_gcs_create_is_atomic():
    objects: dict[str, bytes] = {}

    def handler(req):
        if req.url.path.startswith("/upload/"):
            name = req.url.params["name"]
            if req.url.params.get("ifGenerationMatch") == "0" and name in objects:
                return httpx.Response(412)
            objects[name] = req.content
            return httpx.Response(200, json={"name": name})
        if req.method == "GET" and req.url.params.get("alt") == "media":
            name = req.url.path.split("/o/", 1)[1].replace("%2F", "/")
            return httpx.Response(200, content=objects[name]) if name in objects else httpx.Response(404)
        if req.method == "DELETE":
            objects.pop(req.url.path.split("/o/", 1)[1].replace("%2F", "/"), None)
            return httpx.Response(204)
        return httpx.Response(200, json={"items": [{"name": n} for n in objects]})

    store = GCSStore("bucket", transport=httpx.MockTransport(handler), token=lambda: "t")
    store.create("locks/x.json", {"a": 1})
    with pytest.raises(AlreadyExists):
        store.create("locks/x.json", {"a": 2})
    assert store.get("locks/x.json") == {"a": 1}
    store.delete("locks/x.json")
    assert store.get("locks/x.json") is None


def test_post_is_not_retried_after_a_5xx_but_is_after_429():
    """A 5xx may arrive after the server acted: repeating start_workflow would start a second flow."""
    calls = {"n": 0}

    def five_hundred(req):
        calls["n"] += 1
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(ApiError):
        gitlab(five_hundred).start_workflow({"goal": "x"})
    assert calls["n"] == 1

    calls["n"] = 0

    def rate_limited_once(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(201, json={"id": 5})

    assert gitlab(rate_limited_once).start_workflow({"goal": "x"})["id"] == 5
    assert calls["n"] == 2


def test_post_is_not_retried_after_a_read_timeout():
    calls = {"n": 0}

    def slow(req):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(httpx.ReadTimeout):
        gitlab(slow).create_issue_note(1, "hello")
    assert calls["n"] == 1


def test_graphql_queries_are_retried():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502)
        nodes = [{"id": "x", "status": "RUNNING"}]
        return httpx.Response(200, json={"data": {"duoWorkflowWorkflows": {"nodes": nodes}}})

    assert gitlab(flaky).workflow_status(1)["status"] == "running"
    assert calls["n"] == 3
