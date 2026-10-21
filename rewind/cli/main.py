"""`rewind` command line."""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from rewind.cloudrun import CloudRunClient
from rewind.config import ConfigError, Settings
from rewind.engine import Rewind, RewindError
from rewind.gitlab import GitLabClient
from rewind.http import ApiError
from rewind.models import Outcome, Reversibility, RewindReport
from rewind.policy import Policy
from rewind.resources import Context
from rewind.store import open_store

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Rewind: Ctrl+Z for autonomous software delivery on GitLab.")

COLORS = {Reversibility.REVERSIBLE: "green", Reversibility.COMPENSATABLE: "yellow",
          Reversibility.IRREVERSIBLE: "red"}
OUTCOME_COLORS = {Outcome.RESTORED: "green", Outcome.COMPENSATED: "green", Outcome.ALREADY: "green",
                  Outcome.CONFLICT: "yellow", Outcome.FAILED: "red", Outcome.IRREVERSIBLE: "red",
                  Outcome.PLANNED: "cyan"}

LABELS = {
    "rewind:auto": ("#5b4bdb", "Hand this issue to Rewind's hands-off loop"),
    "rewind:running": ("#1f78d1", "A Rewind run is in progress"),
    "rewind:shipped": ("#1f8a5b", "Shipped to production hands-off"),
    "rewind:rewound": ("#8e44ad", "Rewound to its checkpoint"),
    "rewind:blocked": ("#c0392b", "Blocked by the reversibility gate"),
    "rewind:failed": ("#7f8c8d", "The run failed"),
    "rewind:reversible": ("#2ecc71", "Everything this change ships can be undone"),
    "rewind:needs-human": ("#e67e22", "Contains something Rewind cannot undo"),
    "rewind:restore": ("#95a5a6", "Restore merge request created by Rewind"),
}


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from None


def _engine(s: Settings | None = None) -> Rewind:
    s = s or _settings()
    try:
        s.require("gitlab_token", "project_id", "hash_key")
    except ConfigError as exc:
        typer.secho(f"✗ {exc}. Copy .env.example to .env and fill it in.", fg="red", err=True)
        raise typer.Exit(2) from None
    gitlab = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id)
    cloudrun = None
    if s.gcp_project and s.cloud_run_services:
        cloudrun = CloudRunClient(s.gcp_project, s.gcp_region, s.cloud_run_services)
    ctx = Context(gitlab=gitlab, cloudrun=cloudrun, policy=Policy.load(s.policy_path or None),
                  hash_key=s.hash_key.encode())
    return Rewind(ctx, open_store(s.store_url), s.project_id)


def _print_change_set(cs) -> None:
    by = cs.by_class()
    typer.echo(f"{len(cs.changes)} change(s) since checkpoint {cs.checkpoint_id}")
    for cls in Reversibility:
        if by[cls]:
            typer.secho(f"\n  {cls.value} ({len(by[cls])})", fg=COLORS[cls], bold=True)
            for c in by[cls]:
                typer.echo(f"    • {c.summary}")
    for kind, err in cs.blind_spots.items():
        typer.secho(f"\n  ⚠ blind spot: {kind}: {err}", fg="yellow")


def _print_report(r: RewindReport) -> None:
    for i, s in enumerate(r.steps, 1):
        typer.echo(f"  {i:>2}. ", nl=False)
        typer.secho(f"{s.outcome.value:<12}", fg=OUTCOME_COLORS[s.outcome], nl=False)
        detail = f"  ({s.detail})" if s.outcome in (Outcome.FAILED, Outcome.CONFLICT, Outcome.IRREVERSIBLE) else ""
        typer.echo(f" {s.action}{detail}")
    if r.dry_run:
        return
    total = (r.finished_at - r.started_at).total_seconds() if r.finished_at else 0
    typer.echo("")
    if r.mitigated_after_s is not None:
        typer.echo(f"  production mitigated in {r.mitigated_after_s:.1f}s")
    typer.echo(f"  fully restored in {total:.1f}s")
    if r.zero_drift:
        typer.secho("  verification: zero drift ✓", fg="green", bold=True)
    else:
        typer.secho("  verification: drift found", fg="red", bold=True)
        for d in (r.verification.drift if r.verification else []):
            typer.echo(f"    - {d}")
    typer.echo(f"  undo this rewind with: rewind undo-last  (pre-rewind checkpoint {r.pre_rewind_checkpoint_id})")


