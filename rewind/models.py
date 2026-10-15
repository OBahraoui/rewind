"""Core data model: snapshots, change sets, rewind plans and reports.

Conventions
-----------
* A snapshot holds, per resource *kind*, a map of stable *key* -> *state*.
* State dicts are plain JSON. Keys starting with ``_`` are informational: they are
  kept for display and for applying inverses, but never compared when diffing.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Time-sortable, human-readable IDs: cp_20261017T141203_a3f9."""
    return f"{prefix}_{utcnow().strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(2)}"


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"  # the exact prior state can be restored
    COMPENSATABLE = "compensatable"  # effect is neutralised by a new, recorded action
    IRREVERSIBLE = "irreversible"  # cannot be undone; must be gated, never "undone"


ChangeOp = Literal["created", "updated", "deleted"]


def comparable(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {k: v for k, v in state.items() if not k.startswith("_")}


class Snapshot(BaseModel):
    id: str
    project_id: str
    taken_at: datetime = Field(default_factory=utcnow)
    reason: str = "checkpoint"
    meta: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    # Kinds whose capture failed; Rewind cannot vouch for them (fail closed).
    errors: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = 0


class Checkpoint(BaseModel):
    id: str
    project_id: str
    snapshot_id: str
    created_at: datetime = Field(default_factory=utcnow)
    kind: Literal["checkpoint", "pre-rewind"] = "checkpoint"
    label: str = ""
    run_id: str | None = None
    issue_iid: int | None = None
    default_branch: str = ""
    default_sha: str = ""
    serving: dict[str, str] = Field(default_factory=dict)  # environment -> revision


class Change(BaseModel):
    id: str
    kind: str
    key: str
    op: ChangeOp
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    fields: list[str] = Field(default_factory=list)
    reversibility: Reversibility = Reversibility.IRREVERSIBLE
    reason: str = ""
    summary: str = ""


class ChangeSet(BaseModel):
    checkpoint_id: str
    from_snapshot: str
    to_snapshot: str
    computed_at: datetime = Field(default_factory=utcnow)
    changes: list[Change] = Field(default_factory=list)
    blind_spots: dict[str, str] = Field(default_factory=dict)

    def by_class(self) -> dict[Reversibility, list[Change]]:
        out: dict[Reversibility, list[Change]] = {r: [] for r in Reversibility}
        for c in self.changes:
            out[c.reversibility].append(c)
        return out

    @property
    def irreversible(self) -> list[Change]:
        return [c for c in self.changes if c.reversibility is Reversibility.IRREVERSIBLE]


class Outcome(StrEnum):
    RESTORED = "restored"  # reversible change put back exactly
    COMPENSATED = "compensated"  # compensating action recorded
    ALREADY = "already"  # live state already matched the target (idempotent no-op)
    CONFLICT = "conflict"  # live state moved since planning; refused to overwrite
    FAILED = "failed"  # the inverse raised
    IRREVERSIBLE = "irreversible"  # declared, not attempted
    PLANNED = "planned"  # dry run


class StepResult(BaseModel):
    change_id: str
    kind: str
    key: str
    action: str
    outcome: Outcome
    detail: str = ""
    duration_ms: int = 0
    finished_at: datetime = Field(default_factory=utcnow)


class Verification(BaseModel):
    ok: bool
    snapshot_id: str
    checked: int = 0
    drift: list[str] = Field(default_factory=list)
    explained: list[str] = Field(default_factory=list)


class RewindReport(BaseModel):
    id: str
    project_id: str
    checkpoint_id: str
    pre_rewind_checkpoint_id: str | None = None
    reason: str = "manual"
    trigger: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    # Seconds from start until production stopped serving the bad change.
    mitigated_after_s: float | None = None
    change_set: ChangeSet | None = None
    steps: list[StepResult] = Field(default_factory=list)
    verification: Verification | None = None
    error: str | None = None

    def count(self, outcome: Outcome) -> int:
        return sum(1 for s in self.steps if s.outcome is outcome)

    @property
    def zero_drift(self) -> bool:
        return bool(
            self.verification
            and self.verification.ok
            and not any(s.outcome in (Outcome.FAILED, Outcome.CONFLICT) for s in self.steps)
        )
