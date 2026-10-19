"""The hands-off loop end to end against the fakes."""

from __future__ import annotations

import pytest

from rewind.config import FlowRef, Settings
from rewind.flows import Flows
from rewind.ledger import LEDGER_MARKER
from rewind.runs import RunState
from service.orchestrator import Orchestrator
from service.repo import RunRepository

PRICING = '''from app import flags


def legacy_total(items):
    return sum(items)


def new_pricing_total(items):
    return sum(items)


def total(items):
    if flags.is_enabled("new_pricing"):
        return new_pricing_total(items)
    return legacy_total(items)
'''


@pytest.fixture
def settings() -> Settings:
    refs = {s: FlowRef.parse(v) for s, v in {"plan": "catalog:7", "develop": "definition:developer/v1",
                                             "review": "definition:code_review/v1",
                                             "fix": "definition:fix_pipeline/v1"}.items()}
    return Settings(project_id="42", hash_key="k" * 32, flows=refs, max_fix_attempts=1,
                    public_url="https://rewind.example")


@pytest.fixture
def orch(engine, settings, gl) -> Orchestrator:
    gl.commit_files("main", {"app/pricing.py": PRICING})
    repo = RunRepository(engine.store, "42")
    return Orchestrator(settings, engine, repo, Flows(gl, "42", settings.flows, engine.ctx.policy))


def finish_flow(gl, run, step, status="finished"):
    gl.workflows[run.current_flow(step).workflow_id]["status"] = status


def agent_opens_mr(gl, issue, pricing_body: str, extra: dict | None = None) -> int:
    files = {"app/pricing.py": PRICING.replace("def new_pricing_total(items):\n    return sum(items)", pricing_body),
             "config/flags.yaml": "new_pricing:\n  staging: true\n  production: true\n", **(extra or {})}
    gl.commit_files("agent/feature", files)
    mr = gl.create_merge_request("agent/feature", "main", "Draft: Promo pricing", description=f"Closes #{issue}")
    return mr["iid"]


