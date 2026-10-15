"""Collaboration state: issues, merge requests, labels, releases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rewind.models import Change
from rewind.resources.base import Context, Handler, label_delta, ts


def _labels(ctx: Context, labels: list[str]) -> list[str]:
    return sorted(lbl for lbl in labels if not ctx.policy.ignore.label(lbl))


def _since(ctx: Context) -> str:
    return (datetime.now(UTC) - timedelta(days=ctx.policy.snapshot.lookback_days)).isoformat()


class IssueHandler(Handler):
    kind = "issue"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        items = {i["iid"]: i for i in ctx.gitlab.list_issues(state="opened")}
        items.update({i["iid"]: i for i in ctx.gitlab.list_issues(state="closed", updated_after=_since(ctx))})
        return {str(iid): self._state(ctx, i) for iid, i in items.items()}

    def _state(self, ctx: Context, i: dict) -> dict[str, Any]:
        assignees = i.get("assignees") or []
        return {
            "title": i["title"],
            "state": i["state"],
            "labels": _labels(ctx, i.get("labels") or []),
            "assignees": sorted(a["username"] for a in assignees),
            "confidential": bool(i.get("confidential")),
            "_assignee_ids": sorted(a["id"] for a in assignees),
            "_created_at": ts(i.get("created_at")),
            "_web_url": i.get("web_url", ""),
        }

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        i = ctx.gitlab.get_issue(int(key))
        return self._state(ctx, i) if i else None

    def summarize(self, change: Change) -> str:
        title = (change.after or change.before or {}).get("title", "")
        if change.op == "updated":
            return f"issue #{change.key} changed {', '.join(change.fields)} — {title}"
        return f"issue #{change.key} {change.op} — {title}"

    def action(self, change: Change) -> str:
        if change.op == "created":
            return f"close issue #{change.key} with a note"
        return f"restore {', '.join(change.fields)} on issue #{change.key}"

    def priority(self, change: Change) -> int:
        return 70

    def is_restored(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> bool:
        if change.op == "created":
            return live is None or live["state"] == "closed"
        return super().is_restored(ctx, change, live)

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        iid = int(change.key)
        if change.op == "created":
            ctx.gitlab.create_issue_note(
                iid, f"⏪ Closed by Rewind: this issue was created by an agent run that was rewound "
                     f"to checkpoint `{ctx.checkpoint_id}`.\n\n{ctx.marker(change)}")
            ctx.gitlab.update_issue(iid, state_event="close")
            return f"closed issue #{iid}"
        assert change.before and live
        b = change.before
        fields: dict[str, Any] = {}
        if b["state"] != live["state"]:
            fields["state_event"] = "reopen" if b["state"] == "opened" else "close"
        if b["title"] != live["title"]:
            fields["title"] = b["title"]
        add, remove = label_delta(live["labels"], b["labels"])
        if add:
            fields["add_labels"] = ",".join(add)
        if remove:
            fields["remove_labels"] = ",".join(remove)
        if b["assignees"] != live["assignees"]:
            fields["assignee_ids"] = b["_assignee_ids"] or [0]
        if b["confidential"] != live["confidential"]:
            fields["confidential"] = b["confidential"]
        ctx.gitlab.update_issue(iid, **fields)
        return f"issue #{iid}: restored {', '.join(sorted(fields))}"


class MergeRequestHandler(Handler):
    kind = "merge_request"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        items = {m["iid"]: m for m in ctx.gitlab.list_merge_requests(state="opened")}
        for state in ("merged", "closed"):
            items.update({m["iid"]: m for m in ctx.gitlab.list_merge_requests(state=state, updated_after=_since(ctx))})
        return {str(iid): self._state(ctx, m) for iid, m in items.items()
                if not ctx.policy.ignore.branch(m.get("source_branch", ""))}

    def _state(self, ctx: Context, m: dict) -> dict[str, Any]:
        return {
            "title": m["title"],
            "state": m["state"],
            "labels": _labels(ctx, m.get("labels") or []),
            "target_branch": m["target_branch"],
            "_source_branch": m.get("source_branch", ""),
            "_merge_commit_sha": m.get("merge_commit_sha") or m.get("squash_commit_sha") or "",
            "_sha": m.get("sha") or "",
            "_created_at": ts(m.get("created_at")),
            "_web_url": m.get("web_url", ""),
        }

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        m = ctx.gitlab.get_merge_request(int(key))
        return self._state(ctx, m) if m else None

    def summarize(self, change: Change) -> str:
        s = change.after or change.before or {}
        if change.op == "created":
            how = {"merged": "opened and merged", "closed": "opened and closed"}.get(s.get("state", ""), "opened")
            return f"MR !{change.key} {how} — {s.get('title', '')}"
        if change.op == "updated":
            if (change.after or {}).get("state") == "merged" and (change.before or {}).get("state") != "merged":
                return f"MR !{change.key} merged — {s.get('title', '')}"
            return f"MR !{change.key} changed {', '.join(change.fields)}"
        return f"MR !{change.key} deleted"

    def action(self, change: Change) -> str:
        state = (change.after or {}).get("state")
        if state == "merged":
            return f"annotate merged MR !{change.key} (code is restored by the repository restore)"
        if change.op == "created":
            return f"close MR !{change.key} with a note"
        return f"restore {', '.join(change.fields)} on MR !{change.key}"

    def priority(self, change: Change) -> int:
        return 58

    def _annotated(self, ctx: Context, change: Change) -> bool:
        marker = ctx.marker(change)
        return any(marker in (n.get("body") or "") for n in ctx.gitlab.list_mr_notes(int(change.key)))

    def is_restored(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> bool:
        if live is None:
            return change.before is None
        if live["state"] == "merged" and (change.before or {}).get("state") != "merged":
            return self._annotated(ctx, change)
        if change.op == "created":
            return live["state"] == "closed"
        return super().is_restored(ctx, change, live)

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        iid = int(change.key)
        assert live is not None
        if live["state"] == "merged":
            ctx.gitlab.create_mr_note(
                iid, f"⏪ Rewound: the changes merged here were reverted by Rewind to checkpoint "
                     f"`{ctx.checkpoint_id}`. A merge can't be undone, so this MR stays merged as an audit "
                     f"record.\n\n{ctx.marker(change)}")
            return f"annotated merged MR !{iid}"
        if change.op == "created":
            ctx.gitlab.create_mr_note(
                iid, f"⏪ Closed by Rewind: rewound to checkpoint `{ctx.checkpoint_id}`.\n\n{ctx.marker(change)}")
            ctx.gitlab.update_merge_request(iid, state_event="close")
            return f"closed MR !{iid}"
        assert change.before
        b = change.before
        fields: dict[str, Any] = {}
        if b["state"] != live["state"]:
            fields["state_event"] = "reopen" if b["state"] == "opened" else "close"
        if b["title"] != live["title"]:
            fields["title"] = b["title"]
        if b["target_branch"] != live["target_branch"]:
            fields["target_branch"] = b["target_branch"]
        add, remove = label_delta(live["labels"], b["labels"])
        if add:
            fields["add_labels"] = ",".join(add)
        if remove:
            fields["remove_labels"] = ",".join(remove)
        ctx.gitlab.update_merge_request(iid, **fields)
        return f"MR !{iid}: restored {', '.join(sorted(fields))}"


class LabelHandler(Handler):
    kind = "label"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return {lbl["name"]: self._state(lbl) for lbl in ctx.gitlab.list_labels()
                if not ctx.policy.ignore.label(lbl["name"])}

    @staticmethod
    def _state(lbl: dict) -> dict[str, Any]:
        return {"color": (lbl.get("color") or "").lower(), "description": lbl.get("description") or ""}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        lbl = ctx.gitlab.get_label(key)
        return self._state(lbl) if lbl else None

    def summarize(self, change: Change) -> str:
        return f"label ~{change.key} {change.op}"

    def action(self, change: Change) -> str:
        return {"created": f"delete label ~{change.key}", "deleted": f"recreate label ~{change.key}",
                "updated": f"restore label ~{change.key}"}[change.op]

    def priority(self, change: Change) -> int:
        # Recreate before issues/MRs re-add it; delete after they drop it.
        return 90 if change.op == "created" else 30

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        if change.before is None:
            ctx.gitlab.delete_label(change.key)
            return f"deleted label ~{change.key}"
        if live is None:
            ctx.gitlab.create_label(change.key, change.before["color"], change.before["description"])
            return f"recreated label ~{change.key}"
        ctx.gitlab.update_label(change.key, color=change.before["color"], description=change.before["description"])
        return f"restored label ~{change.key}"


class ReleaseHandler(Handler):
    kind = "release"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return {r["tag_name"]: self._state(r) for r in ctx.gitlab.list_releases()}

    @staticmethod
    def _state(r: dict) -> dict[str, Any]:
        return {"name": r.get("name") or "", "description": r.get("description") or "",
                "_web_url": (r.get("_links") or {}).get("self", "")}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        r = ctx.gitlab.get_release(key)
        return self._state(r) if r else None

    def summarize(self, change: Change) -> str:
        return f"release {change.key} {change.op}"

    def action(self, change: Change) -> str:
        return {"created": f"delete release {change.key}", "deleted": f"recreate release {change.key}",
                "updated": f"restore release {change.key} notes"}[change.op]

    def priority(self, change: Change) -> int:
        return 50

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        if change.before is None:
            ctx.gitlab.delete_release(change.key)
            return f"deleted release {change.key}"
        if live is None:
            ctx.gitlab.create_release(change.key, change.before["name"], change.before["description"])
            return f"recreated release {change.key}"
        ctx.gitlab.update_release(change.key, name=change.before["name"], description=change.before["description"])
        return f"restored release {change.key}"
