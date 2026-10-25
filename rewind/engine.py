"""The Rewind engine: checkpoint, diff, plan, rewind, verify, undo.

Rewind guarantees:
  * Idempotent: every step first checks whether the live object already matches the
    target, so re-running a rewind is a no-op.
  * Conflict-safe: a step only acts if the live object still matches what the plan
    saw; if someone changed it since, the step reports a conflict and leaves it alone.
  * Ordered for mitigation: production traffic and flags first, then code, then
    collaboration records.
  * Undoable: before acting it takes a pre-rewind checkpoint, so a rewind can itself
    be rewound (`undo_last`).
  * Honest: irreversible changes are declared, never "undone".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from rewind.models import (
    Change,
    ChangeSet,
    Checkpoint,
    Outcome,
    Reversibility,
    RewindReport,
    Snapshot,
    StepResult,
    Verification,
    new_id,
    utcnow,
)
from rewind.resources import HANDLERS, Context
from rewind.snapshot import diff, load_meta, take_snapshot
from rewind.store import Store

log = logging.getLogger("rewind.engine")

MITIGATION_PRIORITY = 30  # handlers below this (traffic, flags) stop user impact


class RewindError(RuntimeError):
    pass


class Rewind:
    def __init__(self, ctx: Context, store: Store, project_id: str):
        self.ctx, self.store, self.project_id = ctx, store, str(project_id)
        self._prefix = f"projects/{_slug(self.project_id)}"
        saved = self.store.get(f"{self._prefix}/own.json") or {}
        for kind, keys in saved.items():
            for key in keys:
                self.ctx.register_own(kind, key)

    def register_own(self, kind: str, key: str | int) -> None:
        """Record an object Rewind created (e.g. an incident) so snapshots never count it as a change."""
        self.ctx.register_own(kind, key)
        self._save_own()

    def _save_own(self) -> None:
        self.store.put(f"{self._prefix}/own.json", {k: sorted(v) for k, v in self.ctx.own.items()})

    # -- persistence ---------------------------------------------------------
    def _save_snapshot(self, snap: Snapshot) -> None:
        self.store.put(f"{self._prefix}/snapshots/{snap.id}.json", snap.model_dump(mode="json"))

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        data = self.store.get(f"{self._prefix}/snapshots/{snapshot_id}.json")
        if data is None:
            raise RewindError(f"snapshot {snapshot_id} not found")
        return Snapshot.model_validate(data)

    def _save_checkpoint(self, cp: Checkpoint) -> None:
        self.store.put(f"{self._prefix}/checkpoints/{cp.id}.json", cp.model_dump(mode="json"))

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        data = self.store.get(f"{self._prefix}/checkpoints/{checkpoint_id}.json")
        if data is None:
            raise RewindError(f"checkpoint {checkpoint_id} not found")
        return Checkpoint.model_validate(data)

    def list_checkpoints(self, kind: str | None = None) -> list[Checkpoint]:
        keys = self.store.list(f"{self._prefix}/checkpoints/")
        cps = [Checkpoint.model_validate(self.store.get(k)) for k in keys]
        cps = [c for c in cps if kind is None or c.kind == kind]
        return sorted(cps, key=lambda c: c.created_at, reverse=True)

    def save_report(self, report: RewindReport) -> None:
        self.store.put(f"{self._prefix}/rewinds/{report.id}.json", report.model_dump(mode="json"))

    def get_report(self, report_id: str) -> RewindReport:
        data = self.store.get(f"{self._prefix}/rewinds/{report_id}.json")
        if data is None:
            raise RewindError(f"rewind {report_id} not found")
        return RewindReport.model_validate(data)

    def list_reports(self) -> list[RewindReport]:
        keys = self.store.list(f"{self._prefix}/rewinds/")
        reports = [RewindReport.model_validate(self.store.get(k)) for k in keys]
        return sorted(reports, key=lambda r: r.started_at, reverse=True)

    # -- checkpoint & diff ---------------------------------------------------
    def snapshot(self, reason: str = "observe") -> Snapshot:
        return take_snapshot(self.ctx, self.project_id, reason)

    def checkpoint(self, label: str = "", *, run_id: str | None = None, issue_iid: int | None = None,
                   kind: str = "checkpoint") -> Checkpoint:
        snap = take_snapshot(self.ctx, self.project_id, kind)
        self._save_snapshot(snap)
        repo = snap.resources.get("repository", {}).get("default", {})
        serving = {env: s["serving"][0][0] for env, s in snap.resources.get("cloud_run", {}).items()
                   if s.get("serving")}
        prefix = {"checkpoint": "cp", "pre-rewind": "pre", "step": "step"}[kind]
        cp = Checkpoint(id=new_id(prefix), project_id=self.project_id,
                        snapshot_id=snap.id, kind=kind, label=label, run_id=run_id, issue_iid=issue_iid,
                        default_branch=snap.meta.get("default_branch", ""), default_sha=repo.get("sha", ""),
                        serving=serving)
        self._save_checkpoint(cp)
        log.info("checkpoint", extra={"checkpoint": cp.id, "snapshot_ms": snap.duration_ms,
                                      "blind_spots": list(snap.errors)})
        return cp

    def diff(self, checkpoint_id: str, against: Snapshot | None = None) -> ChangeSet:
        cp = self.get_checkpoint(checkpoint_id)
        before = self.load_snapshot(cp.snapshot_id)
        after = against or take_snapshot(self.ctx, self.project_id, "diff")
        return diff(before, after, self.ctx.policy, checkpoint_id)

    # -- rewind --------------------------------------------------------------
    @staticmethod
    def ordered(changes: list[Change]) -> list[Change]:
        return sorted(changes, key=lambda c: (HANDLERS[c.kind].priority(c), c.kind, c.key))

    def plan(self, checkpoint_id: str) -> RewindReport:
        """Dry run: the exact inverse operations a rewind would apply, in order."""
        return self.rewind(checkpoint_id, dry_run=True)

    def rewind(self, checkpoint_id: str, *, dry_run: bool = False, reason: str = "manual",
               trigger: dict | None = None, code_scope: str = "",
               only: Callable[[Change], bool] | None = None) -> RewindReport:
        """`code_scope`: revert only code the run changed up to this commit; keep later commits by others.
        `only`: undo just the changes it accepts (e.g. a run's own); the rest is left alone, listed in the
        report's trigger as `left_alone`, and not counted as drift."""
        cp = self.get_checkpoint(checkpoint_id)
        report = RewindReport(id=new_id("rw"), project_id=self.project_id, checkpoint_id=cp.id,
                              reason=reason, trigger={**(trigger or {}), **({"code_scope": code_scope}
                                                                         if code_scope else {})},
                              dry_run=dry_run)
        ctx = replace(self.ctx, rewind_id=report.id, checkpoint_id=cp.id, checkpoint_sha=cp.default_sha,
                      code_scope=code_scope)
        load_meta(ctx)
        started = time.monotonic()

        if dry_run:
            report.change_set = self._keep(report, self.diff(cp.id), only)
            for c in self.ordered(report.change_set.changes):
                report.steps.append(self._planned(c))
            report.finished_at = utcnow()
            return report

        with self.store.lock(f"rewind-{_slug(self.project_id)}", owner=report.id):
            pre = self.checkpoint(f"before {report.id}", kind="pre-rewind", run_id=cp.run_id,
                                  issue_iid=cp.issue_iid)
            report.pre_rewind_checkpoint_id = pre.id
            before = self.load_snapshot(cp.snapshot_id)
            now = self.load_snapshot(pre.snapshot_id)
            report.change_set = self._keep(report, diff(before, now, ctx.policy, cp.id), only)
            self.save_report(report)  # visible as "in progress" immediately

            for change in self.ordered(report.change_set.changes):
                step = self._apply(ctx, change)
                report.steps.append(step)
                # Mitigated = production traffic and flags are back; code cleanup can follow.
                if HANDLERS[change.kind].priority(change) < MITIGATION_PRIORITY and \
                        step.outcome in (Outcome.RESTORED, Outcome.ALREADY):
                    report.mitigated_after_s = round(time.monotonic() - started, 2)
                self.save_report(report)

            self._save_own()  # restore branches/MRs created during this rewind
            report.verification = self.verify(ctx, before, report.change_set,
                                              left_alone=set(report.trigger.get("left_alone", [])))
            report.finished_at = utcnow()
            self.save_report(report)
        log.info("rewind finished", extra={"rewind": report.id, "zero_drift": report.zero_drift,
                                           "seconds": round(time.monotonic() - started, 2)})
        return report

    @staticmethod
    def _keep(report: RewindReport, change_set: ChangeSet, only: Callable[[Change], bool] | None) -> ChangeSet:
        if only is None:
            return change_set
        left = [c.id for c in change_set.changes if not only(c)]
        if left:
            report.trigger["left_alone"] = left
        return change_set.model_copy(update={"changes": [c for c in change_set.changes if only(c)]})

    def _planned(self, change: Change) -> StepResult:
        handler = HANDLERS[change.kind]
        if change.reversibility is Reversibility.IRREVERSIBLE:
            return StepResult(change_id=change.id, kind=change.kind, key=change.key, action="none",
                              outcome=Outcome.IRREVERSIBLE, detail=change.reason)
        return StepResult(change_id=change.id, kind=change.kind, key=change.key, action=handler.action(change),
                          outcome=Outcome.PLANNED)

    def _apply(self, ctx: Context, change: Change) -> StepResult:
        handler = HANDLERS[change.kind]
        t0 = time.monotonic()
        base = {"change_id": change.id, "kind": change.kind, "key": change.key}
        if change.reversibility is Reversibility.IRREVERSIBLE:
            return StepResult(**base, action="none", outcome=Outcome.IRREVERSIBLE, detail=change.reason)
        action = handler.action(change)
        try:
            live = handler.fetch(ctx, change.key)
            if handler.is_restored(ctx, change, live):
                outcome, detail = Outcome.ALREADY, "already at the checkpoint state"
            elif not handler.can_apply(ctx, change, live):
                outcome, detail = Outcome.CONFLICT, "changed by someone else since the plan; left untouched"
            else:
                detail = handler.apply(ctx, change, live)
                outcome = (Outcome.RESTORED if change.reversibility is Reversibility.REVERSIBLE
                           else Outcome.COMPENSATED)
        except Exception as exc:
            log.exception("inverse failed", extra={"change": change.id})
            outcome, detail = Outcome.FAILED, f"{type(exc).__name__}: {exc}"[:500]
        step = StepResult(**base, action=action, outcome=outcome, detail=detail,
                          duration_ms=int((time.monotonic() - t0) * 1000))
        log.info("rewind step", extra={"rewind": ctx.rewind_id, "change": change.id, "outcome": outcome.value,
                                       "duration_ms": step.duration_ms})
        return step

    def verify(self, ctx: Context, checkpoint: Snapshot, planned: ChangeSet,
               left_alone: set[str] | frozenset[str] = frozenset()) -> Verification:
        """Re-snapshot and prove every reversible change is back and every compensation is in place."""
        post = take_snapshot(ctx, self.project_id, "verify")
        self._save_snapshot(post)
        residual = diff(checkpoint, post, ctx.policy, planned.checkpoint_id)
        planned_by_id = {c.id: c for c in planned.changes}
        drift, explained = [], []
        for change in residual.changes:
            if change.id in left_alone:
                explained.append(f"{change.summary} (not this run's; left alone)")
                continue
            original = planned_by_id.get(change.id)
            if original and original.reversibility is Reversibility.IRREVERSIBLE:
                explained.append(f"{change.summary} (irreversible, declared)")
                continue
            handler = HANDLERS[change.kind]
            target = original or change
            live = (post.resources.get(change.kind) or {}).get(change.key)
            if original and original.reversibility is Reversibility.COMPENSATABLE and \
                    handler.is_restored(ctx, target, live):
                explained.append(f"{change.summary} (compensated)")
                continue
            drift.append(change.summary)
        for kind, err in residual.blind_spots.items():
            drift.append(f"could not verify {kind}: {err}")
        return Verification(ok=not drift, snapshot_id=post.id, checked=len(planned.changes), drift=drift,
                            explained=explained)

    def undo_last(self) -> RewindReport:
        """Rewind the most recent rewind, by rewinding to its pre-rewind checkpoint."""
        reports = [r for r in self.list_reports() if not r.dry_run and r.pre_rewind_checkpoint_id]
        if not reports:
            raise RewindError("no rewind to undo")
        last = reports[0]
        return self.rewind(last.pre_rewind_checkpoint_id, reason=f"undo {last.id}",
                           trigger={"undo_of": last.id})


def _slug(project_id: str) -> str:
    return project_id.replace("/", "__")
