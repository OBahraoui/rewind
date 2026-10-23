"""The hands-off loop as a durable, idempotent state machine.

`advance(run)` looks at the run's state and at live GitLab state, does at most one
step, persists, and returns. It is driven by webhooks (fast path) and by a
periodic tick (safety net), so a missed webhook or a restart never strands a run.
Runs are serialized per project so every change set is attributable to one run.

Trust is asymmetric: undoing is cheap to authorize (anyone on the project, by a
`/rewind` comment), accepting the irreversible is not (only through Rewind's
authenticated API, never a comment, because agents comment with the user's identity).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from datetime import timedelta
from typing import Any

from rewind import gate as gate_mod
from rewind import goals, ledger, security
from rewind.config import Settings
from rewind.engine import Rewind
from rewind.flows import Flows
from rewind.http import ApiError
from rewind.models import ChangeSet, RewindReport, utcnow
from rewind.resources.repository import REWIND_MARKER
from rewind.runs import Approval, FlowRun, Run, RunState, gate_fingerprints, security_fingerprints
from rewind.snapshot import diff as diff_snapshots
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
MAX_FLOW_ATTEMPTS = 2  # a failed agent session is retried once as a fresh session

# Job failures that are the platform's fault, not the change's: retried before anything is rewound.
INFRA_FAILURES = {"runner_system_failure", "stuck_or_timeout_failure", "scheduler_failure", "api_failure",
                  "runner_unsupported", "unknown_failure", "data_integrity_failure", "job_execution_timeout",
                  "stale_schedule", "archived_failure", "unmet_prerequisites"}
ACTIVE_PIPELINE = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
FINISHED_JOB = {"success", "failed", "canceled"}

# States in which the default branch must not move except through Rewind's gated merge.
GUARDED = {RunState.PLANNING, RunState.DEVELOPING, RunState.GATING, RunState.REROUTING, RunState.TESTING,
           RunState.FIXING, RunState.REVIEWING, RunState.MERGING, RunState.RELEASING}

# Duo's developer flow opens merge requests as drafts, and GitLab refuses to merge a draft.
DRAFT_PREFIX = re.compile(r"^\s*(\[draft\]|\(draft\)|draft:)\s*", re.IGNORECASE)

# Pipeline stage -> DevSecOps lifecycle stage, for evidence links.
STAGE_MAP = {"verify": "verify", "package": "package", "secure": "secure", "test": "secure",
             "staging": "release", "production": "release", "configure": "configure", "monitor": "monitor",
             "release": "release"}

STEP_LABEL = {"plan": "planner flow", "develop": "developer flow", "review": "code review flow",
              "fix": "fix-pipeline flow", "secfix": "security-fix flow"}
# Who made a change that no agent session made.
UNATTRIBUTED = {"cloud_run": "release pipeline", "feature_flag": "release pipeline (configure)",
                "release": "release pipeline", "tag": "release pipeline", "repository": "Rewind's gated merge",
                "issue": "Rewind's gated merge (Closes #)", "branch": "Rewind's gated merge"}

LABELS = {
    "running": "rewind:running", "shipped": "rewind:shipped", "rewound": "rewind:rewound",
    "blocked": "rewind:blocked", "failed": "rewind:failed",
}

COMMAND = re.compile(r"^\s*/rewind(?=\s|$)(?:\s+(?P<verb>[\w-]+))?.*$", re.IGNORECASE)
HELP = """⏪ Rewind commands
- `/rewind`: undo this run's whole change set (production traffic, flags, code, records) and verify zero drift
- `/rewind dry-run`: show the exact inverse plan without changing anything
- `/rewind status`: where the run is
- `/rewind stop`: stop the run here; Rewind gates, merges and releases nothing more from it
Accepting something Rewind can't undo is never done from a comment (agents comment with your identity):
use `rewind approve <run> --reason "..."` or the authenticated API."""


class Orchestrator:
    def __init__(self, settings: Settings, engine: Rewind, repo: RunRepository, flows: Flows):
        self.settings, self.engine, self.repo, self.flows = settings, engine, repo, flows
        self.gitlab = engine.ctx.gitlab
        self.policy = engine.ctx.policy
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @property
    def default_branch(self) -> str:
        return self.engine.ctx.meta.get("default_branch") or "main"

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

    @contextlib.contextmanager
    def _run_lock(self, run_id: str, blocking: bool = False):
        with self._guard:
            lock = self._locks.setdefault(run_id, threading.Lock())
        acquired = lock.acquire(blocking=blocking, timeout=60 if blocking else -1)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    def advance(self, run_id: str) -> Run | None:
        with self._run_lock(run_id) as acquired:
            if not acquired:
                return None  # another trigger is already advancing this run
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

    # -- the state machine ---------------------------------------------------
    def _step(self, run: Run) -> None:
        handler = getattr(self, f"_on_{run.state.value}")
        timeout = TIMEOUTS.get(run.state)
        if timeout and utcnow() - run.state_since > timeout:
            self._on_timeout(run)
            return
        try:
            if self._guard_default_branch(run):
                return
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

    def _start_flow(self, run: Run, step: str, goal: str, **kw: Any) -> FlowRun | None:
        """Start a flow, unless the daily credit guard is spent (then the run stops for a human)."""
        limit = self.settings.max_flows_per_day
        if not self.repo.take_flow_budget(limit):
            self._block(run, f"Credit guard: Rewind already started {limit} flows today "
                             f"(REWIND_MAX_FLOWS_PER_DAY). Nothing was started; re-label the issue tomorrow "
                             f"or raise the limit.")
            return None
        flow = self.flows.start(step, goal, **kw)
        run.flows.append(flow)
        return flow

    def _on_queued(self, run: Run) -> None:
        earlier = [r for r in self.repo.active() if r.id != run.id and r.created_at < run.created_at]
        if earlier:
            return  # serialized: wait for the project's current run
        if not run.checkpoint_id:
            recent = self.gitlab.list_pipelines(ref=self.default_branch)[:5]
            busy = [p for p in recent if p["status"] in ACTIVE_PIPELINE]
            if busy:
                # A release still in flight would land inside this run's change set and be credited to the agent.
                if run.extra.get("waiting_for") != busy[0]["id"]:
                    run.extra["waiting_for"] = busy[0]["id"]
                    run.note(f"waiting for {self.default_branch} pipeline #{busy[0]['id']} to finish before taking "
                             f"the checkpoint, so the starting state is stable")
                return
        if run.checkpoint_id:  # a retry after a failed flow start must keep the original checkpoint
            cp = self.engine.get_checkpoint(run.checkpoint_id)
        else:
            cp = self.engine.checkpoint(f"run {run.id} · issue #{run.issue_iid}", run_id=run.id,
                                        issue_iid=run.issue_iid)
        run.checkpoint_id = cp.id
        run.extra["base_sha"] = cp.default_sha
        serving = ", ".join(f"{env}: {rev}" for env, rev in cp.serving.items()) or "not tracked"
        run.add_evidence("govern", f"Checkpoint {cp.id} (default branch {cp.default_sha[:8]}, {serving})", "recorded")
        if self.flows.enabled("plan"):
            flow = self._start_flow(run, "plan", goals.plan(run.issue_iid, run.issue_title, cp.id),
                                    issue_iid=run.issue_iid)
            if flow:
                run.move(RunState.PLANNING, f"checkpoint {cp.id} taken; planner flow #{flow.workflow_id} started")
        else:
            run.move(RunState.PLANNING, f"checkpoint {cp.id} taken")

    def _flow_status(self, run: Run, step: str) -> str:
        flow = run.current_flow(step)
        if flow is None:
            return "done"
        status = self.flows.poll(flow)
        if status == "failed" and flow.attempt < MAX_FLOW_ATTEMPTS and flow.request:
            flow.finished_at = flow.finished_at or utcnow()
            if self.repo.take_flow_budget(self.settings.max_flows_per_day):
                retry = self.flows.retry(flow)
                run.flows.append(retry)
                run.note(f"{STEP_LABEL.get(step, step)} #{flow.workflow_id} failed; "
                         f"retrying once as #{retry.workflow_id}")
                return "running"
        if status in ("done", "failed"):
            flow.finished_at = flow.finished_at or utcnow()
            if flow.checkpoint_id is None:  # step checkpoint: what this agent session changed, undoable alone
                label = f"{STEP_LABEL.get(step, step)} #{flow.workflow_id}"
                flow.checkpoint_id = self.engine.checkpoint(f"after {label}", run_id=run.id, issue_iid=run.issue_iid,
                                                            kind="step").id
        if status == "needs_human" and utcnow() - flow.started_at > NEEDS_HUMAN_GRACE:
            return "needs_human"
        return "running" if status == "needs_human" else status

    def _on_planning(self, run: Run) -> None:
        status = self._flow_status(run, "plan")
        if status == "running":
            return
        plan = run.current_flow("plan")
        if status == "done" and plan:
            run.add_evidence("plan", f"Plan posted by planner flow #{plan.workflow_id}", "done",
                             plan.web_url or run.issue_url)
        elif status != "done":
            run.note(f"planner flow ended with {status}; the plan is advisory, continuing")
        issue = self.gitlab.get_issue(run.issue_iid) or {}
        flow = self._start_flow(run, "develop", goals.develop(run.issue_iid, run.issue_title,
                                                              issue.get("description") or "", self.default_branch,
                                                              run.checkpoint_id or "",
                                                              plan_first=not self.flows.enabled("plan")),
                                issue_iid=run.issue_iid)
        if flow:
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
        self._plan_evidence(run)
        run.mr_iid, run.mr_url = mr["iid"], mr.get("web_url", "")
        self.gitlab.create_mr_note(mr["iid"], f"⏪ Rewind checkpoint `{run.checkpoint_id}` covers everything this "
                                             f"merge request ships (run `{run.id}`). It will be gated for "
                                             f"reversibility before merge. Comment `/rewind help` for commands.")
        dev = run.current_flow("develop")
        run.add_evidence("create", f"MR !{mr['iid']} opened by developer flow #{dev.workflow_id if dev else '?'}",
                         "opened", run.mr_url)
        run.move(RunState.GATING, f"MR !{mr['iid']} opened")

    def _plan_evidence(self, run: Run) -> None:
        """The plan comment the developer agent posted before coding (when no planner flow ran)."""
        if any(e.stage == "plan" for e in run.evidence):
            return
        dev = run.current_flow("develop")
        since = dev.started_at.isoformat()[:19] if dev else ""
        for note in self.gitlab.list_issue_notes(run.issue_iid):
            body = note.get("body") or ""
            if goals.PLAN_MARKER in body and (note.get("created_at") or "") >= since:
                run.add_evidence("plan", f"Plan posted by developer flow #{dev.workflow_id if dev else '?'} "
                                         f"before coding", "posted", f"{run.issue_url}#note_{note['id']}")
                return

    def _on_gating(self, run: Run) -> None:
        mr = self.gitlab.get_merge_request(run.mr_iid)
        if not mr or mr["state"] != "opened":
            self._fail(run, f"MR !{run.mr_iid} is no longer open")
            return
        base = (mr.get("diff_refs") or {}).get("base_sha") or run.extra.get("base_sha") or \
            self.engine.get_checkpoint(run.checkpoint_id).default_sha
        run.extra["mr_base_sha"] = base
        change_set = self.engine.diff(run.checkpoint_id)
        decision = gate_mod.evaluate(self.engine.ctx, change_set, base, mr["sha"])
        run.gate = decision
        run.change_counts = decision.counts
        self._attribute(run, change_set)
        self.repo.save_change_set(run.id, change_set)
        run.add_evidence("govern", f"Reversibility gate on {mr['sha'][:8]}", decision.verdict)
        approval = run.approval_for(mr["sha"], gate_fingerprints(decision)) if decision.findings else None
        if decision.verdict == "allow" or approval:
            run.gated_sha = mr["sha"]
            self._label(run, add=["rewind:reversible"] if not approval else [], remove=["rewind:needs-human"])
            if approval and decision.verdict != "allow":
                run.add_evidence("govern", f"Human override by {approval.by} on {mr['sha'][:8]}: {approval.reason}",
                                 "approved")
                run.move(RunState.TESTING, f"gate said {decision.verdict}; {approval.by} accepted it for "
                                           f"{mr['sha'][:8]}; waiting for the MR pipeline")
            else:
                run.move(RunState.TESTING, f"gate allowed {mr['sha'][:8]}; waiting for the MR pipeline")
        elif decision.verdict == "reroute" and run.reroutes < self.policy.gate.max_reroutes:
            run.reroutes += 1
            flow = self._start_flow(
                run, "develop", goals.reroute(run.mr_iid, mr["source_branch"], [f.message for f in decision.findings],
                                              decision.instructions),
                mr_iid=run.mr_iid, source_branch=mr["source_branch"])  # issue_id + mr_id is a GitLab 500
            if flow:
                run.move(RunState.REROUTING,
                         f"gate rerouted: {decision.instructions[0] if decision.instructions else ''}")
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
        kind = "release pipeline" if pipeline.get("ref") == self.default_branch else "MR pipeline"
        for job in self.gitlab.list_pipeline_jobs(pipeline["id"]):
            stage = STAGE_MAP.get(job.get("stage", ""))
            if not stage or (stages and stage not in stages) or job.get("status") not in FINISHED_JOB:
                continue
            run.add_evidence(stage, f"{job['name']} · {kind} #{pipeline['id']}", job["status"], job.get("web_url", ""))

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
            if self._security_gate(run, mr, pipeline):
                return
            if self.flows.enabled("review"):
                flow = self._start_flow(run, "review", goals.review(run.mr_iid), mr_iid=run.mr_iid)
                if flow:
                    run.move(RunState.REVIEWING, f"MR pipeline green; code review flow #{flow.workflow_id} started")
            else:
                run.move(RunState.MERGING, "MR pipeline green")
        elif status in ("failed", "canceled", "skipped"):
            self._pipeline_evidence(run, pipeline, {"verify", "secure"})
            if run.fix_attempts >= self.settings.max_fix_attempts or not self.flows.enabled("fix"):
                self._block(run, f"MR pipeline still {status} after {run.fix_attempts} fix attempt(s)")
                return
            run.fix_attempts += 1
            flow = self._start_flow(run, "fix", goals.fix(run.mr_iid, mr["source_branch"],
                                                          pipeline.get("web_url", "")),
                                    mr_iid=run.mr_iid, source_branch=mr["source_branch"])
            if flow:
                run.extra["fixing_step"] = "fix"
                run.move(RunState.FIXING, f"MR pipeline {status}; fix-pipeline flow #{flow.workflow_id} started")

    def _security_gate(self, run: Run, mr: dict, pipeline: dict) -> bool:
        """Findings this change introduced go back to an agent to fix; True if the run moved."""
        base = run.extra.get("mr_base_sha") or run.extra.get("base_sha")
        if not base:
            return False
        changed = {d["new_path"] for d in self.gitlab.compare(base, run.gated_sha).get("diffs", [])}
        scan = security.introduced_findings(self.gitlab, pipeline["id"], changed,
                                            self.policy.security.fix_severities)
        findings = scan.findings
        run.security_findings = findings
        severities = "/".join(self.policy.security.fix_severities).lower()
        if scan.unavailable:
            # Never call a scan clean that didn't produce a report.
            run.add_evidence("secure", "Security scan incomplete: no report from " + ", ".join(scan.unavailable),
                             "incomplete", pipeline.get("web_url", ""))
        if not findings:
            if scan.scanned:
                run.add_evidence("secure", f"No new {severities} findings in the {len(changed)} file(s) this "
                                           f"change touched ({', '.join(scan.scanned)})", "clean",
                                 pipeline.get("web_url", ""))
            return False
        described = [security.describe(f) for f in findings]
        run.add_evidence("secure", f"{len(findings)} {severities} finding(s) introduced by this change", "found",
                         pipeline.get("web_url", ""))
        if run.approval_for(run.gated_sha, security_fingerprints(findings)):
            run.note(f"{len(findings)} security finding(s) accepted by an approval for {run.gated_sha[:8]}")
            return False
        if run.security_fix_attempts < self.policy.security.max_fix_attempts and self.flows.enabled("develop"):
            run.security_fix_attempts += 1
            flow = self._start_flow(run, "secfix", goals.security_fix(run.mr_iid, mr["source_branch"], described),
                                    mr_iid=run.mr_iid, source_branch=mr["source_branch"], using="develop")
            if flow:
                run.extra["fixing_step"] = "secfix"
                run.move(RunState.FIXING, f"{len(findings)} security finding(s) introduced; security-fix flow "
                                          f"#{flow.workflow_id} started")
            return True
        self._label(run, add=["rewind:needs-human"])
        self._block(run, f"{len(findings)} security finding(s) introduced by this change remain after "
                         f"{run.security_fix_attempts} agent fix attempt(s): " + "; ".join(described[:3]))
        return True

    def _on_fixing(self, run: Run) -> None:
        step = run.extra.get("fixing_step", "fix")
        status = self._flow_status(run, step)
        if status == "running":
            return
        if status != "done":
            self._block(run, f"{STEP_LABEL.get(step, step)} ended with {status}")
            return
        run.move(RunState.GATING, "fix pushed; any new commit is gated again")

    def _on_reviewing(self, run: Run) -> None:
        status = self._flow_status(run, "review")
        if status == "running":
            return
        flow = run.current_flow("review")
        run.add_evidence("verify", f"Duo code review flow #{flow.workflow_id if flow else '?'}", status,
                         (flow.web_url if flow else "") or run.mr_url)
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
        if mr.get("draft") or mr.get("work_in_progress"):
            mr["title"] = DRAFT_PREFIX.sub("", mr["title"]) or mr["title"]
            self.gitlab.update_merge_request(run.mr_iid, title=mr["title"])
            run.note("marked the merge request ready: agents open merge requests as drafts")
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
        pipelines = self.gitlab.list_pipelines(ref=self.default_branch, sha=run.merge_sha)
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
            self._attribute(run, change_set)
            self.repo.save_change_set(run.id, change_set)
            self._label(run, add=[LABELS["shipped"]], remove=[LABELS["running"]])
            run.move(RunState.SHIPPED, "release pipeline green: staging, production and monitoring passed")
        elif failed_jobs or status in ("failed", "canceled"):
            retried = run.extra.setdefault("retried_jobs", [])
            infra = [j for j in failed_jobs if j.get("failure_reason") in INFRA_FAILURES and j["name"] not in retried]
            if infra and len(infra) == len(failed_jobs):
                # The platform failed, not the change: retry once instead of rewinding a good release.
                for job in infra:
                    self.gitlab.retry_job(job["id"])
                    retried.append(job["name"])
                    run.note(f"{job['name']} hit a {job['failure_reason'].replace('_', ' ')} (GitLab infrastructure, "
                             f"not this change); retried it")
                return
            self._pipeline_evidence(run, pipeline)
            if failed_jobs:
                names = ", ".join(f"{j['name']} ({(j.get('failure_reason') or 'failed').replace('_', ' ')})"
                                  for j in failed_jobs)
                reason = f"release pipeline failed: {names}"
            else:
                reason = (f"release pipeline was {status} before it finished (for example superseded by a newer "
                          f"push to {self.default_branch}), so production state is unknown")
            self._rewind(run, reason, {"pipeline_id": pipeline["id"], "jobs": [j["name"] for j in failed_jobs]})

    def _on_rewinding(self, run: Run) -> None:
        self._rewind(run, run.extra.get("rewind_reason", "resuming an interrupted rewind"))

    # -- the default-branch guard --------------------------------------------
    def _guard_default_branch(self, run: Run) -> bool:
        """Catch commits that reach the default branch without passing Rewind's gate.

        Before merge the branch must stay at the checkpoint; while releasing, at the gated
        merge. An agent-made commit that appears anyway bypassed the gate, so the run is
        rewound. A human's commit makes the change set unattributable, so the run stops.
        """
        if run.state not in GUARDED or run.state == RunState.MERGING:
            return False
        expected = run.merge_sha if run.state == RunState.RELEASING else run.extra.get("base_sha")
        if not expected:
            return False
        head = ((self.gitlab.get_branch(self.default_branch) or {}).get("commit") or {}).get("id")
        if not head or head == expected:
            return False
        if run.state != RunState.RELEASING and run.mr_iid:
            mr = self.gitlab.get_merge_request(run.mr_iid) or {}
            if mr.get("state") == "merged" and run.gated_sha and mr.get("sha") == run.gated_sha:
                run.merge_sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or head
                if run.state == RunState.FIXING:  # merged while known problems were being fixed
                    what = STEP_LABEL.get(run.extra.get("fixing_step", "fix"), "a fix")
                    self._rewind(run, f"MR !{run.mr_iid} was merged outside Rewind while {what} was still "
                                      f"fixing it (failing checks or security findings)", {"merged_outside": True})
                    return True
                if run.state != RunState.REVIEWING:
                    run.add_evidence("release", f"MR !{run.mr_iid} merged outside Rewind before its checks "
                                                f"finished", "unverified", run.mr_url)
                run.move(RunState.RELEASING, f"MR !{run.mr_iid} was merged outside Rewind at the gated commit "
                                             f"{run.gated_sha[:8]}; watching the release")
                return True
        commits = [c for c in self.gitlab.compare(expected, head).get("commits", [])
                   if REWIND_MARKER not in (c.get("title") or "") + (c.get("message") or "")]
        gated: set[str] = set()
        if run.gated_sha and run.state != RunState.RELEASING:
            gated = {c["id"] for c in self.gitlab.compare(expected, run.gated_sha).get("commits", [])}
        ungated = [c for c in commits if c["id"] not in gated]
        if not ungated:
            return False
        agent = [c for c in ungated if self.policy.agents.is_agent_commit(c)]
        if agent:
            first = agent[0]
            self._rewind(run, f"agent commit {first['id'][:8]} (\"{first.get('title', '')}\") reached "
                              f"{self.default_branch} without passing the reversibility gate",
                         {"ungated_commits": [c["id"] for c in agent]})
            return True
        authors = ", ".join(sorted({c.get("author_name") or "unknown" for c in ungated}))
        if run.state == RunState.RELEASING:
            if not run.extra.get("noted_foreign_commits"):
                run.extra["noted_foreign_commits"] = True
                run.note(f"{self.default_branch} moved during the release (commits by {authors}); still watching "
                         f"the release of {run.merge_sha[:8]}")
            return False
        self._block(run, f"{self.default_branch} changed outside this run (commits by {authors}), so its change "
                         f"set is no longer attributable to one run. Rewind stopped instead of guessing.")
        return True

    # -- provenance ------------------------------------------------------------
    def _attribute(self, run: Run, change_set: ChangeSet) -> None:
        """Link every change to the agent session that made it, using the step checkpoints."""
        try:
            prev = self.engine.load_snapshot(self.engine.get_checkpoint(run.checkpoint_id).snapshot_id)
            seen: dict[str, list[list[str]]] = {}
            for flow in run.flows:
                if not flow.checkpoint_id:
                    continue
                snap = self.engine.load_snapshot(self.engine.get_checkpoint(flow.checkpoint_id).snapshot_id)
                label = f"{STEP_LABEL.get(flow.step, flow.step)} #{flow.workflow_id}"
                for c in diff_snapshots(prev, snap, self.policy).changes:
                    entry = [label, flow.web_url]
                    if entry not in seen.setdefault(c.id, []):
                        seen[c.id].append(entry)
                prev = snap
            run.provenance = {c.id: seen.get(c.id) or [[UNATTRIBUTED.get(c.kind, "outside agent sessions"), ""]]
                              for c in change_set.changes}
        except Exception:
            log.exception("provenance failed", extra={"run": run.id})

    # -- outcomes ------------------------------------------------------------
    def _rewind(self, run: Run, reason: str, trigger: dict[str, Any] | None = None) -> None:
        if run.state != RunState.REWINDING:
            run.extra["rewind_reason"] = reason
            run.move(RunState.REWINDING, reason)
            self._open_incident(run, reason)
            self.repo.save(run)
            self._publish(run)
        self._cancel_release(run)
        # Revert only the code this run merged; a commit an agent pushed around the gate is reverted too.
        scope = "" if (trigger or {}).get("ungated_commits") else run.merge_sha
        report = self.engine.rewind(run.checkpoint_id, reason=reason, trigger={"run": run.id, **(trigger or {})},
                                    code_scope=scope)
        run.rewind_id = report.id
        if report.change_set:
            self._attribute(run, report.change_set)
        run.add_evidence("monitor", f"Rewind {report.id}", "zero drift" if report.zero_drift else "drift")
        self._label(run, add=[LABELS["rewound"]], remove=[LABELS["running"], LABELS["shipped"], LABELS["blocked"]])
        msg = f"rewound to {run.checkpoint_id}"
        if report.mitigated_after_s is not None:
            msg += f"; production mitigated in {report.mitigated_after_s:.1f}s"
        run.move(RunState.REWOUND, msg + ("; zero drift" if report.zero_drift else "; drift found"))
        self._resolve_incident(run, report)

    def _cancel_release(self, run: Run) -> None:
        """A release still running would redeploy what Rewind is about to undo."""
        if not run.merge_sha:
            return
        active = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled", "manual"}
        for p in self.gitlab.list_pipelines(ref=self.default_branch, sha=run.merge_sha):
            if p["status"] in active:
                try:
                    self.gitlab.cancel_pipeline(p["id"])
                    run.note(f"cancelled release pipeline #{p['id']} before rewinding")
                except ApiError as exc:
                    run.note(f"could not cancel pipeline #{p['id']} ({exc.status}); rewinding anyway")

    def _open_incident(self, run: Run, reason: str) -> None:
        """GitLab Incident Management: an incident that Rewind resolves itself when verification passes."""
        if run.incident_iid:
            return
        try:
            body = (f"**{reason}**\n\nRun `{run.id}` for #{run.issue_iid} ({run.issue_title}).\n\n"
                    f"Rewind is restoring checkpoint `{run.checkpoint_id}` now: production traffic and flags first, "
                    f"then code, then records. Verification results will be posted here.")
            incident = self.gitlab.create_issue(f"⏪ Rewinding: {run.issue_title}", body, labels=["rewind:incident"],
                                                issue_type="incident")
            run.incident_iid, run.incident_url = incident["iid"], incident.get("web_url", "")
            self.engine.register_own("issue", incident["iid"])  # Rewind's record, never part of a change set
            run.note(f"incident #{incident['iid']} opened")
        except Exception:
            log.warning("could not open incident", extra={"run": run.id})

    def _resolve_incident(self, run: Run, report: RewindReport) -> None:
        if not run.incident_iid:
            return
        total = (report.finished_at - report.started_at).total_seconds() if report.finished_at else 0
        lines = [f"⏪ Rewind `{report.id}` finished in {total:.1f}s."]
        if report.mitigated_after_s is not None:
            lines.append(f"Production mitigated (traffic and flags back) after **{report.mitigated_after_s:.1f}s**.")
        try:
            if report.zero_drift:
                lines.append("Verification: **zero drift** against the checkpoint. Resolving this incident.")
                self.gitlab.create_issue_note(run.incident_iid, "\n\n".join(lines))
                self.gitlab.update_issue(run.incident_iid, state_event="close")
            else:
                drift = report.verification.drift if report.verification else []
                lines.append("Verification found drift, so this stays open for a human:\n" +
                             "\n".join(f"- {d}" for d in drift))
                self.gitlab.create_issue_note(run.incident_iid, "\n\n".join(lines))
                self.gitlab.update_issue(run.incident_iid, add_labels="rewind:needs-human")
        except Exception:
            log.warning("could not update incident", extra={"run": run.id})

    def _block(self, run: Run, reason: str) -> None:
        run.error = reason
        self._label(run, add=[LABELS["blocked"]], remove=[LABELS["running"]])
        run.move(RunState.BLOCKED, reason)

    def _fail(self, run: Run, reason: str) -> None:
        run.error = reason
        self._label(run, add=[LABELS["failed"]], remove=[LABELS["running"]])
        run.move(RunState.FAILED, reason)

    # -- humans: comment commands and approvals --------------------------------
    def command(self, *, text: str, author_id: int, author: str, issue_iid: int | None = None,
                mr_iid: int | None = None) -> str | None:
        """Handle a `/rewind ...` comment. Returns the reply posted, or None if it wasn't a command."""
        m = COMMAND.match(text.splitlines()[0] if text else "")
        if not m:
            return None
        verb = (m.group("verb") or "").lower()
        runs = self.repo.find(mr_iid=mr_iid) if mr_iid else ([self.repo.latest_for_issue(issue_iid)]
                                                             if issue_iid else [])
        run = next((r for r in runs if r), None)
        reply = self._command_reply(run, verb, author_id, author)

        def post(body: str) -> None:
            if mr_iid:
                self.gitlab.create_mr_note(mr_iid, body)
            elif issue_iid:
                self.gitlab.create_issue_note(issue_iid, body)

        post(reply)
        return reply

    def _command_reply(self, run: Run | None, verb: str, author_id: int, author: str) -> str:
        if verb in ("help", "?"):
            return HELP
        member = self.gitlab.project_member(author_id) or {}
        if int(member.get("access_level") or 0) < 30:
            return f"⏪ @{author}, only project members with the Developer role or higher can use Rewind commands."
        if run is None:
            return "⏪ There is no Rewind run for this issue or merge request."
        if verb == "approve":
            return (f"⏪ Accepting something Rewind can't undo is never done from a comment: agents comment with your "
                    f"identity, so a comment can't prove a human decided. Run\n\n"
                    f"`rewind approve {run.id} --reason \"...\"`\n\nwith your Rewind API token. The approval is bound "
                    f"to the exact commit you approve.")
        if verb == "stop":
            try:
                self.stop(run.id, by=f"@{author}")
            except ValueError as exc:
                return f"⏪ {exc}"
            return (f"⏪ Stopped run `{run.id}` as requested by @{author}. Rewind will gate, merge and release "
                    f"nothing more from it. An agent session that is still running may finish on its own; "
                    f"its merge request stays open. Comment `/rewind` to undo what the run already changed.")
        if verb == "status":
            link = f" · [dashboard]({self.settings.public_url}/runs/{run.id})" if self.settings.public_url else ""
            return f"⏪ Run `{run.id}`: **{ledger.STATUS[run.state]}**{link}"
        if verb in ("dry-run", "plan"):
            plan = self.engine.plan(run.checkpoint_id)
            head = f"⏪ Dry run for checkpoint `{run.checkpoint_id}` (nothing changed):"
            return "\n".join([head, "", *ledger.render_report(plan)])
        if verb:
            return f"⏪ Unknown command `/rewind {verb}`.\n\n{HELP}"
        if run.state == RunState.REWOUND:
            return f"⏪ Run `{run.id}` is already rewound. To put it back: `rewind undo-last`."
        if not run.checkpoint_id:
            return f"⏪ Run `{run.id}` never took a checkpoint, so there is nothing to rewind."
        if run.state not in (RunState.SHIPPED, RunState.BLOCKED, RunState.FAILED, RunState.RELEASING):
            return (f"⏪ Run `{run.id}` is still {run.state.value}; agents may be mid-change. Rewind undoes it "
                    f"automatically if the release regresses, or comment `/rewind` again once it has finished.")
        with self._run_lock(run.id, blocking=True) as acquired:
            if not acquired:
                return "⏪ Rewind is busy with this run; try again in a minute."
            run = self.repo.get(run.id)
            if run.state not in (RunState.SHIPPED, RunState.BLOCKED, RunState.FAILED, RunState.RELEASING):
                return f"⏪ Run `{run.id}` is now {run.state.value}; nothing to do."
            self._rewind(run, f"requested by @{author} in a comment", {"requested_by": author})
            self.repo.save(run)
            self._publish(run)
        report = self.engine.get_report(run.rewind_id)
        verdict = "zero drift ✅" if report.zero_drift else "drift found ⚠️ (see the ledger)"
        mitigated = f" Production mitigated in {report.mitigated_after_s:.1f}s." if report.mitigated_after_s else ""
        return (f"⏪ Rewound run `{run.id}` to checkpoint `{run.checkpoint_id}` as requested by @{author}."
                f"{mitigated} Verification: {verdict}. Undo with `rewind undo-last`.")

    def stop(self, run_id: str, *, by: str) -> Run:
        """A human halts a run. Stopping only removes autonomy, so it needs no special trust."""
        with self._run_lock(run_id, blocking=True) as acquired:
            if not acquired:
                raise ValueError("Rewind is busy with this run; try again in a minute.")
            run = self.repo.get(run_id)
            if run is None:
                raise ValueError(f"no run {run_id}")
            if run.state.terminal:
                raise ValueError(f"Run `{run.id}` already finished ({run.state.value}).")
            if run.state == RunState.REWINDING:
                raise ValueError(f"Run `{run.id}` is rewinding; let it finish so production is consistent.")
            watching = run.state == RunState.RELEASING
            self._fail(run, f"Stopped by {by}" + ("; Rewind is no longer watching the release" if watching else ""))
            self.repo.save(run)
            self._publish(run)
            return run

    def approve(self, run_id: str, *, by: str, reason: str) -> Run:
        """A human accepts what the gate could not allow, for the MR's current head only."""
        if not reason.strip():
            raise ValueError("an approval needs a reason")
        with self._run_lock(run_id, blocking=True) as acquired:
            if not acquired:
                raise RuntimeError("run is busy; try again")
            run = self.repo.get(run_id)
            if run is None:
                raise LookupError(run_id)
            if run.state != RunState.BLOCKED or not run.mr_iid:
                raise ValueError(f"run is {run.state.value}; only a blocked run with a merge request can be approved")
            mr = self.gitlab.get_merge_request(run.mr_iid)
            if not mr or mr["state"] != "opened":
                raise ValueError(f"MR !{run.mr_iid} is not open")
            covers = gate_fingerprints(run.gate) | security_fingerprints(run.security_findings)
            run.approvals.append(Approval(by=by, reason=reason.strip(), sha=mr["sha"], covers=sorted(covers)))
            run.error = None
            self._label(run, add=[LABELS["running"]], remove=[LABELS["blocked"]])
            run.move(RunState.GATING, f"{by} accepted the risk for {mr['sha'][:8]}: {reason.strip()}")
            self.gitlab.create_mr_note(run.mr_iid, f"⏪ **{by}** accepted what Rewind can't undo in this change, for "
                                                   f"commit `{mr['sha'][:8]}` only: {reason.strip()}")
            self.repo.save(run)
            self._publish(run)
        self.advance(run_id)
        return self.repo.get(run_id)

    # -- side channels -------------------------------------------------------
    def _label(self, run: Run, add: list[str] = (), remove: list[str] = ()) -> None:
        fields = {}
        if add:
            fields["add_labels"] = ",".join(add)
        if remove:
            fields["remove_labels"] = ",".join(remove)
        if not fields:
            return
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
            change_set = report.change_set
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
