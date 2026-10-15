"""In-memory GitLab and Cloud Run with the same methods as the real clients.

The fake models real semantics where Rewind depends on them: git trees and
commits, merges (three-way, ``Closes #n`` closes issues), feature flag strategy
updates with ``_destroy``, the merge ``sha`` guard, and Cloud Run traffic.
"""

from __future__ import annotations

import base64
import copy
import difflib
import functools
import hashlib
import itertools
import re
import threading
from datetime import UTC, datetime
from typing import Any

from rewind.http import ApiError, NotFound

_lock = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with _lock:
            if fn.__name__ in self.fail_next:  # inject a failure into any call, read or write
                raise self.fail_next.pop(fn.__name__)
            return copy.deepcopy(fn(self, *a, **kw))
    return wrapper


def now() -> str:
    return datetime.now(UTC).isoformat()


def _nf(what: str) -> NotFound:
    return NotFound("GET", what, 404, "404 Not Found")


class FakeGitLab:
    def __init__(self, files: dict[str, str] | None = None, project_id: str = "42"):
        self.project_id = project_id
        self.default_branch = "main"
        self._seq = itertools.count(1)
        self._iid = {"issue": itertools.count(1), "mr": itertools.count(1)}
        self.commits: dict[str, dict] = {}
        root = self._new_commit({p: (c.encode(), "100644") for p, c in (files or {"README.md": "hello\n"}).items()},
                                [], "initial commit")
        self.branches: dict[str, dict] = {"main": {"sha": root, "protected": True}}
        self.tags: dict[str, dict] = {}
        self.releases: dict[str, dict] = {}
        self.mrs: dict[int, dict] = {}
        self.mr_notes: dict[int, list[dict]] = {}
        self.issues: dict[int, dict] = {}
        self.issue_notes: dict[int, list[dict]] = {}
        self.labels: dict[str, dict] = {}
        self.flags: dict[str, dict] = {}
        self.variables: dict[tuple[str, str], dict] = {}
        self.pipelines: dict[int, dict] = {}
        self.jobs: dict[int, list[dict]] = {}
        self.deployments: list[dict] = []
        self.hooks: list[dict] = []
        self.workflows: dict[int, dict] = {}
        self.users = {"rewind-bot": {"id": 1, "username": "rewind-bot", "name": "Rewind"},
                      "alice": {"id": 2, "username": "alice", "name": "Alice"}}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail_next: dict[str, Exception] = {}

    # -- internals -----------------------------------------------------------
    def _record(self, name: str, *args, **kw) -> None:
        """Log a write, so tests can assert what Rewind changed."""
        self.calls.append((name, args, kw))

    def _new_commit(self, tree: dict, parents: list[str], title: str) -> str:
        sha = hashlib.sha1(f"{next(self._seq)}{title}".encode()).hexdigest()  # noqa: S324
        self.commits[sha] = {"tree": tree, "parents": parents, "title": title, "created_at": now()}
        return sha

    def _resolve(self, ref: str) -> str:
        if ref in self.branches:
            return self.branches[ref]["sha"]
        if ref in self.tags:
            return self.tags[ref]["target"]
        if ref in self.commits:
            return ref
        raise _nf(f"ref {ref}")

    def tree(self, ref: str) -> dict[str, str]:
        """Test helper: {path: text} at a ref."""
        return {p: c.decode() for p, (c, _) in self.commits[self._resolve(ref)]["tree"].items()}

    def _ancestors(self, sha: str) -> list[str]:
        seen, order, queue = set(), [], [sha]
        while queue:
            s = queue.pop(0)
            if s in seen:
                continue
            seen.add(s)
            order.append(s)
            queue.extend(self.commits[s]["parents"])
        return order

    def _branch_json(self, name: str) -> dict:
        b = self.branches[name]
        c = self.commits[b["sha"]]
        return {"name": name, "protected": b["protected"], "default": name == self.default_branch,
                "commit": {"id": b["sha"], "title": c["title"]}}

    # -- project -------------------------------------------------------------
    @locked
    def project(self) -> dict:
        return {"id": self.project_id, "default_branch": self.default_branch,
                "web_url": "https://gitlab.example/acme/shop", "path_with_namespace": "acme/shop"}

    @locked
    def current_user(self) -> dict:
        return self.users["rewind-bot"]

    # -- repository ----------------------------------------------------------
    @locked
    def get_branch(self, name: str) -> dict | None:
        return self._branch_json(name) if name in self.branches else None

    @locked
    def list_branches(self) -> list[dict]:
        return [self._branch_json(n) for n in sorted(self.branches)]

    @locked
    def create_branch(self, name: str, ref: str) -> dict:
        self._record("create_branch", name, ref)
        if name in self.branches:
            raise ApiError("POST", "branches", 400, "Branch already exists")
        self.branches[name] = {"sha": self._resolve(ref), "protected": False}
        return self._branch_json(name)

    @locked
    def delete_branch(self, name: str) -> None:
        self._record("delete_branch", name)
        if name not in self.branches:
            raise _nf(f"branch {name}")
        if self.branches[name]["protected"]:
            raise ApiError("DELETE", "branches", 403, "protected")
        del self.branches[name]

    @locked
    def compare(self, from_ref: str, to_ref: str) -> dict:
        a = self.commits[self._resolve(from_ref)]["tree"]
        b = self.commits[self._resolve(to_ref)]["tree"]
        diffs = []
        for path in sorted(set(a) | set(b)):
            if a.get(path) == b.get(path):
                continue
            old, new = a.get(path), b.get(path)
            text = "".join(difflib.unified_diff(
                (old[0].decode(errors="replace") if old else "").splitlines(keepends=True),
                (new[0].decode(errors="replace") if new else "").splitlines(keepends=True)))
            diffs.append({"old_path": path, "new_path": path, "new_file": old is None, "deleted_file": new is None,
                          "renamed_file": False, "a_mode": old[1] if old else "0", "b_mode": new[1] if new else "0",
                          "diff": text})
        return {"diffs": diffs, "commits": [], "compare_timeout": False}

    @locked
    def get_file_raw(self, path: str, ref: str) -> bytes | None:
        entry = self.commits[self._resolve(ref)]["tree"].get(path)
        return entry[0] if entry else None

    @locked
    def create_commit(self, branch: str, message: str, actions: list[dict], start_sha: str | None = None) -> dict:
        self._record("create_commit", branch, message)
        base = start_sha or self.branches[branch]["sha"]
        tree = dict(self.commits[base]["tree"])
        for act in actions:
            path = act["file_path"]
            content = act.get("content", "")
            data = base64.b64decode(content) if act.get("encoding") == "base64" else content.encode()
            mode = "100755" if act.get("execute_filemode") else "100644"
            kind = act["action"]
            if kind == "create":
                if path in tree:
                    raise ApiError("POST", "commits", 400, f"A file with this name already exists: {path}")
                tree[path] = (data, mode)
            elif kind == "update":
                if path not in tree:
                    raise ApiError("POST", "commits", 400, f"File does not exist: {path}")
                tree[path] = (data, tree[path][1])
            elif kind == "delete":
                tree.pop(path)
            elif kind == "chmod":
                tree[path] = (tree[path][0], "100755" if act["execute_filemode"] else "100644")
            elif kind == "move":
                tree[path] = tree.pop(act["previous_path"])
        sha = self._new_commit(tree, [base], message.splitlines()[0])
        self.branches.setdefault(branch, {"sha": sha, "protected": False})["sha"] = sha
        return {"id": sha, "title": message.splitlines()[0]}

    def commit_files(self, branch: str, files: dict[str, str | None], message: str = "change") -> str:
        """Test helper: write {path: content} (None deletes) on a branch, creating it from main."""
        if branch not in self.branches:
            self.create_branch(branch, self.default_branch)
        tree = self.commits[self.branches[branch]["sha"]]["tree"]
        actions = []
        for p, c in files.items():
            if c is None:
                actions.append({"action": "delete", "file_path": p})
            else:
                actions.append({"action": "update" if p in tree else "create", "file_path": p, "content": c})
        return self.create_commit(branch, message, actions)["id"]

    @locked
    def get_commit(self, sha: str) -> dict | None:
        return {"id": sha, "title": self.commits[sha]["title"]} if sha in self.commits else None

    @locked
    def list_tags(self) -> list[dict]:
        return [{"name": n, "message": t["message"], "commit": {"id": t["target"]}}
                for n, t in sorted(self.tags.items())]

    @locked
    def get_tag(self, name: str) -> dict | None:
        t = self.tags.get(name)
        return {"name": name, "message": t["message"], "commit": {"id": t["target"]}} if t else None

    @locked
    def create_tag(self, name: str, ref: str, message: str | None = None) -> dict:
        self._record("create_tag", name, ref)
        if name in self.tags:
            raise ApiError("POST", "tags", 400, "Tag already exists")
        self.tags[name] = {"target": self._resolve(ref), "message": message or ""}
        return {"name": name}

    @locked
    def delete_tag(self, name: str) -> None:
        self._record("delete_tag", name)
        if name in self.releases:
            raise ApiError("DELETE", "tags", 400, "tag has a release")
        if self.tags.pop(name, None) is None:
            raise _nf(name)

    # -- releases ------------------------------------------------------------
    def _release_json(self, tag: str) -> dict:
        r = self.releases[tag]
        return {"tag_name": tag, "name": r["name"], "description": r["description"],
                "_links": {"self": f"https://gitlab.example/acme/shop/-/releases/{tag}"}}

    @locked
    def list_releases(self) -> list[dict]:
        return [self._release_json(t) for t in sorted(self.releases)]

    @locked
    def get_release(self, tag: str) -> dict | None:
        return self._release_json(tag) if tag in self.releases else None

    @locked
    def create_release(self, tag_name: str, name: str, description: str) -> dict:
        self._record("create_release", tag_name)
        if tag_name not in self.tags:
            raise ApiError("POST", "releases", 422, "tag does not exist")
        self.releases[tag_name] = {"name": name, "description": description}
        return self._release_json(tag_name)

    @locked
    def update_release(self, tag: str, **fields: Any) -> dict:
        self._record("update_release", tag, **fields)
        self.releases[tag].update(fields)
        return self._release_json(tag)

    @locked
    def delete_release(self, tag: str) -> None:
        self._record("delete_release", tag)
        if self.releases.pop(tag, None) is None:
            raise _nf(tag)

    # -- merge requests ------------------------------------------------------
    def _mr_json(self, iid: int) -> dict:
        m = self.mrs[iid]
        sha = self.branches[m["source_branch"]]["sha"] if m["source_branch"] in self.branches else m.get("sha")
        return {**m, "iid": iid, "sha": sha, "web_url": f"https://gitlab.example/acme/shop/-/merge_requests/{iid}"}

    @locked
    def list_merge_requests(self, **params: Any) -> list[dict]:
        out = [self._mr_json(i) for i in sorted(self.mrs)]
        if params.get("state"):
            out = [m for m in out if m["state"] == params["state"]]
        if params.get("updated_after"):
            out = [m for m in out if m["updated_at"] >= params["updated_after"]]
        if params.get("created_after"):
            out = [m for m in out if m["created_at"] >= params["created_after"]]
        return out

    @locked
    def get_merge_request(self, iid: int) -> dict | None:
        return self._mr_json(iid) if iid in self.mrs else None

    @locked
    def create_merge_request(self, source: str, target: str, title: str, description: str = "",
                             labels: list[str] | None = None, remove_source_branch: bool = True) -> dict:
        self._record("create_merge_request", source, target, title)
        iid = next(self._iid["mr"])
        self.mrs[iid] = {"title": title, "description": description, "state": "opened", "labels": labels or [],
                         "source_branch": source, "target_branch": target, "merge_commit_sha": None,
                         "created_at": now(), "updated_at": now(), "head_pipeline": None,
                         "detailed_merge_status": "mergeable"}
        self.mr_notes[iid] = []
        return self._mr_json(iid)

    @locked
    def update_merge_request(self, iid: int, **fields: Any) -> dict:
        self._record("update_merge_request", iid, **fields)
        m = self.mrs[iid]
        ev = fields.pop("state_event", None)
        if ev == "close":
            m["state"] = "closed"
        elif ev == "reopen":
            m["state"] = "opened"
        if "add_labels" in fields:
            m["labels"] = sorted(set(m["labels"]) | set(fields.pop("add_labels").split(",")))
        if "remove_labels" in fields:
            m["labels"] = sorted(set(m["labels"]) - set(fields.pop("remove_labels").split(",")))
        m.update(fields)
        m["updated_at"] = now()
        return self._mr_json(iid)

    @locked
    def merge_merge_request(self, iid: int, *, sha: str | None = None, merge_commit_message: str | None = None,
                            should_remove_source_branch: bool = True) -> dict:
        self._record("merge_merge_request", iid, sha=sha)
        m = self.mrs[iid]
        if m["state"] != "opened":
            raise ApiError("PUT", "merge", 405, "Method Not Allowed")
        src = self.branches[m["source_branch"]]["sha"]
        if sha and sha != src:
            raise ApiError("PUT", "merge", 409, "SHA does not match HEAD of source branch")
        tgt = self.branches[m["target_branch"]]["sha"]
        ancestors = set(self._ancestors(tgt))
        base = next(s for s in self._ancestors(src) if s in ancestors)
        tree = dict(self.commits[tgt]["tree"])
        bt, st = self.commits[base]["tree"], self.commits[src]["tree"]
        for p in set(bt) | set(st):
            if bt.get(p) != st.get(p):
                if st.get(p) is None:
                    tree.pop(p, None)
                else:
                    tree[p] = st[p]
        msg = merge_commit_message or f"Merge branch '{m['source_branch']}'"
        merge_sha = self._new_commit(tree, [tgt, src], msg.splitlines()[0])
        self.branches[m["target_branch"]]["sha"] = merge_sha
        m.update(state="merged", merge_commit_sha=merge_sha, sha=src, updated_at=now(),
                 merge_commit_message=msg)
        if should_remove_source_branch:
            self.branches.pop(m["source_branch"], None)
        for ref in re.findall(r"(?i)(?:closes|fixes|resolves)\s+#(\d+)", m.get("description", "")):
            if int(ref) in self.issues:
                self.issues[int(ref)].update(state="closed", updated_at=now())
        return self._mr_json(iid)

    @locked
    def list_mr_notes(self, iid: int) -> list[dict]:
        return list(self.mr_notes.get(iid, []))

    @locked
    def create_mr_note(self, iid: int, body: str) -> dict:
        self._record("create_mr_note", iid)
        note = {"id": next(self._seq), "body": body, "author": self.users["rewind-bot"]}
        self.mr_notes.setdefault(iid, []).append(note)
        return note

    @locked
    def mr_pipelines(self, iid: int) -> list[dict]:
        return [p for p in self.pipelines.values() if p.get("mr_iid") == iid]

    # -- issues --------------------------------------------------------------
    def _issue_json(self, iid: int) -> dict:
        i = self.issues[iid]
        return {**i, "iid": iid, "assignees": [self.users[u] for u in i["assignees"]],
                "web_url": f"https://gitlab.example/acme/shop/-/issues/{iid}"}

    def add_issue(self, title: str, description: str = "", labels: list[str] | None = None,
                  created_at: str | None = None) -> int:
        """Test helper (also what an agent's create_issue tool would do)."""
        with _lock:
            iid = next(self._iid["issue"])
            self.issues[iid] = {"title": title, "description": description, "state": "opened",
                                "labels": labels or [], "assignees": [], "confidential": False,
                                "created_at": created_at or now(), "updated_at": now()}
            self.issue_notes[iid] = []
            return iid

    @locked
    def list_issues(self, **params: Any) -> list[dict]:
        out = [self._issue_json(i) for i in sorted(self.issues)]
        if params.get("state"):
            out = [i for i in out if i["state"] == params["state"]]
        if params.get("updated_after"):
            out = [i for i in out if i["updated_at"] >= params["updated_after"]]
        return out

    @locked
    def get_issue(self, iid: int) -> dict | None:
        return self._issue_json(iid) if iid in self.issues else None

    @locked
    def update_issue(self, iid: int, **fields: Any) -> dict:
        self._record("update_issue", iid, **fields)
        i = self.issues[iid]
        ev = fields.pop("state_event", None)
        if ev == "close":
            i["state"] = "closed"
        elif ev == "reopen":
            i["state"] = "opened"
        if "add_labels" in fields:
            i["labels"] = sorted(set(i["labels"]) | set(fields.pop("add_labels").split(",")))
        if "remove_labels" in fields:
            i["labels"] = sorted(set(i["labels"]) - set(fields.pop("remove_labels").split(",")))
        if "assignee_ids" in fields:
            ids = fields.pop("assignee_ids")
            i["assignees"] = [u for u, info in self.users.items() if info["id"] in ids]
        i.update(fields)
        i["updated_at"] = now()
        return self._issue_json(iid)

    @locked
    def list_issue_notes(self, iid: int) -> list[dict]:
        return list(self.issue_notes.get(iid, []))

    @locked
    def create_issue_note(self, iid: int, body: str) -> dict:
        self._record("create_issue_note", iid)
        note = {"id": next(self._seq), "body": body, "author": self.users["rewind-bot"]}
        self.issue_notes.setdefault(iid, []).append(note)
        return note

    @locked
    def update_issue_note(self, iid: int, note_id: int, body: str) -> dict:
        for n in self.issue_notes[iid]:
            if n["id"] == note_id:
                n["body"] = body
                return n
        raise _nf(f"note {note_id}")

    @locked
    def related_merge_requests(self, iid: int) -> list[dict]:
        pat = re.compile(rf"#{iid}\b")
        return [self._mr_json(m) for m, d in self.mrs.items() if pat.search(d.get("description", ""))]

    # -- labels --------------------------------------------------------------
    @locked
    def list_labels(self) -> list[dict]:
        return [{"name": n, **v} for n, v in sorted(self.labels.items())]

    @locked
    def get_label(self, name: str) -> dict | None:
        return {"name": name, **self.labels[name]} if name in self.labels else None

    @locked
    def create_label(self, name: str, color: str, description: str = "") -> dict:
        self._record("create_label", name)
        if name in self.labels:
            raise ApiError("POST", "labels", 409, "Label already exists")
        self.labels[name] = {"color": color, "description": description}
        return {"name": name, **self.labels[name]}

    @locked
    def update_label(self, name: str, **fields: Any) -> dict:
        self._record("update_label", name)
        self.labels[name].update(fields)
        return {"name": name, **self.labels[name]}

    @locked
    def delete_label(self, name: str) -> None:
        self._record("delete_label", name)
        if self.labels.pop(name, None) is None:
            raise _nf(name)
        for coll in (self.issues, self.mrs):
            for item in coll.values():
                if name in item["labels"]:
                    item["labels"] = [x for x in item["labels"] if x != name]

    # -- feature flags -------------------------------------------------------
    def _flag_json(self, name: str) -> dict:
        return {"name": name, **self.flags[name]}

    @locked
    def list_feature_flags(self) -> list[dict]:
        return [self._flag_json(n) for n in sorted(self.flags)]

    @locked
    def get_feature_flag(self, name: str) -> dict | None:
        return self._flag_json(name) if name in self.flags else None

    def _strategies(self, raw: list[dict], existing: list[dict] | None = None) -> list[dict]:
        out = [dict(s) for s in existing or []]
        for s in raw:
            if s.get("_destroy"):
                out = [x for x in out if x["id"] != s["id"]]
                continue
            item = {"id": s.get("id") or next(self._seq), "name": s["name"], "parameters": s.get("parameters", {}),
                    "scopes": [{"id": next(self._seq), "environment_scope": sc["environment_scope"]}
                               for sc in s.get("scopes", [])]}
            out = [x for x in out if x["id"] != item["id"]] + [item]
        return out

    @locked
    def create_feature_flag(self, payload: dict) -> dict:
        self._record("create_feature_flag", payload["name"])
        if payload["name"] in self.flags:
            raise ApiError("POST", "feature_flags", 400, "name has already been taken")
        self.flags[payload["name"]] = {"active": payload.get("active", True),
                                       "description": payload.get("description", ""),
                                       "version": "new_version_flag",
                                       "strategies": self._strategies(payload.get("strategies", []))}
        return self._flag_json(payload["name"])

    @locked
    def update_feature_flag(self, name: str, payload: dict) -> dict:
        self._record("update_feature_flag", name, **payload)
        f = self.flags[name]
        if "strategies" in payload:
            f["strategies"] = self._strategies(payload["strategies"], f["strategies"])
        for k in ("active", "description"):
            if k in payload:
                f[k] = payload[k]
        return self._flag_json(name)

    @locked
    def delete_feature_flag(self, name: str) -> None:
        self._record("delete_feature_flag", name)
        if self.flags.pop(name, None) is None:
            raise _nf(name)

    def add_flag(self, name: str, active: bool, scopes: tuple[str, ...] = ("*",)) -> None:
        self.create_feature_flag({"name": name, "active": active, "strategies": [
            {"name": "default", "parameters": {}, "scopes": [{"environment_scope": s} for s in scopes]}]})

    # -- variables -----------------------------------------------------------
    @locked
    def list_variables(self) -> list[dict]:
        return [{"key": k, "environment_scope": s, **v} for (k, s), v in sorted(self.variables.items())]

    @locked
    def get_variable(self, key: str, scope: str = "*") -> dict | None:
        v = self.variables.get((key, scope))
        return {"key": key, "environment_scope": scope, **v} if v else None

    def set_variable(self, key: str, value: str | None, scope: str = "*", **meta: Any) -> None:
        with _lock:
            self.variables[(key, scope)] = {"value": value, "variable_type": "env_var", "protected": False,
                                            "masked": False, "raw": False, **meta}

    @locked
    def update_variable(self, key: str, scope: str, **fields: Any) -> dict:
        self._record("update_variable", key, scope)
        self.variables[(key, scope)].update(fields)
        return {"key": key, **self.variables[(key, scope)]}

    @locked
    def delete_variable(self, key: str, scope: str = "*") -> None:
        self._record("delete_variable", key, scope)
        if self.variables.pop((key, scope), None) is None:
            raise _nf(key)

    # -- pipelines, deployments, hooks ---------------------------------------
    def add_pipeline(self, ref: str, sha: str, status: str = "running", mr_iid: int | None = None,
                     jobs: list[dict] | None = None) -> int:
        with _lock:
            pid = next(self._seq)
            self.pipelines[pid] = {"id": pid, "ref": ref, "sha": sha, "status": status, "mr_iid": mr_iid,
                                   "web_url": f"https://gitlab.example/acme/shop/-/pipelines/{pid}"}
            self.jobs[pid] = jobs or []
            if mr_iid:
                self.mrs[mr_iid]["head_pipeline"] = {"id": pid, "sha": sha, "status": status}
            return pid

    def set_pipeline_status(self, pid: int, status: str) -> None:
        with _lock:
            self.pipelines[pid]["status"] = status
            iid = self.pipelines[pid].get("mr_iid")
            if iid:
                self.mrs[iid]["head_pipeline"]["status"] = status

    @locked
    def list_pipelines(self, **params: Any) -> list[dict]:
        out = sorted(self.pipelines.values(), key=lambda p: -p["id"])
        for k in ("ref", "sha"):
            if params.get(k):
                out = [p for p in out if p[k] == params[k]]
        return out

    @locked
    def get_pipeline(self, pipeline_id: int) -> dict | None:
        return self.pipelines.get(pipeline_id)

    @locked
    def list_pipeline_jobs(self, pipeline_id: int, **params: Any) -> list[dict]:
        jobs = self.jobs.get(pipeline_id, [])
        scope = params.get("scope")
        return [j for j in jobs if not scope or j["status"] == scope]

    @locked
    def create_deployment(self, environment: str, sha: str, ref: str, status: str = "success") -> dict:
        self._record("create_deployment", environment, sha)
        d = {"id": next(self._seq), "environment": environment, "sha": sha, "ref": ref, "status": status}
        self.deployments.append(d)
        return d

    @locked
    def list_hooks(self) -> list[dict]:
        return list(self.hooks)

    @locked
    def create_hook(self, payload: dict) -> dict:
        hook = {"id": next(self._seq), **payload}
        self.hooks.append(hook)
        return hook

    @locked
    def update_hook(self, hook_id: int, payload: dict) -> dict:
        for h in self.hooks:
            if h["id"] == hook_id:
                h.update(payload)
                return h
        raise _nf(f"hook {hook_id}")

    # -- Duo Agent Platform --------------------------------------------------
    PRIVILEGES = ["read_write_files", "read_only_gitlab", "read_write_gitlab", "run_commands", "use_git",
                  "run_mcp_tools", "start_flows", "read_only_files"]

    @locked
    def agent_privileges(self) -> list[dict]:
        return [{"id": i + 1, "name": n.upper()} for i, n in enumerate(self.PRIVILEGES)]

    @locked
    def start_workflow(self, payload: dict) -> dict:
        self._record("start_workflow", **payload)
        wid = next(self._seq)
        self.workflows[wid] = {"id": wid, "status": "running", "payload": payload}
        return {"id": wid, "status": "created"}

    @locked
    def workflow_status(self, workflow_id: int | str) -> dict:
        w = self.workflows.get(int(workflow_id))
        if not w:
            raise _nf(f"workflow {workflow_id}")
        return {"id": w["id"], "status": w["status"]}

    @locked
    def workflow_trace(self, workflow_id: int | str) -> str:
        return ""


class FakeCloudRun:
    def __init__(self, environments: tuple[str, ...] = ("staging", "production")):
        self.services = {env: f"shop-{env}" for env in environments}
        self._rev = itertools.count(1)
        self._state: dict[str, dict] = {}
        for env in environments:
            self.deploy(env)
        self.calls: list[tuple] = []

    def deploy(self, env: str) -> str:
        with _lock:
            rev = f"shop-{env}-{next(self._rev):05d}"
            self._state[env] = {"latest": rev, "traffic": [[rev, 100]]}
            return rev

    def serving(self, env: str) -> dict | None:
        with _lock:
            s = self._state.get(env)
            if not s:
                return None
            return {"service": self.services[env], "serving": sorted([list(t) for t in s["traffic"]]),
                    "_latest_ready": s["latest"], "_uri": f"https://shop-{env}.run.app"}

    def pin_traffic(self, env: str, split: list[list[Any]], **_: Any) -> None:
        with _lock:
            self.calls.append(("pin_traffic", env, split))
            self._state[env]["traffic"] = [list(t) for t in split]
