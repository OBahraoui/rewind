"""Thin, typed GitLab REST/GraphQL client (plain httpx, no SDK).

Only the endpoints Rewind needs. Every method returns parsed JSON. Tests use
``rewind.testing.FakeGitLab``, which implements the same methods in memory.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from rewind.http import NotFound, RetryingClient


def _enc(value: str | int) -> str:
    return quote(str(value), safe="")


class GitLabClient:
    MAX_ITEMS = 2000  # safety cap for a single listing

    def __init__(
        self,
        base_url: str,
        token: str,
        project_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ):
        kw: dict[str, Any] = {"transport": transport}
        if sleep is not None:
            kw["sleep"] = sleep
        self.base_url = base_url.rstrip("/")
        self.project_id = str(project_id)
        self._api = RetryingClient(f"{self.base_url}/api/v4", {"PRIVATE-TOKEN": token}, **kw)
        self._gql = RetryingClient(self.base_url, {"Authorization": f"Bearer {token}"}, **kw)
        self._p = f"/projects/{_enc(project_id)}"

    def close(self) -> None:
        self._api.close()
        self._gql.close()

    # -- plumbing ------------------------------------------------------------
    def _get(self, path: str, **params: Any) -> Any:
        return self._api.json("GET", path, params=params or None)

    def _get_or_none(self, path: str, **params: Any) -> Any:
        try:
            return self._get(path, **params)
        except NotFound:
            return None

    def _paginate(self, path: str, limit: int | None = None, **params: Any) -> list[dict]:
        limit = limit or self.MAX_ITEMS
        params = {"per_page": 100, **params}
        page, out = 1, []
        while True:
            resp = self._api.request("GET", path, params={**params, "page": page})
            batch = resp.json()
            out.extend(batch)
            nxt = resp.headers.get("x-next-page", "")
            if not batch or not nxt or len(out) >= limit:
                return out[:limit]
            page = int(nxt)

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self._gql.json("POST", "/api/graphql", idempotent=True,
                              json={"query": query, "variables": variables or {}})
        if body.get("errors"):
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    # -- project -------------------------------------------------------------
    def project(self) -> dict:
        return self._get(self._p)

    def current_user(self) -> dict:
        return self._get("/user")

    # -- repository ----------------------------------------------------------
    def get_branch(self, name: str) -> dict | None:
        return self._get_or_none(f"{self._p}/repository/branches/{_enc(name)}")

    def list_branches(self) -> list[dict]:
        return self._paginate(f"{self._p}/repository/branches")

    def create_branch(self, name: str, ref: str) -> dict:
        return self._api.json("POST", f"{self._p}/repository/branches", params={"branch": name, "ref": ref})

    def delete_branch(self, name: str) -> None:
        self._api.request("DELETE", f"{self._p}/repository/branches/{_enc(name)}")

    def compare(self, from_ref: str, to_ref: str) -> dict:
        return self._get(f"{self._p}/repository/compare", **{"from": from_ref, "to": to_ref, "straight": "true"})

    def get_file_raw(self, path: str, ref: str) -> bytes | None:
        try:
            resp = self._api.request("GET", f"{self._p}/repository/files/{_enc(path)}/raw", params={"ref": ref})
        except NotFound:
            return None
        return resp.content

    def create_commit(self, branch: str, message: str, actions: list[dict], start_sha: str | None = None) -> dict:
        body: dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
        if start_sha:
            body["start_sha"] = start_sha
        return self._api.json("POST", f"{self._p}/repository/commits", json=body)

    def get_commit(self, sha: str) -> dict | None:
        return self._get_or_none(f"{self._p}/repository/commits/{_enc(sha)}")

    def list_tags(self) -> list[dict]:
        return self._paginate(f"{self._p}/repository/tags")

    def get_tag(self, name: str) -> dict | None:
        return self._get_or_none(f"{self._p}/repository/tags/{_enc(name)}")

    def create_tag(self, name: str, ref: str, message: str | None = None) -> dict:
        params = {"tag_name": name, "ref": ref}
        if message:
            params["message"] = message
        return self._api.json("POST", f"{self._p}/repository/tags", params=params)

    def delete_tag(self, name: str) -> None:
        self._api.request("DELETE", f"{self._p}/repository/tags/{_enc(name)}")

    # -- releases ------------------------------------------------------------
    def list_releases(self) -> list[dict]:
        return self._paginate(f"{self._p}/releases")

    def get_release(self, tag: str) -> dict | None:
        return self._get_or_none(f"{self._p}/releases/{_enc(tag)}")

    def create_release(self, tag_name: str, name: str, description: str) -> dict:
        body = {"tag_name": tag_name, "name": name, "description": description}
        return self._api.json("POST", f"{self._p}/releases", json=body)

    def update_release(self, tag: str, **fields: Any) -> dict:
        return self._api.json("PUT", f"{self._p}/releases/{_enc(tag)}", json=fields)

    def delete_release(self, tag: str) -> None:
        self._api.request("DELETE", f"{self._p}/releases/{_enc(tag)}")

    # -- merge requests ------------------------------------------------------
    def list_merge_requests(self, **params: Any) -> list[dict]:
        return self._paginate(f"{self._p}/merge_requests", **params)

    def get_merge_request(self, iid: int) -> dict | None:
        return self._get_or_none(f"{self._p}/merge_requests/{iid}")

    def create_merge_request(self, source: str, target: str, title: str, description: str = "",
                             labels: list[str] | None = None, remove_source_branch: bool = True) -> dict:
        body = {"source_branch": source, "target_branch": target, "title": title, "description": description,
                "remove_source_branch": remove_source_branch, "labels": ",".join(labels or [])}
        return self._api.json("POST", f"{self._p}/merge_requests", json=body)

    def update_merge_request(self, iid: int, **fields: Any) -> dict:
        return self._api.json("PUT", f"{self._p}/merge_requests/{iid}", json=fields)

    def merge_merge_request(self, iid: int, *, sha: str | None = None, merge_commit_message: str | None = None,
                            should_remove_source_branch: bool = True) -> dict:
        body: dict[str, Any] = {"should_remove_source_branch": should_remove_source_branch}
        if sha:
            body["sha"] = sha  # GitLab refuses the merge if the head moved since we gated it
        if merge_commit_message:
            body["merge_commit_message"] = merge_commit_message
        return self._api.json("PUT", f"{self._p}/merge_requests/{iid}/merge", json=body)

    def list_mr_notes(self, iid: int) -> list[dict]:
        return self._paginate(f"{self._p}/merge_requests/{iid}/notes", sort="asc")

    def create_mr_note(self, iid: int, body: str) -> dict:
        return self._api.json("POST", f"{self._p}/merge_requests/{iid}/notes", json={"body": body})

    def mr_pipelines(self, iid: int) -> list[dict]:
        return self._get(f"{self._p}/merge_requests/{iid}/pipelines")

    # -- issues --------------------------------------------------------------
    def list_issues(self, **params: Any) -> list[dict]:
        return self._paginate(f"{self._p}/issues", **params)

    def get_issue(self, iid: int) -> dict | None:
        return self._get_or_none(f"{self._p}/issues/{iid}")

    def create_issue(self, title: str, description: str = "", labels: list[str] | None = None) -> dict:
        body = {"title": title, "description": description, "labels": ",".join(labels or [])}
        return self._api.json("POST", f"{self._p}/issues", json=body)

    def update_issue(self, iid: int, **fields: Any) -> dict:
        return self._api.json("PUT", f"{self._p}/issues/{iid}", json=fields)

    def list_issue_notes(self, iid: int) -> list[dict]:
        return self._paginate(f"{self._p}/issues/{iid}/notes", sort="asc")

    def create_issue_note(self, iid: int, body: str) -> dict:
        return self._api.json("POST", f"{self._p}/issues/{iid}/notes", json={"body": body})

    def update_issue_note(self, iid: int, note_id: int, body: str) -> dict:
        return self._api.json("PUT", f"{self._p}/issues/{iid}/notes/{note_id}", json={"body": body})

    def related_merge_requests(self, iid: int) -> list[dict]:
        return self._get(f"{self._p}/issues/{iid}/related_merge_requests")

    # -- labels --------------------------------------------------------------
    def list_labels(self) -> list[dict]:
        return self._paginate(f"{self._p}/labels")

    def get_label(self, name: str) -> dict | None:
        return self._get_or_none(f"{self._p}/labels/{_enc(name)}")

    def create_label(self, name: str, color: str, description: str = "") -> dict:
        body = {"name": name, "color": color, "description": description}
        return self._api.json("POST", f"{self._p}/labels", json=body)

    def update_label(self, name: str, **fields: Any) -> dict:
        return self._api.json("PUT", f"{self._p}/labels/{_enc(name)}", json=fields)

    def delete_label(self, name: str) -> None:
        self._api.request("DELETE", f"{self._p}/labels/{_enc(name)}")

    # -- feature flags -------------------------------------------------------
    def list_feature_flags(self) -> list[dict]:
        return self._paginate(f"{self._p}/feature_flags")

    def get_feature_flag(self, name: str) -> dict | None:
        return self._get_or_none(f"{self._p}/feature_flags/{_enc(name)}")

    def create_feature_flag(self, payload: dict) -> dict:
        return self._api.json("POST", f"{self._p}/feature_flags", json=payload)

    def update_feature_flag(self, name: str, payload: dict) -> dict:
        return self._api.json("PUT", f"{self._p}/feature_flags/{_enc(name)}", json=payload)

    def delete_feature_flag(self, name: str) -> None:
        self._api.request("DELETE", f"{self._p}/feature_flags/{_enc(name)}")

    # -- CI/CD variables -----------------------------------------------------
    def list_variables(self) -> list[dict]:
        return self._paginate(f"{self._p}/variables")

    def get_variable(self, key: str, scope: str = "*") -> dict | None:
        params = {"filter[environment_scope]": scope}
        return self._get_or_none(f"{self._p}/variables/{_enc(key)}", **params)

    def create_variable(self, key: str, value: str, *, masked: bool = False, protected: bool = False,
                        scope: str = "*") -> dict:
        body = {"key": key, "value": value, "masked": masked, "protected": protected, "environment_scope": scope}
        return self._api.json("POST", f"{self._p}/variables", json=body)

    def update_variable(self, key: str, scope: str, **fields: Any) -> dict:
        body = {**fields, "filter": {"environment_scope": scope}}
        return self._api.json("PUT", f"{self._p}/variables/{_enc(key)}", json=body)

    def delete_variable(self, key: str, scope: str = "*") -> None:
        self._api.request("DELETE", f"{self._p}/variables/{_enc(key)}",
                          params={"filter[environment_scope]": scope})

    # -- pipelines, jobs, deployments ----------------------------------------
    def list_pipelines(self, **params: Any) -> list[dict]:
        return self._get(f"{self._p}/pipelines", **params)

    def get_pipeline(self, pipeline_id: int) -> dict | None:
        return self._get_or_none(f"{self._p}/pipelines/{pipeline_id}")

    def list_pipeline_jobs(self, pipeline_id: int, **params: Any) -> list[dict]:
        return self._paginate(f"{self._p}/pipelines/{pipeline_id}/jobs", **params)

    def create_deployment(self, environment: str, sha: str, ref: str, status: str = "success") -> dict:
        body = {"environment": environment, "sha": sha, "ref": ref, "tag": False, "status": status}
        return self._api.json("POST", f"{self._p}/deployments", json=body)

    def create_project_access_token(self, name: str, scopes: list[str], access_level: int, expires_at: str) -> dict:
        body = {"name": name, "scopes": scopes, "access_level": access_level, "expires_at": expires_at}
        return self._api.json("POST", f"{self._p}/access_tokens", json=body)

    # -- webhooks ------------------------------------------------------------
    def list_hooks(self) -> list[dict]:
        return self._get(f"{self._p}/hooks")

    def create_hook(self, payload: dict) -> dict:
        return self._api.json("POST", f"{self._p}/hooks", json=payload)

    def update_hook(self, hook_id: int, payload: dict) -> dict:
        return self._api.json("PUT", f"{self._p}/hooks/{hook_id}", json=payload)

    # -- Duo Agent Platform --------------------------------------------------
    def agent_privileges(self) -> list[dict]:
        body = self._get("/ai/duo_workflows/workflows/agent_privileges")
        return body.get("all_privileges", body) if isinstance(body, dict) else body

    def start_workflow(self, payload: dict) -> dict:
        return self._api.json("POST", "/ai/duo_workflows/workflows", json=payload)

    def workflow_status(self, workflow_id: int | str) -> dict:
        """Current status of a flow. GraphQL is the documented read path."""
        query = """
        query($id: AiDuoWorkflowsWorkflowID!) {
          duoWorkflowWorkflows(workflowId: $id) { nodes { id status } }
        }"""
        gid = f"gid://gitlab/Ai::DuoWorkflows::Workflow/{workflow_id}"
        nodes = self.graphql(query, {"id": gid})["duoWorkflowWorkflows"]["nodes"]
        if not nodes:
            raise NotFound("GRAPHQL", gid, 404, "workflow not found")
        return {"id": workflow_id, "status": str(nodes[0]["status"]).lower()}

    def workflow_trace(self, workflow_id: int | str) -> str:
        return self._api.request("GET", f"/ai/duo_workflows/workflows/{workflow_id}/trace.jsonl").text
