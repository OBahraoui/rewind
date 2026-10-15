"""The hands-off loop as a durable, idempotent state machine.

`advance(run)` looks at the run's state and at live GitLab state, does at most one
step, persists, and returns. It is driven by webhooks (fast path) and by a
periodic tick (safety net), so a missed webhook or a restart never strands a run.
Runs are serialized per project so every change set is attributable to one run.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any

from rewind import gate as gate_mod
from rewind import goals, ledger
from rewind.config import Settings
from rewind.engine import Rewind
from rewind.flows import Flows
from rewind.http import ApiError
from rewind.models import RewindReport, utcnow
from rewind.runs import Run, RunState
from service.repo import RunRepository

log = logging.getLogger("rewind.orchestrator")

# How long each state may last before the run stops waiting.
TIMEOUTS = {
    RunState.PLANNING: timedelta(minutes=30),
    RunState.DEVELOPING: timedelta(minutes=60),
    RunState.REROUTING: timedelta(minutes=45),
    RunState.TESTING: timedelta(minutes=45),
    RunState.FIXING: timedelta(minutes=45),
    RunState.REVIEWING: timedelta(minutes=20),
    RunState.MERGING: timedelta(minutes=10),
    RunState.RELEASING: timedelta(minutes=45),
}
NEEDS_HUMAN_GRACE = timedelta(minutes=3)

# Pipeline stage -> DevSecOps lifecycle stage, for evidence links.
STAGE_MAP = {"verify": "verify", "package": "package", "secure": "secure", "test": "secure",
             "staging": "release", "production": "release", "configure": "configure", "monitor": "monitor",
             "release": "release"}

LABELS = {
    "running": "rewind:running", "shipped": "rewind:shipped", "rewound": "rewind:rewound",
    "blocked": "rewind:blocked", "failed": "rewind:failed",
}


class Orchestrator:
    def __init__(self, settings: Settings, engine: Rewind, repo: RunRepository, flows: Flows):
        self.settings, self.engine, self.repo, self.flows = settings, engine, repo, flows
        self.gitlab = engine.ctx.gitlab
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- entry points --------------------------------------------------------
    def start_run(self, issue_iid: int) -> Run | None:
        """Create a run for a labelled issue, unless one is already in flight for it."""
        with self._guard:
            latest = self.repo.latest_for_issue(issue_iid)
            if latest and not latest.state.terminal:
                return None
            issue = self.gitlab.get_issue(issue_iid)
            if not issue or issue["state"] != "opened":
                return None
            run = Run(project_id=self.engine.project_id, issue_iid=issue_iid, issue_title=issue["title"],
                      issue_url=issue.get("web_url", ""))
            run.move(RunState.QUEUED, "issue labelled for a hands-off run")
            self.repo.save(run)
        self._label(run, add=[LABELS["running"]],
                    remove=[LABELS[k] for k in ("shipped", "rewound", "blocked", "failed")])
        self.advance(run.id)
        return self.repo.get(run.id)

    def tick(self) -> None:
        for run in self.repo.active():
            try:
                self.advance(run.id)
            except Exception:
                log.exception("advance failed", extra={"run": run.id})

    def advance(self, run_id: str) -> Run | None:
        with self._guard:
            lock = self._locks.setdefault(run_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return None  # another trigger is already advancing this run
        try:
            run = self.repo.get(run_id)
            if not run or run.state.terminal:
                return run
            # Drive through instantaneous transitions in one call; stop when waiting on the outside world.
            for _ in range(10):
                before = (run.state, len(run.timeline), len(run.evidence))
                self._step(run)
                self.repo.save(run)
                if (run.state, len(run.timeline), len(run.evidence)) != before:
                    self._publish(run)
                if run.state.terminal or run.state == before[0]:
                    break
            return run
        finally:
            lock.release()

    # -- the state machine ---------------------------------------------------
    def _step(self, run: Run) -> None:
        handler = getattr(self, f"_on_{run.state.value}")
        timeout = TIMEOUTS.get(run.state)
        if timeout and utcnow() - run.state_since > timeout:
            self._on_timeout(run)
            return
        try:
            handler(run)
        except ApiError as exc:
            log.warning("transient API error; will retry on next tick",
                        extra={"run": run.id, "state": run.state.value, "status": exc.status})
            if exc.status in (401, 403):
                self._fail(run, f"GitLab refused the request ({exc.status}); check the token's role and scopes")

    def _on_timeout(self, run: Run) -> None:
        if run.state == RunState.REVIEWING:
            run.note("code review did not finish in time; review is advisory, continuing")
            run.move(RunState.MERGING, "merging the gated commit")
        elif run.state == RunState.RELEASING:
            self._rewind(run, "release pipeline did not finish in time; production state is unknown")
        else:
            self._fail(run, f"timed out while {run.state.value}")

    def _on_queued(self, run: Run) -> None:
        earlier = [r for r in self.repo.active() if r.id != run.id and r.created_at < run.created_at]
        if earlier:
            return  # serialized: wait for the project's current run
        cp = self.engine.checkpoint(f"run {run.id} · issue #{run.issue_iid}", run_id=run.id, issue_iid=run.issue_iid)
        run.checkpoint_id = cp.id
        serving = ", ".join(f"{env}: {rev}" for env, rev in cp.serving.items()) or "not tracked"
        run.add_evidence("govern", f"Checkpoint {cp.id} (default branch {cp.default_sha[:8]}, {serving})", "recorded")
        if self.flows.enabled("plan"):
            flow = self.flows.start("plan", goals.plan(run.issue_iid, run.issue_title, cp.id), issue_iid=run.issue_iid)
            run.flows.append(flow)
            run.move(RunState.PLANNING, f"checkpoint {cp.id} taken; planner flow #{flow.workflow_id} started")
        else:
            run.move(RunState.PLANNING, f"checkpoint {cp.id} taken")

    def _flow_status(self, run: Run, step: str) -> str:
        flow = run.current_flow(step)
        if flow is None:
            return "done"
        status = self.flows.poll(flow)
        if status in ("done", "failed"):
            flow.finished_at = flow.finished_at or utcnow()
        if status == "needs_human" and utcnow() - flow.started_at > NEEDS_HUMAN_GRACE:
            return "needs_human"
        return "running" if status == "needs_human" else status

    def _on_planning(self, run: Run) -> None:
        status = self._flow_status(run, "plan")
        if status == "running":
            return
        if status == "done" and run.current_flow("plan"):
            run.add_evidence("plan", f"Plan posted by planner flow #{run.current_flow('plan').workflow_id}",
                             "done", run.issue_url)
        elif status != "done":
            run.note(f"planner flow ended with {status}; the plan is advisory, continuing")
        issue = self.gitlab.get_issue(run.issue_iid) or {}
        flow = self.flows.start("develop", goals.develop(run.issue_iid, run.issue_title, issue.get("description") or "",
                                                         self.engine.ctx.meta.get("default_branch", "main"),
                                                         run.checkpoint_id or ""),
                                issue_iid=run.issue_iid)
        run.flows.append(flow)
        run.move(RunState.DEVELOPING, f"developer flow #{flow.workflow_id} started")

    def _find_mr(self, run: Run) -> dict | None:
        flow_start = run.current_flow("develop").started_at.isoformat() if run.current_flow("develop") else ""
        candidates = [m for m in self.gitlab.related_merge_requests(run.issue_iid)
                      if m["state"] == "opened" and m.get("created_at", "") >= flow_start[:19]]
        if not candidates:
            candidates = [m for m in self.gitlab.list_merge_requests(state="opened", created_after=flow_start)
                          if not m["source_branch"].startswith("rewind/")]
        return max(candidates, key=lambda m: m["iid"]) if candidates else None

    def _on_developing(self, run: Run) -> None:
        status = self._flow_status(run, "develop")
        if status == "running":
            return
        if status != "done":
            self._fail(run, f"developer flow ended with {status}")
            return
        mr = self._find_mr(run)
        if not mr:
            self._fail(run, "developer flow finished without opening a merge request")
            return
        run.mr_iid, run.mr_url = mr["iid"], mr.get("web_url", "")
        self.gitlab.create_mr_note(mr["iid"], f"⏪ Rewind checkpoint `{run.checkpoint_id}` covers everything this "
                                             f"merge request ships (run `{run.id}`). It will be gated for "
                                             f"reversibility before merge.")
        run.add_evidence("create", f"MR !{mr['iid']} opened by developer flow, checkpoint noted", "opened",
                         run.mr_url)
        run.move(RunState.GATING, f"MR !{mr['iid']} opened")

    def _on_gating(self, run: Run) -> None:
        mr = self.gitlab.get_merge_request(run.mr_iid)
        if not mr or mr["state"] != "opened":
            self._fail(run, f"MR !{run.mr_iid} is no longer open")
            return
        base = (mr.get("diff_refs") or {}).get("base_sha") or self.engine.get_checkpoint(run.checkpoint_id).default_sha
        change_set = self.engine.diff(run.checkpoint_id)
        decision = gate_mod.evaluate(self.engine.ctx, change_set, base, mr["sha"])
        run.gate = decision
        run.change_counts = decision.counts
        self.repo.save_change_set(run.id, change_set)
        run.add_evidence("govern", f"Reversibility gate on {mr['sha'][:8]}", decision.verdict)
        if decision.verdict == "allow":
            run.gated_sha = mr["sha"]
            self._label(run, add=["rewind:reversible"], remove=["rewind:needs-human"])
            run.move(RunState.TESTING, f"gate allowed {mr['sha'][:8]}; waiting for the MR pipeline")
        elif decision.verdict == "reroute" and run.reroutes < self.engine.ctx.policy.gate.max_reroutes:
            run.reroutes += 1
            flow = self.flows.start(
                "develop", goals.reroute(run.mr_iid, mr["source_branch"], [f.message for f in decision.findings],
                                         decision.instructions),
                issue_iid=run.issue_iid, mr_iid=run.mr_iid, source_branch=mr["source_branch"])
            run.flows.append(flow)
            run.move(RunState.REROUTING, f"gate rerouted: {decision.instructions[0] if decision.instructions else ''}")
        else:
            self._label(run, add=["rewind:needs-human"], remove=["rewind:reversible"])
            reasons = "; ".join(f.message for f in decision.findings[:3])
            self._block(run, f"Reversibility gate blocked MR !{run.mr_iid}: {reasons}")

    def _on_rerouting(self, run: Run) -> None:
        status = self._flow_status(run, "develop")
        if status == "running":
            return
        if status != "done":
            self._block(run, f"reroute flow ended with {status}")
            return
        run.move(RunState.GATING, "rerouted change ready; gating again")

    def _pipeline_evidence(self, run: Run, pipeline: dict, stages: set[str] | None = None) -> None:
        for job in self.gitlab.list_pipeline_jobs(pipeline["id"]):
            stage = STAGE_MAP.get(job.get("stage", ""))
            if not stage or (stages and stage not in stages):
                continue
            run.add_evidence(stage, f"{job['name']} (pipeline #{pipeline['id']})", job["status"],
                             job.get("web_url", ""))

    def _on_testing(self, run: Run) -> None:
        mr = self.gitlab.get_merge_request(run.mr_iid)
        if mr["sha"] != run.gated_sha:
            run.move(RunState.GATING, "MR head moved after gating; gating again")
            return
        pipeline = mr.get("head_pipeline")
        if not pipeline or pipeline.get("sha") != run.gated_sha:
            return
        status = pipeline["status"]
        if status == "success":
            self._pipeline_evidence(run, pipeline, {"verify", "secure", "package"})
            if self.flows.enabled("review"):
                flow = self.flows.start("review", goals.review(run.mr_iid), mr_iid=run.mr_iid)
                run.flows.append(flow)
                run.move(RunState.REVIEWING, f"MR pipeline green; code review flow #{flow.workflow_id} started")
            else:
                run.move(RunState.MERGING, "MR pipeline green")
        elif status in ("failed", "canceled", "skipped"):
            self._pipeline_evidence(run, pipeline, {"verify", "secure"})
            if run.fix_attempts >= self.settings.max_fix_attempts or not self.flows.enabled("fix"):
                self._block(run, f"MR pipeline still {status} after {run.fix_attempts} fix attempt(s)")
                return
            run.fix_attempts += 1
            flow = self.flows.start("fix", goals.fix(run.mr_iid, mr["source_branch"], pipeline.get("web_url", "")),
                                    mr_iid=run.mr_iid, source_branch=mr["source_branch"])
            run.flows.append(flow)
            run.move(RunState.FIXING, f"MR pipeline {status}; fix-pipeline flow #{flow.workflow_id} started")

    def _on_fixing(self, run: Run) -> None:
        status = self._flow_status(run, "fix")
        if status == "running":
            return
        if status != "done":
            self._block(run, f"fix-pipeline flow ended with {status}")
            return
        run.move(RunState.GATING, "fix pushed; any new commit is gated again")

    def _on_reviewing(self, run: Run) -> None:
        status = self._flow_status(run, "review")
        if status == "running":
            return
        flow = run.current_flow("review")
        run.add_evidence("verify", f"Duo code review flow #{flow.workflow_id if flow else '?'}", status, run.mr_url)
        run.move(RunState.MERGING, "code review finished")

    def _on_merging(self, run: Run) -> None:
        mr = self.gitlab.get_merge_request(run.mr_iid)
        if mr["state"] == "merged":
            run.merge_sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or ""
            run.move(RunState.RELEASING, f"merged as {run.merge_sha[:8]}")
            return
        if mr["sha"] != run.gated_sha:
            run.move(RunState.GATING, "MR head moved before merge; gating again")
            return
        message = (f"Merge !{run.mr_iid}: {mr['title']}\n\nRewind-Run: {run.id}\n"
                   f"Rewind-Checkpoint: {run.checkpoint_id}")
        try:
            merged = self.gitlab.merge_merge_request(run.mr_iid, sha=run.gated_sha, merge_commit_message=message)
        except ApiError as exc:
            if exc.status == 409:
                run.move(RunState.GATING, "MR head moved during merge; gating again")
                return
            if exc.status in (405, 406, 422):
                return  # not mergeable yet (GitLab still checking); retried on the next tick
            raise
        run.merge_sha = merged.get("merge_commit_sha") or merged.get("squash_commit_sha") or ""
        run.add_evidence("release", f"MR !{run.mr_iid} merged as {run.merge_sha[:8]}", "merged", run.mr_url)
        run.move(RunState.RELEASING, f"merged gated commit {run.gated_sha[:8]} as {run.merge_sha[:8]}")

    def _on_releasing(self, run: Run) -> None:
        default = self.engine.ctx.meta.get("default_branch", "main")
        pipelines = self.gitlab.list_pipelines(ref=default, sha=run.merge_sha)
        if not pipelines:
            return
        pipeline = pipelines[0]
        run.release_pipeline_id, run.release_pipeline_url = pipeline["id"], pipeline.get("web_url", "")
        status = pipeline["status"]
        failed_jobs = [j for j in self.gitlab.list_pipeline_jobs(pipeline["id"]) if j["status"] == "failed"
                       and not j.get("allow_failure")]
        if status == "success":
            self._pipeline_evidence(run, pipeline)
            change_set = self.engine.diff(run.checkpoint_id)
            run.change_counts = {r.value: len(v) for r, v in change_set.by_class().items()}
            self.repo.save_change_set(run.id, change_set)
            self._label(run, add=[LABELS["shipped"]], remove=[LABELS["running"]])
            run.move(RunState.SHIPPED, "release pipeline green: staging, production and monitoring passed")
        elif failed_jobs or status in ("failed", "canceled"):
            self._pipeline_evidence(run, pipeline)
            names = ", ".join(j["name"] for j in failed_jobs) or status
            self._rewind(run, f"release pipeline failed ({names})", {"pipeline_id": pipeline["id"],
                                                                     "jobs": [j["name"] for j in failed_jobs]})

    def _on_rewinding(self, run: Run) -> None:
        self._rewind(run, run.extra.get("rewind_reason", "resuming an interrupted rewind"))

    # -- outcomes ------------------------------------------------------------
    def _rewind(self, run: Run, reason: str, trigger: dict[str, Any] | None = None) -> None:
        if run.state != RunState.REWINDING:
            run.extra["rewind_reason"] = reason
            run.move(RunState.REWINDING, reason)
            self.repo.save(run)
            self._publish(run)
        report = self.engine.rewind(run.checkpoint_id, reason=reason, trigger={"run": run.id, **(trigger or {})})
        run.rewind_id = report.id
        run.add_evidence("monitor", f"Rewind {report.id}", "zero drift" if report.zero_drift else "drift")
        self._label(run, add=[LABELS["rewound"]], remove=[LABELS["running"]])
        msg = f"rewound to {run.checkpoint_id}"
        if report.mitigated_after_s is not None:
            msg += f"; production mitigated in {report.mitigated_after_s:.1f}s"
        run.move(RunState.REWOUND, msg + ("; zero drift" if report.zero_drift else "; drift found"))

    def _block(self, run: Run, reason: str) -> None:
        run.error = reason
        self._label(run, add=[LABELS["blocked"]], remove=[LABELS["running"]])
        run.move(RunState.BLOCKED, reason)

    def _fail(self, run: Run, reason: str) -> None:
        run.error = reason
        self._label(run, add=[LABELS["failed"]], remove=[LABELS["running"]])
        run.move(RunState.FAILED, reason)

    # -- side channels -------------------------------------------------------
    def _label(self, run: Run, add: list[str] = (), remove: list[str] = ()) -> None:
        fields = {}
        if add:
            fields["add_labels"] = ",".join(add)
        if remove:
            fields["remove_labels"] = ",".join(remove)
        try:
            self.gitlab.update_issue(run.issue_iid, **fields)
        except Exception:
            log.warning("labelling failed", extra={"run": run.id})

    def _publish(self, run: Run) -> None:
        """Create or update the single ledger note on the issue."""
        report: RewindReport | None = None
        if run.rewind_id:
            try:
                report = self.engine.get_report(run.rewind_id)
            except Exception:
                report = None
        change_set = self.repo.change_set(run.id)
        if report and report.change_set:
            change_set = None  # the rewind report already lists every change
        body = ledger.render(run, change_set=change_set, report=report, dashboard_url=self.settings.public_url)
        try:
            if run.ledger_note_id is None:
                existing = [n for n in self.gitlab.list_issue_notes(run.issue_iid)
                            if ledger.LEDGER_MARKER in (n.get("body") or "") and run.id in n["body"]]
                if existing:
                    run.ledger_note_id = existing[-1]["id"]
            if run.ledger_note_id is None:
                run.ledger_note_id = self.gitlab.create_issue_note(run.issue_iid, body)["id"]
            else:
                self.gitlab.update_issue_note(run.issue_iid, run.ledger_note_id, body)
            self.repo.save(run)
        except Exception:
            log.exception("ledger publish failed", extra={"run": run.id})