@app.command()
def checkpoint(label: Annotated[str, typer.Option(help="Note stored with the checkpoint")] = "") -> None:
    """Snapshot the project now."""
    cp = _engine().checkpoint(label)
    typer.secho(f"✓ checkpoint {cp.id}", fg="green", bold=True)
    typer.echo(f"  default branch {cp.default_branch} @ {cp.default_sha[:12]}")
    for env, rev in cp.serving.items():
        typer.echo(f"  {env}: {rev}")


@app.command("list")
def list_checkpoints(all_: Annotated[bool, typer.Option("--all", help="Include pre-rewind checkpoints")] = False,
                     limit: int = 20) -> None:
    """List checkpoints, newest first."""
    for cp in _engine().list_checkpoints(None if all_ else "checkpoint")[:limit]:
        typer.echo(f"{cp.id}  {cp.created_at:%Y-%m-%d %H:%M:%S}  {cp.default_sha[:8]}  "
                   f"{'#' + str(cp.issue_iid) if cp.issue_iid else '':<6} {cp.label}")


@app.command()
def show(checkpoint_id: str) -> None:
    """Show what a checkpoint captured."""
    eng = _engine()
    cp = eng.get_checkpoint(checkpoint_id)
    snap = eng.load_snapshot(cp.snapshot_id)
    typer.echo(json.dumps(cp.model_dump(mode="json"), indent=2))
    typer.echo("captured: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(snap.resources.items())))
    if snap.errors:
        typer.secho(f"blind spots: {snap.errors}", fg="yellow")


@app.command()
def diff(checkpoint_id: str, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """What changed since a checkpoint, grouped by reversibility."""
    cs = _engine().diff(checkpoint_id)
    if json_out:
        typer.echo(cs.model_dump_json(indent=2))
    else:
        _print_change_set(cs)


@app.command("to")
def rewind_to(checkpoint_id: str,
              dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the inverse plan only")] = False,
              yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False) -> None:
    """Rewind the project to a checkpoint."""
    eng = _engine()
    plan = eng.plan(checkpoint_id)
    assert plan.change_set is not None
    _print_change_set(plan.change_set)
    typer.secho("\nInverse plan (in order):", bold=True)
    _print_report(plan)
    if dry_run or not plan.steps:
        return
    if not yes:
        typer.confirm("\nApply this rewind?", abort=True)
    t0 = time.monotonic()
    report = eng.rewind(checkpoint_id, reason="manual (CLI)")
    typer.secho(f"\n⏪ rewind {report.id} finished in {time.monotonic() - t0:.1f}s", bold=True)
    _print_report(report)


@app.command("undo-last")
def undo_last(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Undo the most recent rewind."""
    eng = _engine()
    if not yes:
        typer.confirm("Undo the most recent rewind?", abort=True)
    try:
        report = eng.undo_last()
    except RewindError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from None
    _print_report(report)


@app.command()
def runs() -> None:
    """List autonomous runs."""
    from service.repo import RunRepository

    s = _settings()
    for r in RunRepository(open_store(s.store_url), s.project_id).summaries():
        typer.echo(f"{r['id']}  #{r['issue_iid']:<4} {r['state']:<10} gate={r.get('gate') or '-':<8} "
                   f"{r.get('checkpoint_id') or ''}  {r['issue_title']}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = int(os.environ.get("PORT", "8080"))) -> None:
    """Run the Rewind service locally."""
    import uvicorn

    uvicorn.run("service.main:app", factory=True, host=host, port=port)


# -- setup ------------------------------------------------------------------
def _check(label: str, fn) -> bool:
    try:
        detail = fn()
    except Exception as exc:
        typer.secho(f"  ✗ {label}: {exc}"[:300], fg="red")
        return False
    typer.secho(f"  ✓ {label}" + (f": {detail}" if detail else ""), fg="green")
    return True


@app.command()
def doctor() -> None:
    """Check configuration and access before running anything."""
    s = _settings()
    ok = True
    typer.secho("Configuration", bold=True)
    for name, env in [("gitlab_token", "GITLAB_TOKEN"), ("project_id", "GITLAB_PROJECT_ID"),
                      ("hash_key", "REWIND_HASH_KEY"), ("webhook_secret", "GITLAB_WEBHOOK_SECRET"),
                      ("api_token", "REWIND_API_TOKEN")]:
        present = bool(getattr(s, name))
        ok &= present
        typer.secho(f"  {'✓' if present else '✗'} {env}", fg="green" if present else "red")
    if not (s.gitlab_token and s.project_id):
        raise typer.Exit(1)
    gl = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id)
    typer.secho("GitLab", bold=True)
    ok &= _check("token", lambda: "@" + gl.current_user()["username"])

    def project():
        p = gl.project()
        perms = p.get("permissions") or {}
        access = max((perms.get(k) or {}).get("access_level", 0) for k in ("project_access", "group_access"))
        if access < 40:
            raise RuntimeError(f"{p['path_with_namespace']}: need Maintainer (40), have {access}")
        return f"{p['path_with_namespace']} (default branch {p['default_branch']}, access {access})"
    ok &= _check("project", project)
    ok &= _check("Duo Agent Platform privileges",
                 lambda: ", ".join(str(p["name"]).lower() for p in gl.agent_privileges()))
    ok &= _check("feature flags API", lambda: f"{len(gl.list_feature_flags())} flag(s)")
    ok &= _check("CI/CD variables API", lambda: f"{len(gl.list_variables())} variable(s)")
    typer.secho("Rewind", bold=True)
    typer.echo("  flows: " + ", ".join(f"{k}={v}" for k, v in s.flows.items()))
    ok &= _check("policy", lambda: f"{len(Policy.load(s.policy_path or None).classify)} classify rules")
    ok &= _check(f"store {s.store_url}", lambda: open_store(s.store_url).put("doctor.json", {"at": time.time()}))
    if s.gcp_project:
        typer.secho("Google Cloud", bold=True)
        cr = CloudRunClient(s.gcp_project, s.gcp_region, s.cloud_run_services)
        for env in s.cloud_run_services:
            ok &= _check(f"Cloud Run {env}", lambda env=env: (cr.serving(env) or {}).get("serving") or
                         "service not deployed yet (first pipeline creates it)")
    else:
        typer.secho("Google Cloud: GCP_PROJECT_ID not set (Cloud Run steps disabled)", fg="yellow")
    raise typer.Exit(0 if ok else 1)


@app.command()
def bootstrap(webhook: Annotated[bool, typer.Option(help="Register the project webhook")] = True,
              ci_vars: Annotated[bool, typer.Option(help="Push non-secret deploy settings to CI/CD variables")] = True
              ) -> None:
    """Create labels, the demo feature flag, CI/CD variables and the webhook. Safe to re-run."""
    s = _settings()
    s.require("gitlab_token", "project_id")
    gl = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id)
    existing = {lbl["name"] for lbl in gl.list_labels()}
    for name, (color, desc) in LABELS.items():
        if name not in existing:
            gl.create_label(name, color, desc)
            typer.secho(f"  ✓ label {name}", fg="green")
    if not gl.get_feature_flag("new_pricing"):
        gl.create_feature_flag({"name": "new_pricing", "version": "new_version_flag", "active": False,
                                "description": "New pricing rules (managed by config/flags.yaml)",
                                "strategies": [{"name": "default", "parameters": {},
                                                "scopes": [{"environment_scope": "*"}]}]})
        typer.secho("  ✓ feature flag new_pricing (off)", fg="green")

    if ci_vars:
        wanted = {
            "GCP_PROJECT_ID": (s.gcp_project, False), "GCP_REGION": (s.gcp_region, False),
            "GCP_ARTIFACT_REPO": (os.environ.get("GCP_ARTIFACT_REPO", "rewind"), False),
            "GCP_WIF_PROVIDER": (os.environ.get("GCP_WIF_PROVIDER", ""), False),
            "GCP_DEPLOY_SERVICE_ACCOUNT": (os.environ.get("GCP_DEPLOY_SERVICE_ACCOUNT", ""), False),
            "CLOUD_RUN_APP_SERVICE_ACCOUNT": (os.environ.get("CLOUD_RUN_APP_SERVICE_ACCOUNT", ""), False),
            "CLOUD_RUN_SERVICE_STAGING": (s.cloud_run_services.get("staging", ""), False),
            "CLOUD_RUN_SERVICE_PRODUCTION": (s.cloud_run_services.get("production", ""), False),
            "UNLEASH_INSTANCE_ID": (os.environ.get("UNLEASH_INSTANCE_ID", ""), True),
        }
        current = {v["key"] for v in gl.list_variables()}
        for key, (value, masked) in wanted.items():
            if not value:
                typer.secho(f"  - {key} not set in .env; skipped", fg="yellow")
                continue
            if key in current:
                gl.update_variable(key, "*", value=value, masked=masked)
            else:
                gl.create_variable(key, value, masked=masked)
            typer.secho(f"  ✓ CI/CD variable {key}", fg="green")
        if "FLAGS_TOKEN" not in current:
            _create_flags_token(gl)

    if webhook:
        if not (s.public_url and s.webhook_secret):
            typer.secho("  - REWIND_PUBLIC_URL / GITLAB_WEBHOOK_SECRET not set; webhook skipped "
                        "(re-run after deploying the service)", fg="yellow")
        else:
            url = f"{s.public_url}/webhooks/gitlab"
            payload = {"url": url, "token": s.webhook_secret, "issues_events": True,
                       "merge_requests_events": True, "pipeline_events": True, "job_events": True,
                       "note_events": True, "push_events": False, "enable_ssl_verification": True}
            hook = next((h for h in gl.list_hooks() if h["url"] == url), None)
            if hook:
                gl.update_hook(hook["id"], payload)
            else:
                gl.create_hook(payload)
            typer.secho(f"  ✓ webhook → {url}", fg="green")
    typer.secho("Bootstrap complete.", bold=True)


def _create_flags_token(gl: GitLabClient) -> None:
    """A narrowly scoped project token the pipeline's `configure` stage uses to apply config/flags.yaml."""
    expires = (datetime.now(UTC) + timedelta(days=45)).date().isoformat()
    try:
        tok = gl.create_project_access_token("rewind-configure-flags", ["api"], 30, expires)
    except ApiError as exc:
        typer.secho(f"  ✗ could not create a project access token ({exc.status}). Create one manually "
                    f"(Settings > Access tokens, role Developer, scope api) and store it as masked CI/CD "
                    f"variable FLAGS_TOKEN.", fg="yellow")
        return
    gl.create_variable("FLAGS_TOKEN", tok["token"], masked=True, protected=True)
    typer.secho(f"  ✓ project access token for flag changes → masked CI/CD variable FLAGS_TOKEN "
                f"(expires {expires})", fg="green")


@app.command()
def spike(trigger: Annotated[bool, typer.Option(help="Actually start a flow (uses GitLab Credits)")] = False,
          definition: str = "developer/v1",
          timeout_min: int = 25,
          out: Path = Path("docs/spike-results.md")) -> None:
    """Probe what the Duo Agent Platform supports in this namespace; write docs/spike-results.md."""
    s = _settings()
    s.require("gitlab_token", "project_id")
    gl = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id)
    rows: list[tuple[str, str, str]] = []

    def probe(name: str, fn):
        t0 = time.monotonic()
        try:
            detail = fn()
            rows.append((name, "✅ works", f"{detail} ({time.monotonic() - t0:.1f}s)"))
            typer.secho(f"✓ {name}: {detail}", fg="green")
            return detail
        except Exception as exc:
            rows.append((name, "❌ failed", f"{type(exc).__name__}: {str(exc)[:300]}"))
            typer.secho(f"✗ {name}: {exc}"[:400], fg="red")
            return None

    probe("Token and project", lambda: f"@{gl.current_user()['username']} on {gl.project()['path_with_namespace']}")
    privs = probe("List agent privileges", lambda: ", ".join(f"{p['id']}={str(p['name']).lower()}"
                                                            for p in gl.agent_privileges()))

    base = {"project_id": s.project_id, "workflow_definition": definition, "environment": "ambient",
            "goal": "Add a file docs/spike.md containing the single line 'Duo flow spike OK' and open a "
                    "merge request titled 'Spike: Duo flow'."}
    probe("Create flow without starting it",
          lambda: "id " + str(gl.start_workflow({**base, "start_workflow": False})["id"]))

    def callback_probe():
        hooks = gl.list_hooks()
        if not hooks:
            return "skipped: no project webhook to reference yet (run `rewind bootstrap` first)"
        resp = gl.start_workflow({**base, "start_workflow": False, "callback_hook_id": hooks[0]["id"],
                                  "client_reference": "rewind-spike"})
        return f"accepted (id {resp['id']}): callbacks usable"
    probe("Callback hooks (callback_hook_id)", callback_probe)

    if trigger:
        started = probe("Start flow " + definition,
                        lambda: gl.start_workflow({**base, "start_workflow": True}))
        if started:
            wid, t0, last = started["id"], time.monotonic(), None
            transitions = []
            while time.monotonic() - t0 < timeout_min * 60:
                try:
                    status = gl.workflow_status(wid)["status"]
                except Exception as exc:
                    status = f"unreadable ({exc})"
                if status != last:
                    transitions.append(f"{time.monotonic() - t0:.0f}s {status}")
                    typer.echo(f"  {time.monotonic() - t0:5.0f}s  {status}")
                    last = status
                if status in ("finished", "completed", "failed", "stopped", "input_required",
                              "tool_call_approval_required", "plan_approval_required"):
                    break
                time.sleep(10)
            rows.append(("Status polling (GraphQL)", "✅" if last and "unreadable" not in last else "❌",
                         " → ".join(transitions)))

            def trace():
                lines = [json.loads(x) for x in gl.workflow_trace(wid).splitlines() if x.strip()]
                tools: dict[str, int] = {}
                for entry in lines:
                    name = (entry.get("tool_info") or {}).get("name") or entry.get("tool_name")
                    if name:
                        tools[name] = tools.get(name, 0) + 1
                return f"{len(lines)} entries; tool calls: {tools or 'none recorded'}"
            probe("Session trace (trace.jsonl)", trace)
            def spike_mr():
                found = gl.list_merge_requests(state="opened", search="Spike")
                if not found:
                    raise RuntimeError("no open MR titled 'Spike' found")
                return ", ".join(f"!{m['iid']} {m['title']}" for m in found)
            probe("Merge request created", spike_mr)

    out.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# Duo Agent Platform spike — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", "",
            f"Project `{s.project_id}`, flow `{definition}`, triggered: {trigger}.", "",
            "| Probe | Result | Detail |", "|---|---|---|"]
    body += [f"| {a} | {b} | {c.replace('|', '/')} |" for a, b, c in rows]
    body += ["", "Agent privileges available:", "", f"`{privs}`" if privs else "unknown", ""]
    out.write_text("\n".join(body) + "\n", encoding="utf-8")
    typer.secho(f"\nWrote {out}", bold=True)


@app.command()
def approve(run_id: str,
            reason: Annotated[str, typer.Option(help="Why a human accepts what Rewind can't undo")],
            by: Annotated[str, typer.Option(help="Your name, recorded in the ledger")] = os.environ.get("USER", "")
            ) -> None:
    """Accept a blocked run's irreversible change, for the MR's current commit only.

    Deliberately not a comment command: agents comment with your identity.
    """
    import httpx

    s = _settings()
    s.require("public_url", "api_token")
    resp = httpx.post(f"{s.public_url}/api/runs/{run_id}/approve", json={"by": by, "reason": reason},
                      headers={"Authorization": f"Bearer {s.api_token}"}, timeout=120)
    if resp.status_code >= 400:
        typer.secho(f"✗ {resp.status_code}: {resp.json().get('detail', resp.text)}", fg="red")
        raise typer.Exit(1)
    body = resp.json()
    typer.secho(f"✓ {by} approved run {body['run']}; it is now {body['state']}", fg="green", bold=True)


@app.command()
def keys() -> None:
    """Generate the random secrets .env needs."""
    typer.echo(f"REWIND_HASH_KEY={secrets.token_hex(32)}")
    typer.echo(f"REWIND_API_TOKEN={secrets.token_urlsafe(32)}")
    typer.echo(f"GITLAB_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    app()
