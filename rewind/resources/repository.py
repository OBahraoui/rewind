"""Code: the default branch, other branches and tags."""

from __future__ import annotations

import base64
from typing import Any

from rewind.http import ApiError
from rewind.models import Change
from rewind.resources.base import Context, Handler

REWIND_MARKER = "[rewind]"  # CI skips deploy jobs for commits carrying this marker


class RepositoryHandler(Handler):
    """The default branch. Restored by one commit that returns the tree to the checkpoint.

    The agent's commits stay in history (audit trail); only the *content* is put back.
    This works for any number of commits and merges, and cannot conflict.
    """

    kind = "repository"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        state = self.fetch(ctx, "default")
        return {"default": state} if state else {}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        branch = ctx.meta["default_branch"]
        b = ctx.gitlab.get_branch(branch)
        if not b:
            return None
        return {"sha": b["commit"]["id"], "_branch": branch, "_title": b["commit"].get("title", "")}

    def summarize(self, change: Change) -> str:
        b, a = change.before or {}, change.after or {}
        return f"default branch moved {b.get('sha', '?')[:8]} → {a.get('sha', '?')[:8]}"

    def action(self, change: Change) -> str:
        return f"commit restoring the default branch tree to {(change.before or {}).get('sha', '?')[:8]}"

    def priority(self, change: Change) -> int:
        return 40

    def is_restored(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> bool:
        if not live or not change.before:
            return live == change.before
        if live["sha"] == change.before["sha"]:
            return True
        cmp = ctx.gitlab.compare(change.before["sha"], live["sha"])
        return not cmp.get("diffs") and not cmp.get("compare_timeout")

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        assert change.before and live
        target, head = change.before["sha"], live["sha"]
        branch = ctx.meta["default_branch"]
        actions = restore_actions(ctx, target, head)
        if not actions:
            return "tree already matches the checkpoint"
        title = f"Rewind {ctx.rewind_id}: restore {branch} to checkpoint {ctx.checkpoint_id}"
        message = f"{title}\n\nRestores {len(actions)} path(s) to {target[:12]}.\n\n{REWIND_MARKER}"
        if ctx.restore_mode == "direct":
            commit = ctx.gitlab.create_commit(branch, message, actions)
            return f"restore commit {commit['id'][:8]} on {branch} ({len(actions)} paths)"
        return self._restore_via_mr(ctx, branch, head, title, message, actions)

    def _restore_via_mr(self, ctx: Context, branch: str, head: str, title: str, message: str,
                        actions: list[dict]) -> str:
        work = f"rewind/{ctx.rewind_id}"
        ctx.register_own("branch", work)
        if not ctx.gitlab.get_branch(work):
            ctx.gitlab.create_branch(work, head)
        commit = ctx.gitlab.create_commit(work, message, actions)
        mr = ctx.gitlab.create_merge_request(
            work, branch, title,
            description=f"Automatic restore by Rewind.\n\nCheckpoint `{ctx.checkpoint_id}` · rewind `{ctx.rewind_id}`",
            labels=["rewind:restore"],
        )
        ctx.register_own("merge_request", mr["iid"])
        try:
            merged = merge_when_mergeable(ctx, mr["iid"], commit["id"], f"Merge {title}\n\n{REWIND_MARKER}")
        except RuntimeError as exc:
            # e.g. "pipelines must succeed" is on: don't wait minutes with production code wrong.
            ctx.gitlab.create_mr_note(mr["iid"], f"Rewind could not merge this restore immediately ({exc}); "
                                                 f"it committed the same restore directly to `{branch}` instead.")
            ctx.gitlab.update_merge_request(mr["iid"], state_event="close")
            ctx.gitlab.delete_branch(work)
            direct = ctx.gitlab.create_commit(branch, message, actions)
            return f"restore commit {direct['id'][:8]} on {branch} ({len(actions)} paths; MR merge unavailable)"
        return f"restore commit {commit['id'][:8]} merged via !{merged['iid']} ({len(actions)} paths)"


def merge_when_mergeable(ctx: Context, iid: int, sha: str, message: str, attempts: int = 20) -> dict:
    """Merge right after creation: GitLab needs a moment to compute mergeability."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return ctx.gitlab.merge_merge_request(iid, sha=sha, merge_commit_message=message)
        except ApiError as exc:
            # 405/406/422 while GitLab is still "checking"; 409 if sha moved (fatal).
            if exc.status == 409:
                raise
            last = exc
            ctx.sleep(1.5)
    raise RuntimeError(f"could not merge !{iid}: {last}")


def restore_actions(ctx: Context, target: str, head: str) -> list[dict]:
    """Commit actions that make `head`'s tree equal to `target`'s tree."""
    cmp = ctx.gitlab.compare(target, head)
    if cmp.get("compare_timeout"):
        raise RuntimeError("GitLab compare timed out; change set too large to restore safely")
    actions: list[dict] = []
    for d in cmp.get("diffs", []):
        old, new = d["old_path"], d["new_path"]
        if d.get("a_mode") == "160000" or d.get("b_mode") == "160000":
            raise RuntimeError(f"submodule change at {new} is not supported")
        if d.get("new_file"):
            actions.append({"action": "delete", "file_path": new})
            continue
        if d.get("renamed_file"):
            actions.append({"action": "delete", "file_path": new})
            actions.append(_content_action(ctx, "create", old, target, d.get("a_mode")))
            continue
        if d.get("deleted_file"):
            actions.append(_content_action(ctx, "create", old, target, d.get("a_mode")))
            continue
        actions.append(_content_action(ctx, "update", old, target, d.get("a_mode")))
        if d.get("a_mode") and d.get("b_mode") and d["a_mode"] != d["b_mode"]:
            actions.append({"action": "chmod", "file_path": old, "execute_filemode": d["a_mode"] == "100755"})
    return actions


def _content_action(ctx: Context, action: str, path: str, ref: str, mode: str | None) -> dict:
    raw = ctx.gitlab.get_file_raw(path, ref)
    if raw is None:
        raise RuntimeError(f"{path} missing at {ref[:8]}")
    try:
        out = {"action": action, "file_path": path, "content": raw.decode("utf-8"), "encoding": "text"}
    except UnicodeDecodeError:
        out = {"action": action, "file_path": path, "content": base64.b64encode(raw).decode(), "encoding": "base64"}
    if action == "create" and mode == "100755":
        out["execute_filemode"] = True
    return out


class BranchHandler(Handler):
    kind = "branch"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        default = ctx.meta["default_branch"]
        return {
            b["name"]: self._state(b)
            for b in ctx.gitlab.list_branches()
            if b["name"] != default and not ctx.is_own("branch", b["name"])
        }

    @staticmethod
    def _state(b: dict) -> dict[str, Any]:
        return {"sha": b["commit"]["id"], "protected": bool(b.get("protected"))}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        b = ctx.gitlab.get_branch(key)
        return self._state(b) if b else None

    def summarize(self, change: Change) -> str:
        return {"created": f"branch {change.key} created", "deleted": f"branch {change.key} deleted",
                "updated": f"branch {change.key} moved"}[change.op]

    def action(self, change: Change) -> str:
        if change.op == "created":
            return f"delete branch {change.key}"
        return f"set branch {change.key} to {(change.before or {}).get('sha', '?')[:8]}"

    def priority(self, change: Change) -> int:
        return 60

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        if live:
            ctx.gitlab.delete_branch(change.key)
        if change.before:
            ctx.gitlab.create_branch(change.key, change.before["sha"])
            return f"branch {change.key} at {change.before['sha'][:8]}"
        return f"branch {change.key} deleted"


class TagHandler(Handler):
    kind = "tag"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return {t["name"]: self._state(t) for t in ctx.gitlab.list_tags()}

    @staticmethod
    def _state(t: dict) -> dict[str, Any]:
        return {"target": t["commit"]["id"], "message": t.get("message") or ""}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        t = ctx.gitlab.get_tag(key)
        return self._state(t) if t else None

    def summarize(self, change: Change) -> str:
        return f"tag {change.key} {change.op}"

    def action(self, change: Change) -> str:
        return f"delete tag {change.key}" if change.op == "created" else f"recreate tag {change.key}"

    def priority(self, change: Change) -> int:
        return 45 if change.op == "deleted" else 55

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        if live:
            ctx.gitlab.delete_tag(change.key)
        if change.before:
            ctx.gitlab.create_tag(change.key, change.before["target"], change.before.get("message") or None)
            return f"tag {change.key} recreated at {change.before['target'][:8]}"
        return f"tag {change.key} deleted"
