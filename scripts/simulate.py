#!/usr/bin/env python3
"""Run Rewind's hands-off loop offline: the real engine, gate, orchestrator and dashboard
against in-memory GitLab and Cloud Run, with scripted agent and pipeline behaviour.

  python scripts/simulate.py            # run the three scenarios, then serve the dashboard on :8080
  python scripts/simulate.py --no-serve # just print the ledgers

This is a development aid. The submission demo is always a real run on GitLab.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

PRICING = Path(__file__).parents[1].joinpath("app", "pricing.py").read_text()
LEGACY = "def legacy_total(lines: list[Line]) -> int:\n    return subtotal(lines)"
NEW = "def new_pricing_total(lines: list[Line]) -> int:\n    return subtotal(lines)"


def build(tmp: str):
    from rewind.config import FlowRef, Settings
    from rewind.store import FileStore
    from rewind.testing import FakeCloudRun, FakeGitLab
    from service.main import create_app

    gl = FakeGitLab(files={"app/pricing.py": PRICING, "README.md": "# shop\n",
                           "config/flags.yaml": "new_pricing:\n  staging: true\n  production: false\n"})
    gl.add_flag("new_pricing", active=False)
    gl.set_variable("API_URL", "https://api.internal")
    cr = FakeCloudRun()
    refs = {"plan": "catalog:1", "develop": "definition:developer/v1", "review": "definition:code_review/v1",
            "fix": "definition:fix_pipeline/v1"}
    settings = Settings(project_id="42", gitlab_token="sim", hash_key="k" * 32, webhook_secret="sim",
                        api_token="sim", public_url="http://127.0.0.1:8080",
                        flows={k: FlowRef.parse(v) for k, v in refs.items()})
    app = create_app(settings, gitlab=gl, cloudrun=cr, store=FileStore(tmp), run_ticker=False)
    return app, gl, cr


def run_scenario(app, gl, cr, title: str, pricing_change: str, *, monitor_ok: bool, extra: dict | None = None,
                 touch_legacy: bool = False):
    orch = app.state.orchestrator
    issue = gl.add_issue(title, labels=["rewind:auto"])
    run = orch.start_run(issue)

    def finish(step):
        gl.workflows[run.current_flow(step).workflow_id]["status"] = "finished"

    finish("plan")
    gl.create_issue_note(issue, "### 🧭 Plan\n1. Implement in `new_pricing_total()`\n### Reversibility\n...")
    run = orch.advance(run.id)
    branch = f"duo/issue-{issue}"
    target = LEGACY if touch_legacy else NEW
    changed = PRICING.replace(target, target.replace("return subtotal(lines)", pricing_change))
    gl.commit_files(branch, {"app/pricing.py": changed,
                             "config/flags.yaml": "new_pricing:\n  staging: true\n  production: true\n",
                             f"app/tests/test_issue_{issue}.py": "def test_ok():\n    assert True\n",
                             **(extra or {})})
    mr = gl.create_merge_request(branch, "main", title, description=f"Closes #{issue}")["iid"]
    finish("develop")
    run = orch.advance(run.id)
    if run.state.value == "rerouting":
        fixed = PRICING.replace(NEW, NEW.replace("return subtotal(lines)", pricing_change))
        gl.commit_files(branch, {"app/pricing.py": fixed})
        finish("develop")
        run = orch.advance(run.id)
    if run.state.terminal:
        return run
    sha = gl.get_merge_request(mr)["sha"]
    gl.add_pipeline(branch, sha, "success", mr_iid=mr, jobs=[
        {"name": "test:app", "stage": "verify", "status": "success", "web_url": "#"},
        {"name": "semgrep-sast", "stage": "secure", "status": "success", "web_url": "#"},
        {"name": "build-image", "stage": "package", "status": "success", "web_url": "#"}])
    run = orch.advance(run.id)
    finish("review")
    run = orch.advance(run.id)
    cr.deploy("staging")
    cr.deploy("production")
    gl.update_feature_flag("new_pricing", {"active": True})
    jobs = [{"name": n, "stage": s, "status": "success", "web_url": "#"} for n, s in
            [("deploy:staging", "staging"), ("deploy:production", "production"),
             ("configure:production", "configure")]]
    jobs.append({"name": "monitor:production", "stage": "monitor", "status": "success" if monitor_ok else "failed",
                 "web_url": "#"})
    if monitor_ok:
        gl.create_tag(f"v1.0.{issue}", "main")
        gl.create_release(f"v1.0.{issue}", f"v1.0.{issue}", title)
        jobs.append({"name": "release", "stage": "release", "status": "success", "web_url": "#"})
    gl.add_pipeline("main", run.merge_sha, "success" if monitor_ok else "failed", jobs=jobs)
    return orch.advance(run.id)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-serve", action="store_true")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        app, gl, cr = build(tmp)
        bulk = "n = sum(q for _, q in lines)\n    s = subtotal(lines)\n    return s - s // 10 if n >= 10 else s"
        receipts = ("import httpx\n\n\ndef send(order):\n"
                    "    httpx.post('https://receipts.example.com/v1/send', json=order)\n")
        runs = [
            run_scenario(app, gl, cr, "Bulk discount: 10% off 10+ items", bulk, monitor_ok=True),
            run_scenario(app, gl, cr, "Launch promo: $15 off every order", "return subtotal(lines) - 1500",
                         monitor_ok=False, touch_legacy=True),
            run_scenario(app, gl, cr, "Email a receipt when an order is placed", "return subtotal(lines)",
                         monitor_ok=True, extra={"app/receipts.py": receipts}),
        ]
        for run in runs:
            note = next(n for n in gl.issue_notes[run.issue_iid] if "Rewind ledger" in n["body"])
            print(f"\n{'=' * 100}\n#{run.issue_iid} {run.issue_title} → {run.state.value}\n{'=' * 100}")
            print(note["body"])
        if not args.no_serve:
            import uvicorn

            print(f"\nDashboard: http://127.0.0.1:{args.port}/")
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
