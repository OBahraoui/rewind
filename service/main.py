"""Rewind service: webhooks in, autonomous runs driven, dashboard and MCP out."""

from __future__ import annotations

import contextlib
import hmac
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from rewind import ledger
from rewind.cloudrun import CloudRunClient
from rewind.config import Settings
from rewind.engine import Rewind, RewindError
from rewind.flows import Flows
from rewind.gitlab import GitLabClient
from rewind.logging_setup import configure_logging
from rewind.models import Reversibility
from rewind.policy import Policy
from rewind.resources import Context
from rewind.runs import RunState
from rewind.store import LockHeld, Store, open_store
from service.mcp_server import build_mcp
from service.orchestrator import Orchestrator
from service.repo import RunRepository

log = logging.getLogger("rewind.service")
HERE = Path(__file__).parent


class RewindRequest(BaseModel):
    checkpoint_id: str
    dry_run: bool = False
    reason: str = "manual"


class ApproveRequest(BaseModel):
    by: str
    reason: str


def create_app(settings: Settings | None = None, *, gitlab: Any = None, cloudrun: Any = None,
               store: Store | None = None, run_ticker: bool = True) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    settings.require("gitlab_token", "project_id", "hash_key", "webhook_secret", "api_token")
    policy = Policy.load(settings.policy_path or None)
    gitlab = gitlab or GitLabClient(settings.gitlab_url, settings.gitlab_token, settings.project_id)
    if cloudrun is None and settings.gcp_project and settings.cloud_run_services:
        cloudrun = CloudRunClient(settings.gcp_project, settings.gcp_region, settings.cloud_run_services)
    store = store or open_store(settings.store_url)
    ctx = Context(gitlab=gitlab, cloudrun=cloudrun, policy=policy, hash_key=settings.hash_key.encode())
    engine = Rewind(ctx, store, settings.project_id)
    repo = RunRepository(store, settings.project_id)
    flows = Flows(gitlab, settings.project_id, settings.flows, policy)
    orch = Orchestrator(settings, engine, repo, flows)
    mcp_server, mcp_app = build_mcp(engine, repo, settings.public_url)
    stop = threading.Event()

    def ticker() -> None:
        while not stop.wait(settings.tick_seconds):
            try:
                orch.tick()
            except Exception:
                log.exception("tick failed")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        thread = threading.Thread(target=ticker, name="rewind-ticker", daemon=True)
        if run_ticker:
            thread.start()
        async with mcp_server.session_manager.run():
            yield
        stop.set()

    app = FastAPI(title="Rewind", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")
    app.state.engine, app.state.repo, app.state.orchestrator = engine, repo, orch
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/mcp", mcp_app)
    templates = Jinja2Templates(directory=HERE / "templates")

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header.encode(), f"Bearer {settings.api_token}".encode()):
            raise HTTPException(401, "missing or wrong bearer token")

    # -- health & webhooks ---------------------------------------------------
    # Cloud Run reserves URL paths ending in "z" (its front end answers /healthz itself), so /health is the
    # public check; /healthz stays for local runs and container probes.
    @app.get("/health")
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "project": settings.project_id}

    @app.post("/webhooks/gitlab")
    async def webhook(request: Request, background: BackgroundTasks) -> dict:
        token = request.headers.get("x-gitlab-token", "")
        if not hmac.compare_digest(token.encode(), settings.webhook_secret.encode()):
            raise HTTPException(401, "bad webhook token")
        event = request.headers.get("x-gitlab-event", "")
        payload = await request.json()
        # Acknowledge fast: GitLab disables hooks that are slow or failing.
        background.add_task(dispatch, orch, repo, settings, event, payload)
        return {"accepted": True, "event": event}

    # -- JSON API ------------------------------------------------------------
    @app.get("/api/runs")
    def api_runs() -> list[dict]:
        return repo.summaries()

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict:
        run = repo.get(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        cs = repo.change_set(run_id)
        return {"run": run.model_dump(mode="json"), "change_set": cs.model_dump(mode="json") if cs else None}

    @app.get("/api/checkpoints")
    def api_checkpoints() -> list[dict]:
        return [c.model_dump(mode="json") for c in engine.list_checkpoints()]

    @app.get("/api/rewinds/{rewind_id}")
    def api_rewind(rewind_id: str) -> dict:
        try:
            return engine.get_report(rewind_id).model_dump(mode="json")
        except RewindError:
            raise HTTPException(404, "rewind not found") from None

    @app.post("/api/rewind")
    def api_do_rewind(body: RewindRequest, request: Request) -> dict:
        require_token(request)
        try:
            report = engine.rewind(body.checkpoint_id, dry_run=body.dry_run, reason=body.reason)
        except RewindError as exc:
            raise HTTPException(404, str(exc)) from None
        except LockHeld as exc:
            raise HTTPException(409, str(exc)) from None
        return report.model_dump(mode="json")

    @app.post("/api/tick")
    def api_tick(request: Request) -> dict:
        require_token(request)
        orch.tick()
        return {"ok": True}

    @app.post("/api/runs/{run_id}/approve")
    def api_approve(run_id: str, body: ApproveRequest, request: Request) -> dict:
        """The only way to accept what Rewind can't undo: authenticated, named, reasoned, bound to one commit."""
        require_token(request)
        try:
            run = orch.approve(run_id, by=body.by, reason=body.reason)
        except LookupError:
            raise HTTPException(404, "run not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        approvals = [a.model_dump(mode="json") for a in run.approvals]
        return {"run": run.id, "state": run.state.value, "approvals": approvals}

    # -- dashboard -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        runs = repo.summaries()
        stats = {
            "runs": len(runs),
            "shipped": sum(r["state"] == "shipped" for r in runs),
            "rewound": sum(r["state"] == "rewound" for r in runs),
            "blocked": sum(r["state"] == "blocked" for r in runs),
            "flows_today": repo.flows_started_today(),
            "flow_limit": settings.max_flows_per_day,
            "active": any(r["state"] not in ("shipped", "rewound", "blocked", "failed") for r in runs),
        }
        return templates.TemplateResponse(request, "index.html", {
            "runs": runs, "stats": stats, "status": {s.value: ledger.STATUS[s] for s in RunState},
            "project": engine.ctx.meta.get("path") or settings.project_id,
        })

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        run = repo.get(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        report = None
        if run.rewind_id:
            with contextlib.suppress(RewindError):
                report = engine.get_report(run.rewind_id)
        cs = report.change_set if report and report.change_set else repo.change_set(run_id)
        groups = cs.by_class() if cs else {r: [] for r in Reversibility}
        return templates.TemplateResponse(request, "run.html", {
            "run": run, "status": ledger.STATUS[run.state], "groups": groups, "change_set": cs,
            "report": report, "stages": ledger.STAGES,
        })

    @app.exception_handler(LockHeld)
    def _locked(_request: Request, exc: LockHeld):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app


def dispatch(orch: Orchestrator, repo: RunRepository, settings: Settings, event: str, payload: dict) -> None:
    """Route one GitLab webhook to the runs it concerns."""
    try:
        kind = payload.get("object_kind")
        if event == "Issue Hook" or kind == "issue":
            attrs = payload.get("object_attributes", {})
            labels = {lbl.get("title") for lbl in payload.get("labels", [])}
            if settings.trigger_label not in labels or attrs.get("state") != "opened":
                return
            previous = (payload.get("changes", {}).get("labels") or {}).get("previous")
            just_added = previous is not None and settings.trigger_label not in {p.get("title") for p in previous}
            if attrs.get("action") in ("open", "reopen") or just_added:
                orch.start_run(int(attrs["iid"]))
        elif event == "Merge Request Hook" or kind == "merge_request":
            for run in repo.find(mr_iid=int(payload["object_attributes"]["iid"])):
                orch.advance(run.id)
        elif event == "Note Hook" or kind == "note":
            attrs = payload.get("object_attributes", {})
            text = attrs.get("note") or ""
            if not text.lstrip().lower().startswith("/rewind"):
                return
            user = payload.get("user") or {}
            noteable = attrs.get("noteable_type")
            orch.command(
                text=text.strip(), author_id=int(user.get("id") or 0), author=user.get("username") or "unknown",
                issue_iid=(payload.get("issue") or {}).get("iid") if noteable == "Issue" else None,
                mr_iid=(payload.get("merge_request") or {}).get("iid") if noteable == "MergeRequest" else None,
            )
        elif event in ("Pipeline Hook", "Job Hook") or kind in ("pipeline", "build"):
            attrs = payload.get("object_attributes", {})
            sha = attrs.get("sha") or payload.get("sha") or payload.get("checkout_sha")
            mr = (payload.get("merge_request") or {}).get("iid")
            runs = repo.find(mr_iid=int(mr)) if mr else []
            runs += repo.find(sha=sha) if sha else []
            for run in {r.id: r for r in runs}.values():
                orch.advance(run.id)
    except Exception:
        log.exception("webhook dispatch failed", extra={"event": event})


def _app() -> FastAPI:  # uvicorn entry point: `uvicorn service.main:app --factory`
    return create_app()


app = _app
