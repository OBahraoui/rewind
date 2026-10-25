"""Comment commands, approvals, incidents, provenance, credit guard, security fixes, gate-bypass guard."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from rewind.ledger import LEDGER_MARKER
from rewind.runs import RunState
from tests.service.helpers import agent_opens_mr, drive_to_merge, finish_flow, release_jobs

AGENT = ("Duo Developer", "service_account_group_1_abc@noreply.gitlab.com")


def ship(orch, gl, cr, title="Promo pricing"):
    issue = gl.add_issue(title, labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    cr.deploy("production")
    gl.update_feature_flag("new_pricing", {"active": True})
    gl.add_pipeline("main", run.merge_sha, "success", jobs=release_jobs("success"))
    run = orch.advance(run.id)
    assert run.state is RunState.SHIPPED
    return issue, run


def blocked_run(orch, gl):
    issue = gl.add_issue("Email receipts", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    orch.advance(run.id)
    agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items)",
                   {"app/receipts.py": "import httpx\n\n\ndef send(o):\n"
                                       "    httpx.post('https://mail.example', json=o)\n"})
    finish_flow(gl, orch.repo.get(run.id), "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED
    return issue, run


# -- agent sessions and provenance ---------------------------------------------
def test_ledger_links_sessions_and_attributes_changes(orch, gl, cr):
    issue, run = ship(orch, gl, cr)
    assert all(f.web_url.endswith(f"/agent-sessions/{f.workflow_id}") for f in run.flows)
    assert all(f.checkpoint_id and f.checkpoint_id.startswith("step_") for f in run.flows)
    ledger = next(n["body"] for n in gl.issue_notes[issue] if LEDGER_MARKER in n["body"])
    assert "### Agent sessions" in ledger and "/agent-sessions/" in ledger
    # The merged MR was created by the developer flow; production moved because of the release pipeline.
    assert run.provenance[f"merge_request:{run.mr_iid}"][0][0].startswith("developer flow #")
    assert run.provenance["cloud_run:production"][0][0] == "release pipeline"
    assert "by developer flow" in ledger or "developer flow #" in ledger


def test_step_checkpoint_undoes_only_later_steps(orch, gl, cr):
    """Rewinding to the checkpoint taken after the developer flow keeps its MR but undoes the release."""
    _issue, run = ship(orch, gl, cr)
    after_develop = next(f.checkpoint_id for f in run.flows if f.step == "develop")
    report = orch.engine.rewind(after_develop, reason="undo only the release")
    assert report.zero_drift, report.verification
    assert gl.flags["new_pricing"]["active"] is False  # release step undone
    kinds = {s.kind for s in report.steps}
    assert "cloud_run" in kinds and "feature_flag" in kinds


# -- credit guard ----------------------------------------------------------------
def test_credit_guard_stops_before_starting_a_flow(orch, gl):
    orch.settings = replace(orch.settings, max_flows_per_day=1)
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)  # plan flow takes the only slot
    finish_flow(gl, run, "plan")
    started = sum(1 for c in gl.calls if c[0] == "start_workflow")
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED and "Credit guard" in run.error
    assert sum(1 for c in gl.calls if c[0] == "start_workflow") == started  # nothing started past the cap


# -- incidents -------------------------------------------------------------------
def test_rewind_opens_and_resolves_an_incident_without_drift(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    cr.deploy("production")
    gl.add_pipeline("main", run.merge_sha, "failed", jobs=release_jobs("failed"))
    run = orch.advance(run.id)
    assert run.state is RunState.REWOUND
    incident = gl.issues[run.incident_iid]
    assert incident["issue_type"] == "incident" and "rewind:incident" in incident["labels"]
    assert incident["state"] == "closed"  # resolved by Rewind once verification passed
    assert "zero drift" in gl.issue_notes[run.incident_iid][-1]["body"]
    assert orch.engine.get_report(run.rewind_id).zero_drift  # the incident itself is not drift


# -- comment commands ------------------------------------------------------------
def test_rewind_comment_on_shipped_issue(orch, gl, cr):
    tree_before = None
    issue = gl.add_issue("Bulk discount", labels=["rewind:auto"])
    tree_before = gl.tree("main")
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    gl.add_pipeline("main", run.merge_sha, "success", jobs=release_jobs("success"))
    run = orch.advance(run.id)
    assert run.state is RunState.SHIPPED

    reply = orch.command(text="/rewind", author_id=2, author="alice", issue_iid=issue)
    run = orch.repo.get(run.id)
    assert run.state is RunState.REWOUND
    assert "requested by @alice" in run.timeline[-2].message or "requested by @alice" in run.extra["rewind_reason"]
    assert "zero drift" in reply
    assert gl.tree("main") == tree_before
    assert reply == gl.issue_notes[issue][-1]["body"]


def test_commands_require_project_membership(orch, gl, cr):
    issue, run = ship(orch, gl, cr)
    reply = orch.command(text="/rewind", author_id=999, author="stranger", issue_iid=issue)
    assert "Developer role" in reply
    assert orch.repo.get(run.id).state is RunState.SHIPPED


def test_dry_run_status_help_and_unknown(orch, gl, cr):
    issue, _run = ship(orch, gl, cr)
    calls = len(gl.calls)
    plan = orch.command(text="/rewind dry-run", author_id=2, author="alice", issue_iid=issue)
    assert "nothing changed" in plan and "planned" in plan
    assert [c for c in gl.calls[calls:] if c[0] not in ("create_issue_note", "create_branch")] == []
    assert "Shipped" in orch.command(text="/rewind status", author_id=2, author="alice", issue_iid=issue)
    assert "/rewind dry-run" in orch.command(text="/rewind help", author_id=999, author="x", issue_iid=issue)
    assert "Unknown command" in orch.command(text="/rewind fly", author_id=2, author="alice", issue_iid=issue)
    assert orch.command(text="please rewind this", author_id=2, author="alice", issue_iid=issue) is None


def test_approve_is_never_accepted_from_a_comment(orch, gl):
    _issue, run = blocked_run(orch, gl)
    reply = orch.command(text="/rewind approve trust me", author_id=2, author="alice", mr_iid=run.mr_iid)
    assert "never done from a comment" in reply
    assert orch.repo.get(run.id).state is RunState.BLOCKED


# -- authenticated approvals -------------------------------------------------------
def test_approval_unblocks_exactly_the_approved_commit(orch, gl):
    _issue, run = blocked_run(orch, gl)
    approved_sha = gl.get_merge_request(run.mr_iid)["sha"]
    run = orch.approve(run.id, by="Othmane", reason="receipts provider is idempotent; accepted")
    assert run.state is RunState.TESTING and run.gated_sha == approved_sha
    assert run.approvals[0].sha == approved_sha
    assert any("Human override by Othmane" in e.name for e in run.evidence)
    # A new commit after the approval is not covered by it: gated again, blocked again.
    gl.commit_files("agent/feature", {"app/receipts2.py": "import smtplib\n"})
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED


def test_approval_needs_a_reason_and_a_blocked_run(orch, gl, cr):
    _issue, run = blocked_run(orch, gl)
    with pytest.raises(ValueError):
        orch.approve(run.id, by="x", reason="  ")
    run = orch.approve(run.id, by="x", reason="accepted")  # fine once
    assert run.state is RunState.TESTING
    with pytest.raises(ValueError):  # a run that isn't blocked has nothing to approve
        orch.approve(run.id, by="x", reason="again")


# -- security findings -------------------------------------------------------------
def sast_report(path: str, severity: str = "High") -> bytes:
    return json.dumps({"vulnerabilities": [{"name": "Use of eval", "severity": severity,
                                            "location": {"file": path, "start_line": 3},
                                            "identifiers": [{"value": "B307"}]}]}).encode()


def to_mr_pipeline(orch, gl, findings_file: str | None):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    orch.advance(run.id)
    mr = agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 1")
    finish_flow(gl, orch.repo.get(run.id), "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING
    sha = gl.get_merge_request(mr)["sha"]
    pid = gl.add_pipeline("agent/feature", sha, "success", mr_iid=mr,
                          jobs=[{"id": 501, "name": "semgrep-sast", "stage": "secure", "status": "success"}])
    if findings_file:
        gl.artifacts[(501, "gl-sast-report.json")] = sast_report(findings_file)
    return mr, pid, orch.advance(run.id)


def test_agent_fixes_findings_it_introduced(orch, gl):
    mr, _, run = to_mr_pipeline(orch, gl, "app/pricing.py")  # a file the MR changed
    assert run.state is RunState.FIXING and run.extra["fixing_step"] == "secfix"
    fix = [c for c in gl.calls if c[0] == "start_workflow"][-1][2]
    assert "Use of eval" in fix["goal"] and fix["merge_request_id"] == mr
    assert fix["workflow_definition"] == "developer/v1"
    # The agent pushes a fix; it's gated again, and the new pipeline is clean.
    sha = gl.commit_files("agent/feature", {"app/pricing.py": gl.tree("agent/feature")["app/pricing.py"] + "\n"})
    finish_flow(gl, run, "secfix")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING and run.gated_sha == sha
    gl.add_pipeline("agent/feature", sha, "success", mr_iid=mr,
                    jobs=[{"id": 502, "name": "semgrep-sast", "stage": "secure", "status": "success"}])
    gl.artifacts[(502, "gl-sast-report.json")] = json.dumps({"vulnerabilities": []}).encode()
    run = orch.advance(run.id)
    assert run.state is RunState.REVIEWING
    assert any(e.stage == "secure" and e.status == "clean" for e in run.evidence)


def test_findings_in_untouched_files_are_not_the_agents_job(orch, gl):
    _, _, run = to_mr_pipeline(orch, gl, "app/catalog.py")  # pre-existing code the MR didn't touch
    assert run.state is RunState.REVIEWING


def test_findings_that_survive_the_fix_attempt_block(orch, gl):
    mr, _, run = to_mr_pipeline(orch, gl, "app/pricing.py")
    sha = gl.commit_files("agent/feature", {"app/pricing.py": gl.tree("agent/feature")["app/pricing.py"] + "#\n"})
    finish_flow(gl, run, "secfix")
    orch.advance(run.id)
    gl.add_pipeline("agent/feature", sha, "success", mr_iid=mr,
                    jobs=[{"id": 503, "name": "semgrep-sast", "stage": "secure", "status": "success"}])
    gl.artifacts[(503, "gl-sast-report.json")] = sast_report("app/pricing.py")
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED and "security finding" in run.error


# -- the default-branch guard ------------------------------------------------------
def to_testing(orch, gl):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    orch.advance(run.id)
    agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 1")
    finish_flow(gl, orch.repo.get(run.id), "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING
    return issue, run


def test_agent_commit_that_skips_the_gate_is_rewound(orch, gl):
    _issue, run = to_testing(orch, gl)
    main_before = gl.tree("main")
    gl.commit_files("main", {"app/pricing.py": "def legacy_total(items):\n    return 0\n"}, author=AGENT)
    run = orch.advance(run.id)
    assert run.state is RunState.REWOUND
    assert "without passing the reversibility gate" in run.extra["rewind_reason"]
    assert gl.tree("main") == main_before
    assert gl.issues[run.incident_iid]["state"] == "closed"


def test_human_commit_mid_run_stops_the_run_instead_of_reverting_it(orch, gl):
    _issue, run = to_testing(orch, gl)
    gl.commit_files("main", {"docs/notes.md": "human work\n"}, author=("Othmane", "o@example.com"))
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED and "no longer attributable" in run.error
    assert gl.tree("main")["docs/notes.md"] == "human work\n"  # never reverted


def test_gated_mr_merged_by_a_human_is_adopted(orch, gl):
    _issue, run = to_testing(orch, gl)
    gl.merge_merge_request(run.mr_iid) if not gl.get_merge_request(run.mr_iid)["draft"] else None
    gl.update_merge_request(run.mr_iid, title="Promo pricing")
    gl.merge_merge_request(run.mr_iid)
    run = orch.advance(run.id)
    assert run.state is RunState.RELEASING and run.merge_sha == gl.mrs[run.mr_iid]["merge_commit_sha"]



# -- review fixes ------------------------------------------------------------------
def test_scan_without_report_is_incomplete_not_clean(orch, gl):
    _, _, run = to_mr_pipeline(orch, gl, None)  # scanner job succeeded but left no report
    assert any(e.stage == "secure" and e.status == "incomplete" for e in run.evidence)
    assert not any(e.stage == "secure" and e.status == "clean" for e in run.evidence)


def test_words_starting_with_rewind_are_not_commands(orch, gl, cr):
    issue, run = ship(orch, gl, cr)
    for text in ("/rewinding this would be bad", "/rewind:now", "/rewind-me"):
        assert orch.command(text=text, author_id=2, author="alice", issue_iid=issue) is None
    assert orch.repo.get(run.id).state is RunState.SHIPPED


def test_rewind_during_release_cancels_the_pipeline_first(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    pid = gl.add_pipeline("main", run.merge_sha, "running", jobs=release_jobs("created"))
    orch.command(text="/rewind", author_id=2, author="alice", issue_iid=issue)
    assert gl.pipelines[pid]["status"] == "canceled"
    assert orch.repo.get(run.id).state is RunState.REWOUND


def test_second_rewind_comment_does_not_rewind_again(orch, gl, cr):
    issue, run = ship(orch, gl, cr)
    orch.command(text="/rewind", author_id=2, author="alice", issue_iid=issue)
    first = orch.repo.get(run.id).rewind_id
    reply = orch.command(text="/rewind", author_id=2, author="bob", issue_iid=issue)
    assert "already rewound" in reply
    assert orch.repo.get(run.id).rewind_id == first  # undo-last still targets the real rewind


def test_incident_is_never_part_of_the_change_set(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    gl.add_pipeline("main", run.merge_sha, "failed", jobs=release_jobs("failed"))
    run = orch.advance(run.id)
    report = orch.engine.get_report(run.rewind_id)
    assert not any(s.kind == "issue" and s.key == str(run.incident_iid) for s in report.steps)
    assert "Closed by Rewind" not in "".join(n["body"] for n in gl.issue_notes[run.incident_iid])


def test_agent_cannot_hide_issues_behind_rewind_labels(engine, gl):
    cp = engine.checkpoint()
    hidden = gl.add_issue("agent-made", labels=["rewind:incident"])
    change = next(c for c in engine.diff(cp.id).changes if c.kind == "issue")
    assert change.key == str(hidden) and change.op == "created"


def test_approval_covers_only_the_findings_the_human_saw(orch, gl):
    _issue, run = blocked_run(orch, gl)
    run = orch.approve(run.id, by="Othmane", reason="receipts provider is idempotent")
    assert run.state is RunState.TESTING
    # Same commit, but a new irreversible change appears in project state: the old approval doesn't cover it.
    gl.update_variable("API_URL", "*", value="changed")
    run = orch.repo.get(run.id)
    run.move(RunState.GATING, "re-gate")
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED and "CI/CD variable" in run.error


def test_mr_merged_outside_rewind_while_fixing_is_rewound(orch, gl):
    mr, _, run = to_mr_pipeline(orch, gl, "app/pricing.py")
    assert run.state is RunState.FIXING
    gl.update_merge_request(mr, title="Promo")
    gl.merge_merge_request(mr)  # someone merges the known-vulnerable commit
    run = orch.advance(run.id)
    assert run.state is RunState.REWOUND and "merged outside Rewind" in run.extra["rewind_reason"]


def test_failed_agent_session_is_retried_once(orch, gl):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    run = orch.advance(run.id)
    first = run.current_flow("develop")
    finish_flow(gl, run, "develop", status="failed")
    run = orch.advance(run.id)
    retry = run.current_flow("develop")
    assert run.state is RunState.DEVELOPING and retry.workflow_id != first.workflow_id and retry.attempt == 2
    assert gl.workflows[retry.workflow_id]["payload"]["goal"] == gl.workflows[first.workflow_id]["payload"]["goal"]
    finish_flow(gl, run, "develop", status="failed")  # the retry fails too: now the run stops
    run = orch.advance(run.id)
    assert run.state is RunState.FAILED and "developer flow ended with failed" in run.error


def test_runner_failure_is_retried_not_rewound(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    jobs = [{"id": 901, "name": "build-image", "stage": "package", "status": "failed",
             "failure_reason": "runner_system_failure"}]
    pid = gl.add_pipeline("main", run.merge_sha, "failed", jobs=jobs)
    run = orch.advance(run.id)
    assert run.state is RunState.RELEASING and ("retry_job", (901,), {}) in gl.calls
    assert "GitLab infrastructure" in run.timeline[-1].message
    # The retry fails again for the same reason: now Rewind does rewind, and says why.
    gl.jobs[pid][0].update(status="failed", failure_reason="runner_system_failure")
    run = orch.advance(run.id)
    assert run.state is RunState.REWOUND and "runner system failure" in run.extra["rewind_reason"]


def test_checkpoint_waits_for_main_pipeline_to_finish(orch, gl):
    pid = gl.add_pipeline("main", gl.branches["main"]["sha"], "running")
    run = orch.start_run(gl.add_issue("Promo", labels=["rewind:auto"]))
    assert run.state is RunState.QUEUED and run.checkpoint_id is None
    assert "waiting for main pipeline" in run.timeline[-1].message
    gl.set_pipeline_status(pid, "success")
    run = orch.advance(run.id)
    assert run.checkpoint_id and run.state is RunState.PLANNING


def test_evidence_records_only_finished_jobs(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    jobs = [*release_jobs("failed"),
            {"name": "release", "stage": "release", "status": "skipped"},
            {"name": "deploy:production", "stage": "production", "status": "created"}]
    gl.add_pipeline("main", run.merge_sha, "failed", jobs=jobs)
    run = orch.advance(run.id)
    assert not any(e.status in ("created", "skipped", "pending") for e in run.evidence)
    assert any("release pipeline #" in e.name for e in run.evidence)


def test_queued_status_says_what_it_waits_for(orch, gl):
    from rewind import ledger

    gl.add_pipeline("main", gl.branches["main"]["sha"], "running")
    run = orch.start_run(gl.add_issue("Promo", labels=["rewind:auto"]))
    assert "Waiting for pipeline" in ledger.status_text(run)
    body = next(n["body"] for n in gl.issue_notes[run.issue_iid] if ledger.LEDGER_MARKER in n["body"])
    assert "stable state" in body


def test_human_push_during_release_survives_the_rewind(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    gl.commit_files("main", {"docs/notes.md": "human work\n"}, author=("Othmane", "o@example.com"))
    gl.add_pipeline("main", run.merge_sha, "canceled", jobs=release_jobs("canceled"))
    run = orch.advance(run.id)
    assert run.state is RunState.REWOUND
    assert "superseded by a newer push" in run.extra["rewind_reason"]
    assert gl.tree("main")["docs/notes.md"] == "human work\n"
    assert "def legacy_total(items):\n    return sum(items)" in gl.tree("main")["app/pricing.py"]
    assert orch.engine.get_report(run.rewind_id).zero_drift


def test_developer_posts_a_plan_when_no_planner_flow(orch, gl):
    from rewind.config import FlowRef

    orch.flows.refs["plan"] = FlowRef.parse("none")
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    assert run.state is RunState.DEVELOPING
    goal = [c for c in gl.calls if c[0] == "start_workflow"][-1][2]["goal"]
    assert "### 🧭 Plan" in goal
    gl.create_issue_note(issue, "### 🧭 Plan\n1. new_pricing_total() in app/pricing.py, behind new_pricing")
    agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 1")
    finish_flow(gl, run, "develop")
    run = orch.advance(run.id)
    plan = next(e for e in run.evidence if e.stage == "plan")
    assert "before coding" in plan.name and "#note_" in plan.url


def test_stop_halts_a_run_from_a_comment(orch, gl):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    reply = orch.command(text="/rewind stop", author_id=2, author="alice", issue_iid=issue)
    run = orch.repo.get(run.id)
    assert run.state is RunState.FAILED and "Stopped by @alice" in run.error
    assert "Stopped run" in reply
    assert "already finished" in orch.command(text="/rewind stop", author_id=2, author="alice", issue_iid=issue)
    # A stopped run is never advanced again.
    finish_flow(gl, run, "plan")
    assert orch.advance(run.id).state is RunState.FAILED


def test_queued_run_says_which_run_it_waits_for(orch, gl):
    from rewind import ledger

    first = orch.start_run(gl.add_issue("A", labels=["rewind:auto"]))
    second = orch.start_run(gl.add_issue("B", labels=["rewind:auto"]))
    assert second.state is RunState.QUEUED and second.extra["waiting_for_run"] == first.id
    assert f"Waiting for run {first.id} (#{first.issue_iid})" in ledger.status_text(second)


def test_queued_run_times_out_instead_of_waiting_forever(orch, gl):
    from datetime import timedelta

    gl.add_pipeline("main", gl.branches["main"]["sha"], "pending")  # a pipeline that never runs
    run = orch.start_run(gl.add_issue("A", labels=["rewind:auto"]))
    run.state_since = run.state_since - timedelta(minutes=61)
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.FAILED and "waited over an hour for pipeline" in run.error
    assert not any(c[0] == "start_workflow" for c in gl.calls)


def test_a_run_locked_by_another_instance_is_left_alone(orch, gl):
    run = orch.start_run(gl.add_issue("A", labels=["rewind:auto"]))
    with orch.engine.store.lock(f"run-{run.id}", owner="other-instance"):
        assert orch.advance(run.id) is None  # the other instance owns this run right now
    assert orch.advance(run.id) is not None


# -- code review that requests changes ---------------------------------------------
def to_review(orch, gl, title="Draft: Promo pricing"):
    issue, run = to_testing(orch, gl)
    mr = run.mr_iid
    gl.mrs[mr]["title"] = title
    gl.add_pipeline("agent/feature", gl.get_merge_request(mr)["sha"], "success", mr_iid=mr,
                    jobs=[{"name": "test", "stage": "verify", "status": "success"}])
    run = orch.advance(run.id)
    assert run.state is RunState.REVIEWING
    return issue, mr, run


REVIEW = ["A flat $15 off breaks the guardrail that an order is never charged less than half its subtotal. "
          "Clamp the result.", "This test locks in a total of 0 for a tea."]


def test_requested_changes_go_back_to_the_agent_then_merge(orch, gl):
    _, mr, run = to_review(orch, gl)
    gl.review(mr, REVIEW, request_changes=True)
    finish_flow(gl, run, "review")
    run = orch.advance(run.id)  # review done -> merging, where GitLab says changes were requested
    run = orch.advance(run.id)
    assert run.state is RunState.FIXING and run.extra["fixing_step"] == "reviewfix"
    goal = [c for c in gl.calls if c[0] == "start_workflow"][-1][2]
    assert "requested changes" in goal["goal"] and "> A flat $15 off" in goal["goal"]
    assert goal["merge_request_id"] == mr and goal["workflow_definition"] == "developer/v1"
    assert any(e.status == "changes requested" for e in run.evidence)
    # The agent pushes the fix: gated again, tested again, reviewed again, and this time the review is happy.
    sha = gl.commit_files("agent/feature", {"app/pricing.py": gl.tree("agent/feature")["app/pricing.py"] + "\n"})
    finish_flow(gl, run, "reviewfix")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING and run.gated_sha == sha
    gl.add_pipeline("agent/feature", sha, "success", mr_iid=mr, jobs=[])
    run = orch.advance(run.id)
    assert run.state is RunState.REVIEWING
    gl.review(mr, ["Looks good now."], request_changes=False)
    finish_flow(gl, run, "review")
    run = orch.advance(run.id)
    run = orch.advance(run.id) if run.state is RunState.MERGING else run
    assert run.state is RunState.RELEASING, run.timeline[-1]


def test_changes_still_requested_after_the_fix_block_with_the_reviewers_words(orch, gl):
    _, mr, run = to_review(orch, gl)
    gl.review(mr, REVIEW, request_changes=True)
    finish_flow(gl, run, "review")
    orch.advance(run.id)
    run = orch.advance(run.id)
    sha = gl.commit_files("agent/feature", {"app/pricing.py": gl.tree("agent/feature")["app/pricing.py"] + "#\n"})
    finish_flow(gl, run, "reviewfix")
    orch.advance(run.id)
    gl.add_pipeline("agent/feature", sha, "success", mr_iid=mr, jobs=[])
    run = orch.advance(run.id)
    gl.review(mr, ["Still below half the subtotal for a tea."], request_changes=True)
    finish_flow(gl, run, "review")
    orch.advance(run.id)
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED
    assert "still requests changes" in run.error and "Still below half the subtotal for a tea." in run.error
    assert "rewind:needs-human" in gl.issues[run.issue_iid]["labels"]


def test_a_doubled_draft_prefix_is_cleared_in_one_go(orch, gl):
    _, mr, run = to_review(orch, gl, title="Draft: Draft: Promo pricing")
    finish_flow(gl, run, "review")
    run = orch.advance(run.id)
    run = orch.advance(run.id) if run.state is RunState.MERGING else run
    assert run.state is RunState.RELEASING and gl.mrs[mr]["title"] == "Promo pricing"


def test_a_merge_that_times_out_says_what_it_waited_for(orch, gl):
    from datetime import timedelta

    _, mr, run = to_review(orch, gl)
    finish_flow(gl, run, "review")
    gl.mrs[mr]["review_status"] = "not_approved"  # e.g. an approval rule a human must satisfy
    run = orch.advance(run.id)
    run = orch.advance(run.id)
    assert run.state is RunState.MERGING and "waiting to merge: it needs approval" in run.timeline[-1].message
    run.state_since = run.state_since - timedelta(minutes=11)
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.FAILED
    assert run.error == "timed out while merging (it needs approval from a human reviewer)"


QUESTIONS = ["Please confirm that the sign-off covers unlimited free $0 orders.", "Add a test at exactly 1500."]


def blocked_by_review_without_a_change(orch, gl):
    """The review asks questions only a human can answer; the agent pushes nothing."""
    _, mr, run = to_review(orch, gl)
    gl.review(mr, QUESTIONS, request_changes=True)
    finish_flow(gl, run, "review")
    orch.advance(run.id)
    run = orch.advance(run.id)
    assert run.state is RunState.FIXING
    finish_flow(gl, run, "reviewfix")  # done, but no new commit
    return mr, orch.advance(run.id)


def test_a_review_fix_that_changes_nothing_stops_for_a_human_without_reviewing_again(orch, gl):
    _, run = blocked_by_review_without_a_change(orch, gl)
    assert run.state is RunState.BLOCKED
    assert "pushed no change" in run.error and "Please confirm that the sign-off covers" in run.error
    assert "rewind approve" in run.error
    reviews = [c for c in gl.calls if c[0] == "start_workflow" and c[2].get("workflow_definition") == "code_review/v1"]
    assert len(reviews) == 1  # the same commit isn't reviewed twice


def test_a_human_can_accept_the_reviews_concerns_for_one_commit(orch, gl):
    mr, run = blocked_by_review_without_a_change(orch, gl)
    flows_before = sum(1 for c in gl.calls if c[0] == "start_workflow")
    run = orch.approve(run.id, by="Othmane", reason="Finance confirmed unlimited free orders for the sale")
    assert any("Othmane accepted the code review's concerns for" in e.message for e in run.timeline)
    for _ in range(3):
        run = orch.advance(run.id)
    # Not reviewed again; GitLab still blocks, so Rewind asks a person to Bypass, once, and waits.
    assert run.state is RunState.MERGING and "select Bypass" in run.timeline[-1].message
    assert sum(1 for c in gl.calls if c[0] == "start_workflow") == flows_before
    notes = [n["body"] for n in gl.mr_notes[mr]]
    asks = [b for b in notes if "accepted the code review's open concerns" in b]
    assert len(asks) == 1 and "**Bypass**" in asks[0] and "@" not in asks[0]  # a mention would summon the bot
    assert any("Please confirm" in b for b in notes)  # the review's comments stay
    assert run.approvals[-1].covers == [f"review|{run.gated_sha}"]
    gl.bypass_requested_changes(mr)  # a person who can merge clicks Bypass in GitLab
    run = orch.advance(run.id)
    assert run.state is RunState.RELEASING, run.timeline[-1]


def test_waiting_for_a_bypass_allows_an_hour_and_says_why_when_it_runs_out(orch, gl):
    from datetime import timedelta

    _, run = blocked_by_review_without_a_change(orch, gl)
    run = orch.approve(run.id, by="Othmane", reason="accepted")
    for _ in range(3):
        run = orch.advance(run.id)
    run.state_since -= timedelta(minutes=30)  # past the usual 10 minutes for merging
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.MERGING
    run.state_since -= timedelta(minutes=31)
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.FAILED and "select Bypass" in run.error


def test_a_run_that_timed_out_waiting_for_a_bypass_can_be_approved_again(orch, gl):
    from datetime import timedelta

    mr, run = blocked_by_review_without_a_change(orch, gl)
    run = orch.approve(run.id, by="Othmane", reason="accepted")
    for _ in range(3):
        run = orch.advance(run.id)
    run.state_since -= timedelta(minutes=61)
    orch.repo.save(run)
    run = orch.advance(run.id)
    assert run.state is RunState.FAILED
    run = orch.approve(run.id, by="Othmane", reason="accepted again")
    for _ in range(3):
        run = orch.advance(run.id)
    assert run.state is RunState.MERGING
    assert sum("accepted the code review's open concerns" in n["body"] for n in gl.mr_notes[mr]) == 2
    gl.bypass_requested_changes(mr)
    assert orch.advance(run.id).state is RunState.RELEASING


def test_accepting_a_review_is_bound_to_its_commit(orch, gl):
    mr, run = blocked_by_review_without_a_change(orch, gl)
    run = orch.approve(run.id, by="Othmane", reason="accepted")
    gl.commit_files("agent/feature", {"app/pricing.py": gl.tree("agent/feature")["app/pricing.py"] + "#\n"})
    run = orch.advance(run.id)  # a new commit: gated again, and the approval no longer applies to its review
    assert run.state in (RunState.GATING, RunState.TESTING)
    assert not orch._review_accepted(run, gl.get_merge_request(mr)["sha"])


def test_a_run_blocked_by_review_before_the_upgrade_can_still_be_accepted(orch, gl):
    _, run = blocked_by_review_without_a_change(orch, gl)
    run.extra.pop("blocked_by")  # what a run blocked by the previous version looks like
    run.error = "Duo code review still requests changes on MR !1 after 1 agent fix attempt(s)"
    orch.repo.save(run)
    run = orch.approve(run.id, by="Othmane", reason="accepted")
    assert run.approvals[-1].covers == [f"review|{run.gated_sha}"]


def test_undoing_a_run_that_never_merged_leaves_other_peoples_work_alone(orch, gl, cr):
    """A human ships to main and production after a blocked run's checkpoint; `/rewind` on that run must take back
    only the agent's branch and merge request, never the human's commit or deploy."""
    issue, run = blocked_run(orch, gl)
    mr = run.mr_iid
    gl.commit_files("main", {"README.md": "a human's change\n"})
    human_rev = cr.deploy("production")
    gl.members[42] = 30
    reply = orch.command(text="/rewind", author_id=42, author="dev", issue_iid=issue)
    assert "only its own changes were undone" in reply and "left alone" in reply, reply
    run = orch.repo.get(run.id)
    assert run.state is RunState.REWOUND
    assert gl.tree("main")["README.md"] == "a human's change\n"          # the human's commit stays
    assert cr.serving("production")["serving"] == [[human_rev, 100]]      # and so does their deploy
    assert gl.mrs[mr]["state"] == "closed" and "agent/feature" not in gl.branches  # the agent's work is undone
    report = orch.engine.get_report(run.rewind_id)
    assert report.zero_drift and report.trigger["left_alone"]
    assert any("left alone" in e for e in report.verification.explained)


def test_the_block_message_quotes_the_reviewers_point_not_its_preamble():
    from service.orchestrator import _gist

    summary = "I left two comments. The first is a recommendation to check the 50% guardrail. I estimate little work."
    assert _gist([summary]) == "The first is a recommendation to check the 50% guardrail."
    assert _gist(["I left one comment.", "This flat discount lets the total fall below the guardrail."]) == \
        "This flat discount lets the total fall below the guardrail."
