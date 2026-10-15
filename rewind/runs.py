"""An autonomous run: one labelled issue taken from checkpoint to production (or rewound)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from rewind.gate import GateDecision
from rewind.models import new_id, utcnow


class RunState(StrEnum):
    QUEUED = "queued"  # waiting for the project's previous run to finish (runs are serialized)
    PLANNING = "planning"
    DEVELOPING = "developing"
    GATING = "gating"
    REROUTING = "rerouting"
    TESTING = "testing"  # MR pipeline running
    FIXING = "fixing"  # fix-pipeline flow running
    REVIEWING = "reviewing"
    MERGING = "merging"
    RELEASING = "releasing"  # default-branch pipeline: package, deploy, configure, monitor
    REWINDING = "rewinding"
    SHIPPED = "shipped"
    REWOUND = "rewound"
    BLOCKED = "blocked"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {RunState.SHIPPED, RunState.REWOUND, RunState.BLOCKED, RunState.FAILED}


class FlowRun(BaseModel):
    step: str
    ref: str
    workflow_id: int | None = None
    status: str = "created"
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = None


class Evidence(BaseModel):
    stage: str  # DevSecOps lifecycle stage this proves
    name: str
    status: str
    url: str = ""


class TimelineEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    state: str
    message: str


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    project_id: str
    issue_iid: int
    issue_title: str = ""
    issue_url: str = ""
    state: RunState = RunState.QUEUED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    state_since: datetime = Field(default_factory=utcnow)

    checkpoint_id: str | None = None
    mr_iid: int | None = None
    mr_url: str = ""
    gated_sha: str = ""  # the exact MR head the gate approved; merge is refused if it moves
    merge_sha: str = ""
    release_pipeline_id: int | None = None
    release_pipeline_url: str = ""

    flows: list[FlowRun] = Field(default_factory=list)
    gate: GateDecision | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    reroutes: int = 0
    fix_attempts: int = 0
    rewind_id: str | None = None
    ledger_note_id: int | None = None
    change_counts: dict[str, int] = Field(default_factory=dict)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def move(self, state: RunState, message: str) -> None:
        self.state = state
        self.state_since = self.updated_at = utcnow()
        self.timeline.append(TimelineEvent(state=state.value, message=message))

    def note(self, message: str) -> None:
        self.updated_at = utcnow()
        self.timeline.append(TimelineEvent(state=self.state.value, message=message))

    def current_flow(self, step: str) -> FlowRun | None:
        for f in reversed(self.flows):
            if f.step == step:
                return f
        return None

    def add_evidence(self, stage: str, name: str, status: str, url: str = "") -> None:
        self.evidence = [e for e in self.evidence if not (e.stage == stage and e.name == name)]
        self.evidence.append(Evidence(stage=stage, name=name, status=status, url=url))
