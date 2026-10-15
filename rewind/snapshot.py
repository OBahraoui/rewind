"""Capture and compare project state."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from rewind.models import Change, ChangeSet, Snapshot, comparable, new_id
from rewind.policy import Policy
from rewind.resources import HANDLERS, Context

log = logging.getLogger("rewind.snapshot")


def load_meta(ctx: Context) -> None:
    if "default_branch" not in ctx.meta:
        p = ctx.gitlab.project()
        ctx.meta.update(default_branch=p["default_branch"], web_url=p.get("web_url", ""),
                        path=p.get("path_with_namespace", ""))


def take_snapshot(ctx: Context, project_id: str, reason: str = "checkpoint") -> Snapshot:
    """Capture every enabled resource kind in parallel. A failing kind becomes a recorded blind spot."""
    started = time.monotonic()
    load_meta(ctx)
    snap = Snapshot(id=new_id("snap"), project_id=project_id, reason=reason, meta=dict(ctx.meta))
    handlers = [h for h in HANDLERS.values() if h.enabled(ctx)]

    def capture(h):
        try:
            return h.kind, h.snapshot(ctx), None
        except Exception as exc:
            log.exception("snapshot failed", extra={"kind": h.kind})
            return h.kind, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=len(handlers) or 1) as pool:
        for kind, data, err in pool.map(capture, handlers):
            if err:
                snap.errors[kind] = err
            else:
                snap.resources[kind] = data
    snap.duration_ms = int((time.monotonic() - started) * 1000)
    return snap


def diff(before: Snapshot, after: Snapshot, policy: Policy, checkpoint_id: str = "") -> ChangeSet:
    cs = ChangeSet(checkpoint_id=checkpoint_id or before.id, from_snapshot=before.id, to_snapshot=after.id)
    kinds = set(before.resources) | set(after.resources) | set(before.errors) | set(after.errors)
    for kind in sorted(kinds):
        if kind in before.errors or kind in after.errors:
            cs.blind_spots[kind] = before.errors.get(kind) or after.errors[kind]
            continue
        if kind not in before.resources or kind not in after.resources:
            cs.blind_spots[kind] = "not captured in both snapshots"
            continue
        handler = HANDLERS[kind]
        b_all, a_all = before.resources[kind], after.resources[kind]
        for key in sorted(set(b_all) | set(a_all), key=_natural):
            b, a = b_all.get(key), a_all.get(key)
            if comparable(b) == comparable(a):
                continue
            if b is None:
                created_at = (a or {}).get("_created_at", "")
                if created_at and created_at < before.taken_at.isoformat():
                    # Existed at checkpoint time but outside the captured window: no baseline.
                    change = Change(id=f"{kind}:{key}", kind=kind, key=key, op="updated",
                                    before={"_unknown_baseline": True}, after=a, fields=["*"])
                else:
                    change = Change(id=f"{kind}:{key}", kind=kind, key=key, op="created", after=a)
            elif a is None:
                change = Change(id=f"{kind}:{key}", kind=kind, key=key, op="deleted", before=b)
            else:
                cb, ca = comparable(b) or {}, comparable(a) or {}
                fields = sorted(k for k in set(cb) | set(ca) if cb.get(k) != ca.get(k))
                change = Change(id=f"{kind}:{key}", kind=kind, key=key, op="updated", before=b, after=a,
                                fields=fields)
            change.reversibility, change.reason = policy.classify_change(change)
            change.summary = handler.summarize(change) if "_unknown_baseline" not in (change.before or {}) \
                else f"{kind} {key} changed, but it was outside the checkpoint window"
            cs.changes.append(change)
    return cs


def _natural(key: str):
    return (0, int(key), "") if key.isdigit() else (1, 0, key)