def drive_to_merge(orch, gl, cr, run, issue):
    """plan -> develop -> MR -> gate -> MR pipeline -> review -> merge; returns the merged run."""
    finish_flow(gl, run, "plan")
    run = orch.advance(run.id)
    assert run.state is RunState.DEVELOPING
    mr = agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 15")
    finish_flow(gl, run, "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING, run.timeline[-1]
    assert run.gate.verdict == "allow"
    gl.add_pipeline("agent/feature", gl.get_merge_request(mr)["sha"], "success", mr_iid=mr,
                    jobs=[{"name": "test", "stage": "verify", "status": "success", "web_url": "u/1"},
                          {"name": "semgrep-sast", "stage": "secure", "status": "success", "web_url": "u/2"}])
    run = orch.advance(run.id)
    assert run.state is RunState.REVIEWING
    review = [c for c in gl.calls if c[0] == "start_workflow"][-1][2]
    assert review["goal"] == str(mr)  # the code_review flow rejects any goal but the MR IID
    finish_flow(gl, run, "review")
    run = orch.advance(run.id)
    assert run.state is RunState.RELEASING, run.timeline[-1]
    return run


def release_jobs(monitor: str) -> list[dict]:
    return [{"name": "build-image", "stage": "package", "status": "success", "web_url": "u/3"},
            {"name": "deploy-staging", "stage": "staging", "status": "success", "web_url": "u/4"},
            {"name": "deploy-production", "stage": "production", "status": "success", "web_url": "u/5"},
            {"name": "apply-flags", "stage": "configure", "status": "success", "web_url": "u/6"},
            {"name": "monitor-production", "stage": "monitor", "status": monitor, "web_url": "u/7"}]


def test_hands_off_run_ships(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = orch.start_run(issue)
    assert run.state is RunState.PLANNING and run.checkpoint_id
    plan_call = next(c for c in gl.calls if c[0] == "start_workflow")[2]
    assert plan_call["ai_catalog_item_consumer_id"] == 7
    assert plan_call["agent_privileges"] == plan_call["pre_approved_agent_privileges"]

    run = drive_to_merge(orch, gl, cr, run, issue)
    cr.deploy("staging")
    cr.deploy("production")
    gl.update_feature_flag("new_pricing", {"active": True})
    gl.add_pipeline("main", run.merge_sha, "success", jobs=release_jobs("success"))
    run = orch.advance(run.id)

    assert run.state is RunState.SHIPPED
    stages = {e.stage for e in run.evidence}
    assert stages == {"plan", "create", "verify", "package", "secure", "release", "configure", "monitor", "govern"}
    assert "rewind:shipped" in gl.issues[issue]["labels"]
    notes = [n for n in gl.issue_notes[issue] if LEDGER_MARKER in n["body"]]
    assert len(notes) == 1, "the ledger is one note, edited in place"
    assert "Shipped to production" in notes[0]["body"]


def test_monitor_failure_rewinds_production(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    main_tree = gl.tree("main")
    prod = cr.serving("production")["serving"]
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    cr.deploy("production")
    gl.update_feature_flag("new_pricing", {"active": True})
    gl.add_pipeline("main", run.merge_sha, "running", jobs=release_jobs("failed"))

    run = orch.advance(run.id)

    assert run.state is RunState.REWOUND, run.timeline[-1]
    report = orch.engine.get_report(run.rewind_id)
    assert report.zero_drift
    assert gl.tree("main") == main_tree
    assert cr.serving("production")["serving"] == prod
    assert gl.flags["new_pricing"]["active"] is False
    assert gl.issues[issue]["state"] == "opened"
    ledger = next(n for n in gl.issue_notes[issue] if LEDGER_MARKER in n["body"])["body"]
    assert "zero drift" in ledger and "mitigated" in ledger


def test_unflagged_change_is_rerouted_then_allowed(orch, gl):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    run = orch.advance(run.id)
    gl.commit_files("agent/feature", {"app/pricing.py": PRICING.replace(
        "def legacy_total(items):\n    return sum(items)", "def legacy_total(items):\n    return sum(items) - 15")})
    gl.create_merge_request("agent/feature", "main", "Draft: Promo", description=f"Closes #{issue}")
    finish_flow(gl, run, "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.REROUTING
    reroute = [c for c in gl.calls if c[0] == "start_workflow"][-1][2]
    assert "new_pricing_total" in reroute["goal"]
    # GitLab answers 500 when issue_id and merge_request_id are combined: rework passes only the MR.
    assert "issue_id" not in reroute and reroute["merge_request_id"] and reroute["source_branch"]
    # The agent follows the instruction on the same branch.
    flagged = "def new_pricing_total(items):\n    return sum(items) - 15"
    gl.commit_files("agent/feature", {"app/pricing.py": PRICING.replace(
        "def new_pricing_total(items):\n    return sum(items)", flagged)})
    finish_flow(gl, run, "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING and run.reroutes == 1


def test_irreversible_side_effect_is_blocked(orch, gl):
    issue = gl.add_issue("Email receipts", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    run = orch.advance(run.id)
    agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items)",
                   {"app/receipts.py": "import httpx\n\n\ndef send(o):\n"
                                       "    httpx.post('https://mail.example', json=o)\n"})
    finish_flow(gl, run, "develop")
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED
    assert "external" in run.error.lower()
    assert "rewind:blocked" in gl.issues[issue]["labels"]
    assert not any(c[0] == "merge_merge_request" for c in gl.calls)


def test_failing_pipeline_gets_one_fix_then_regated(orch, gl):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    orch.advance(run.id)
    mr = agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 1")
    finish_flow(gl, orch.repo.get(run.id), "develop")
    run = orch.advance(run.id)
    gl.add_pipeline("agent/feature", gl.get_merge_request(mr)["sha"], "failed", mr_iid=mr)
    run = orch.advance(run.id)
    assert run.state is RunState.FIXING
    gl.commit_files("agent/feature", {"app/tests/test_x.py": "def test_x():\n    assert True\n"})
    finish_flow(gl, run, "fix")
    run = orch.advance(run.id)
    assert run.state is RunState.TESTING  # new commit was gated again
    assert run.gated_sha == gl.get_merge_request(mr)["sha"]


def test_runs_are_serialized_per_project(orch, gl):
    a = orch.start_run(gl.add_issue("A", labels=["rewind:auto"]))
    b = orch.start_run(gl.add_issue("B", labels=["rewind:auto"]))
    assert a.state is RunState.PLANNING
    assert b.state is RunState.QUEUED
    assert orch.start_run(a.issue_iid) is None  # no duplicate run for the same issue


def test_merge_refused_if_head_moved_after_gate(orch, gl, cr):
    issue = gl.add_issue("Promo", labels=["rewind:auto"])
    run = orch.start_run(issue)
    finish_flow(gl, run, "plan")
    orch.advance(run.id)
    mr = agent_opens_mr(gl, issue, "def new_pricing_total(items):\n    return sum(items) - 1")
    finish_flow(gl, orch.repo.get(run.id), "develop")
    run = orch.advance(run.id)
    gl.add_pipeline("agent/feature", gl.get_merge_request(mr)["sha"], "success", mr_iid=mr)
    run = orch.advance(run.id)
    finish_flow(gl, run, "review")
    # Someone pushes an ungated commit after review, before merge.
    gl.commit_files("agent/feature", {"app/receipts.py": "import smtplib\n"})
    run = orch.advance(run.id)
    assert run.state is RunState.BLOCKED  # re-gated, and the new commit is irreversible
    assert gl.mrs[mr]["state"] == "opened"


def test_draft_merge_request_is_marked_ready_before_merge(orch, gl, cr):
    issue = gl.add_issue("Promo pricing", labels=["rewind:auto"])
    run = drive_to_merge(orch, gl, cr, orch.start_run(issue), issue)
    mr = gl.mrs[run.mr_iid]
    assert mr["state"] == "merged"
    assert not mr["title"].lower().startswith("draft")
    assert any("marked the merge request ready" in e.message for e in run.timeline)
