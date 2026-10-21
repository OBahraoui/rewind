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
