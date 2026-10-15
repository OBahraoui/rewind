"""Load and apply the reversibility policy (rewind/policy.yaml)."""

from __future__ import annotations

import fnmatch
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from rewind.models import Change, Reversibility


class ClassifyRule(BaseModel):
    kind: str = "*"
    op: str | list[str] = "*"
    fields_any: list[str] = Field(default_factory=list)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    cls: Reversibility = Field(alias="class")
    reason: str

    def matches(self, change: Change) -> bool:
        if not fnmatch.fnmatchcase(change.kind, self.kind):
            return False
        ops = [self.op] if isinstance(self.op, str) else self.op
        if "*" not in ops and change.op not in ops:
            return False
        if self.fields_any and not set(self.fields_any) & set(change.fields):
            return False
        for side, expected in (("before", self.before), ("after", self.after)):
            state = getattr(change, side) or {}
            if any(state.get(k) != v for k, v in expected.items()):
                return False
        return True


class GateRule(BaseModel):
    id: str
    paths: list[str]
    verdict: Literal["block", "reroute"]
    reason: str
    instruction: str = ""
    file_must_match: str | None = None
    added_must_not_match: str | None = None
    frozen_functions: list[str] = Field(default_factory=list)


class GatePolicy(BaseModel):
    protected_paths: list[str] = Field(default_factory=list)
    rules: list[GateRule] = Field(default_factory=list)
    max_reroutes: int = 1


class PrivilegePolicy(BaseModel):
    never: list[str] = Field(default_factory=list)
    steps: dict[str, list[str]] = Field(default_factory=dict)


class IgnorePolicy(BaseModel):
    label_prefixes: list[str] = Field(default_factory=lambda: ["rewind:"])
    branch_prefixes: list[str] = Field(default_factory=lambda: ["rewind/"])

    def label(self, name: str) -> bool:
        return any(name.startswith(p) for p in self.label_prefixes)

    def branch(self, name: str) -> bool:
        return any(name.startswith(p) for p in self.branch_prefixes)


class SnapshotPolicy(BaseModel):
    lookback_days: int = 30


class Policy(BaseModel):
    version: int = 1
    ignore: IgnorePolicy = Field(default_factory=IgnorePolicy)
    snapshot: SnapshotPolicy = Field(default_factory=SnapshotPolicy)
    classify: list[ClassifyRule]
    agent_privileges: PrivilegePolicy = Field(default_factory=PrivilegePolicy)
    gate: GatePolicy = Field(default_factory=GatePolicy)

    @field_validator("classify")
    @classmethod
    def _must_fail_closed(cls, rules: list[ClassifyRule]) -> list[ClassifyRule]:
        last = rules[-1] if rules else None
        if not last or last.kind != "*" or last.cls is not Reversibility.IRREVERSIBLE:
            raise ValueError("the last classify rule must be {kind: '*', class: irreversible} (fail closed)")
        return rules

    @classmethod
    def load(cls, path: str | Path | None = None) -> Policy:
        if path:
            text = Path(path).read_text(encoding="utf-8")
        else:
            text = resources.files("rewind").joinpath("policy.yaml").read_text(encoding="utf-8")
        return cls.model_validate(yaml.safe_load(text))

    def classify_change(self, change: Change) -> tuple[Reversibility, str]:
        # A change with no recorded baseline cannot be put back: fail closed.
        if (change.before or {}).get("_unknown_baseline"):
            return Reversibility.IRREVERSIBLE, "Rewind had no record of this object at the checkpoint."
        for rule in self.classify:
            if rule.matches(change):
                return rule.cls, rule.reason
        raise AssertionError("unreachable: policy ends with a catch-all rule")

    def privileges_for(self, step: str) -> list[str]:
        wanted = self.agent_privileges.steps.get(step, [])
        return [p for p in wanted if p not in self.agent_privileges.never]

    def is_protected(self, path: str) -> bool:
        return any(path_matches(path, pat) for pat in self.gate.protected_paths)


def path_matches(path: str, pattern: str) -> bool:
    """Glob where `**` spans directories and `*` stays within one segment."""
    if pattern.endswith("/**"):
        root = pattern[:-3]
        return path == root or path.startswith(root + "/")
    if "**/" in pattern:
        head, _, tail = pattern.partition("**/")
        if not path.startswith(head):
            return False
        rest = path[len(head):]
        parts = rest.split("/")
        return any(fnmatch.fnmatchcase("/".join(parts[i:]), tail) for i in range(len(parts)))
    if "/" not in pattern and "/" in path:
        return False
    return fnmatch.fnmatchcase(path, pattern) and path.count("/") == pattern.count("/")
