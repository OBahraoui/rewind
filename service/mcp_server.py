"""Read-only MCP tools so Duo agents can ask Rewind *before* they act.

An agent that can call `classify_action` or `check_paths` takes the reversible path
first, instead of learning about the gate when its MR is blocked.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from rewind import ledger
from rewind.engine import Rewind
from rewind.models import Change
from rewind.policy import path_matches
from service.repo import RunRepository


def build_mcp(engine: Rewind, repo: RunRepository, public_url: str = "") -> tuple[MCPServer, Any]:
    policy = engine.ctx.policy
    server = MCPServer(
        name="rewind",
        title="Rewind",
        instructions=("Rewind undoes what agents change on this GitLab project. Before an action, call "
                      "classify_action or check_paths: reversible and compensatable actions are allowed "
                      "hands-off; irreversible ones are blocked and need a human."),
    )

    @server.tool(description="The project's reversibility policy: classes, rules, protected paths, privileges.")
    def rewind_policy() -> dict[str, Any]:
        return {
            "principle": "Let agents do anything Rewind can undo; stop them only for what it can't.",
            "classify": [{"kind": r.kind, "op": r.op, "class": r.cls.value, "reason": r.reason}
                         for r in policy.classify],
            "protected_paths": policy.gate.protected_paths,
            "gate_rules": [{"id": r.id, "paths": r.paths, "exclude_paths": r.exclude_paths, "verdict": r.verdict,
                            "reason": r.reason, "instruction": r.instruction} for r in policy.gate.rules],
            "agent_privileges": policy.agent_privileges.model_dump(),
            "review": policy.review.model_dump(),
        }

    @server.tool(description="Classify a planned action, e.g. kind=feature_flag op=updated, or "
                             "kind=ci_variable op=updated fields=['value_fp'].")
    def classify_action(kind: str, op: str, fields: list[str] | None = None,
                        after_state: str | None = None) -> dict[str, str]:
        change = Change(id=f"{kind}:?", kind=kind, key="?", op=op, fields=fields or [],
                        before={}, after={"state": after_state} if after_state else {})
        cls, reason = policy.classify_change(change)
        return {"class": cls.value, "reason": reason,
                "allowed_hands_off": "no" if cls.value == "irreversible" else "yes"}

    @server.tool(description="Which of these repository paths agents may not change, and which gate rules apply.")
    def check_paths(paths: list[str]) -> list[dict[str, Any]]:
        out = []
        for p in paths:
            rules = [r.id for r in policy.gate.rules if any(path_matches(p, pat) for pat in r.paths)
                     and not any(path_matches(p, pat) for pat in r.exclude_paths)]
            out.append({"path": p, "protected": policy.is_protected(p), "gate_rules": rules})
        return out

    @server.tool(description="Recent checkpoints, newest first.")
    def list_checkpoints(limit: int = 10) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in engine.list_checkpoints()[: max(1, min(limit, 50))]]

    @server.tool(description="The ledger of an autonomous run, by run id or issue iid, as Markdown.")
    def get_ledger(run_id: str | None = None, issue_iid: int | None = None) -> str:
        run = repo.get(run_id) if run_id else (repo.latest_for_issue(issue_iid) if issue_iid else None)
        if not run:
            return "No such run."
        report = engine.get_report(run.rewind_id) if run.rewind_id else None
        return ledger.render(run, change_set=repo.change_set(run.id), report=report)

    @server.tool(description="Everything that changed since a checkpoint, grouped by reversibility (live diff).")
    def explain_checkpoint(checkpoint_id: str) -> str:
        cs = engine.diff(checkpoint_id)
        return "\n".join(ledger.render_change_set(cs))

    hosts = ["127.0.0.1:*", "localhost:*", "testserver"]
    origins = ["http://127.0.0.1:*", "http://localhost:*"]
    if public_url:
        host = urlparse(public_url).netloc
        hosts.append(host)
        origins.append(f"https://{host}")
    app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
    )
    return server, app
