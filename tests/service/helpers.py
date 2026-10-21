"""Shared helpers for the orchestrator and feature tests."""

from __future__ import annotations

from rewind.runs import RunState

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
