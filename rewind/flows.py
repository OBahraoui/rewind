"""Start and track GitLab Duo Agent Platform flows with least privilege."""

from __future__ import annotations

import logging
from typing import Any

from rewind.config import FlowRef
from rewind.policy import Policy
from rewind.runs import FlowRun

log = logging.getLogger("rewind.flows")

DONE = {"finished", "completed"}
FAILED = {"failed", "stopped", "cancelled", "canceled", "error"}
# A hands-off run must never wait on a human: these mean the agent stalled.
NEEDS_HUMAN = {"input_required", "plan_approval_required", "tool_call_approval_required", "paused"}


def classify_status(status: str) -> str:
    s = status.lower()
    if s in DONE:
        return "done"
    if s in FAILED:
        return "failed"
    if s in NEEDS_HUMAN:
        return "needs_human"
    return "running"


class Flows:
    def __init__(self, gitlab: Any, project_id: str, refs: dict[str, FlowRef], policy: Policy):
        self.gitlab, self.project_id, self.refs, self.policy = gitlab, project_id, refs, policy
        self._privilege_ids: dict[str, int] | None = None

    def privilege_ids(self, names: list[str]) -> list[int]:
        if self._privilege_ids is None:
            self._privilege_ids = {str(p["name"]).lower(): int(p["id"]) for p in self.gitlab.agent_privileges()}
        unknown = [n for n in names if n not in self._privilege_ids]
        if unknown:
            log.warning("unknown agent privileges ignored", extra={"privileges": unknown})
        return [self._privilege_ids[n] for n in names if n in self._privilege_ids]

    def enabled(self, step: str) -> bool:
        ref = self.refs.get(step)
        return bool(ref and ref.enabled)

    def payload(self, step: str, goal: str, *, issue_iid: int | None = None, mr_iid: int | None = None,
                source_branch: str | None = None) -> dict[str, Any]:
        ref = self.refs[step]
        privileges = self.privilege_ids(self.policy.privileges_for(step))
        body: dict[str, Any] = {
            "project_id": self.project_id,
            "goal": goal,
            "start_workflow": True,
            "environment": "ambient",
            # Least privilege per step. The platform decides pre-approval for ambient flows;
            # Rewind fails any run that stalls waiting for approval.
            "agent_privileges": privileges,
            "pre_approved_agent_privileges": privileges,
        }
        if ref.kind == "definition":
            body["workflow_definition"] = ref.value
        else:
            body["ai_catalog_item_consumer_id"] = int(ref.value)
        if issue_iid is not None:
            body["issue_id"] = issue_iid
        if mr_iid is not None:
            body["merge_request_id"] = mr_iid
        if source_branch:
            body["source_branch"] = source_branch
        return body

    def start(self, step: str, goal: str, **kw: Any) -> FlowRun:
        body = self.payload(step, goal, **kw)
        resp = self.gitlab.start_workflow(body)
        flow = FlowRun(step=step, ref=str(self.refs[step]), workflow_id=int(resp["id"]),
                       status=str(resp.get("status", "created")).lower())
        log.info("flow started", extra={"step": step, "workflow_id": flow.workflow_id, "ref": flow.ref})
        return flow

    def poll(self, flow: FlowRun) -> str:
        """Refresh `flow.status`; return done | failed | needs_human | running."""
        if flow.workflow_id is None:
            return "failed"
        try:
            flow.status = str(self.gitlab.workflow_status(flow.workflow_id)["status"]).lower()
        except Exception as exc:
            log.warning("flow status unavailable", extra={"workflow_id": flow.workflow_id, "error": str(exc)})
            return "running"
        return classify_status(flow.status)
