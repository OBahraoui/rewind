"""The reversibility gate: decide, before merge, whether an agent's MR may ship hands-off.

The gate sees two things:
  * the change set since the checkpoint (GitLab state the agent touched), and
  * the code diff of the MR (what will ship).

Verdicts:
  allow    everything that would ship can be undone by Rewind
  reroute  undoable if done differently (e.g. behind a feature flag); the agent is told how
  block    something cannot be undone; a human decides
"""

from __future__ import annotations

import ast
import re
from typing import Literal

from pydantic import BaseModel, Field

from rewind.models import ChangeSet, utcnow
from rewind.policy import GateRule, path_matches
from rewind.resources import Context

Verdict = Literal["allow", "reroute", "block"]


class Finding(BaseModel):
    rule_id: str
    verdict: Literal["reroute", "block"]
    message: str
    path: str = ""
    line: int | None = None
    evidence: str = ""
    instruction: str = ""


class GateDecision(BaseModel):
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    files_checked: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    evaluated_at: str = Field(default_factory=lambda: utcnow().isoformat())

    @property
    def instructions(self) -> list[str]:
        return sorted({f.instruction for f in self.findings if f.instruction})


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> list[tuple[int, str]]:
    """(new line number, text) for every added line of a unified diff."""
    out, line_no = [], 0
    for raw in diff_text.splitlines():
        if m := _HUNK.match(raw):
            line_no = int(m.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((line_no, raw[1:]))
            line_no += 1
        elif raw.startswith(" ") or raw == "":
            line_no += 1
    return out


def function_sources(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    return {n.name: ast.dump(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _text(ctx: Context, path: str, ref: str) -> str | None:
    raw = ctx.gitlab.get_file_raw(path, ref)
    return raw.decode("utf-8", errors="replace") if raw is not None else None


def _check_rule(ctx: Context, rule: GateRule, d: dict, base: str, head: str) -> list[Finding]:
    path = d["new_path"]
    findings: list[Finding] = []

    def finding(message: str, line: int | None = None, evidence: str = "") -> Finding:
        return Finding(rule_id=rule.id, verdict=rule.verdict, message=message, path=path, line=line,
                       evidence=evidence.strip()[:200], instruction=rule.instruction)

    if rule.file_must_match and not d.get("deleted_file"):
        content = _text(ctx, path, head) or ""
        if not re.search(rule.file_must_match, content):
            findings.append(finding(rule.reason))
    if rule.added_must_not_match:
        pattern = re.compile(rule.added_must_not_match)
        for line_no, text in added_lines(d.get("diff", "")):
            if pattern.search(text):
                findings.append(finding(rule.reason, line_no, text))
                break
    if rule.frozen_functions:
        before = function_sources(_text(ctx, d["old_path"], base) or "") if not d.get("new_file") else {}
        after = function_sources(_text(ctx, path, head) or "") if not d.get("deleted_file") else {}
        for fn in rule.frozen_functions:
            if before.get(fn) != after.get(fn):
                findings.append(finding(f"{rule.reason} `{fn}()` is the live default path and changed."))
    return findings


def evaluate(ctx: Context, change_set: ChangeSet | None, base_sha: str, head_sha: str) -> GateDecision:
    policy = ctx.policy
    findings: list[Finding] = []

    if change_set is not None:
        for kind, err in change_set.blind_spots.items():
            findings.append(Finding(rule_id="blind-spot", verdict="block",
                                    message=f"Rewind could not capture {kind} ({err}), so it cannot vouch "
                                            f"that this run is undoable."))
        for c in change_set.irreversible:
            findings.append(Finding(rule_id="irreversible-change", verdict="block",
                                    message=f"{c.summary}: {c.reason}"))

    cmp = ctx.gitlab.compare(base_sha, head_sha)
    files: list[str] = []
    for d in cmp.get("diffs", []):
        paths = {d["old_path"], d["new_path"]}
        files.append(d["new_path"])
        for p in sorted(paths):
            if policy.is_protected(p):
                findings.append(Finding(
                    rule_id="protected-path", verdict="block", path=p,
                    message="Agents may not change their own guardrails or the delivery pipeline."))
        for rule in policy.gate.rules:
            if any(path_matches(d["new_path"], pat) or path_matches(d["old_path"], pat) for pat in rule.paths):
                findings.extend(_check_rule(ctx, rule, d, base_sha, head_sha))

    verdict: Verdict = "allow"
    if any(f.verdict == "reroute" for f in findings):
        verdict = "reroute"
    if any(f.verdict == "block" for f in findings):
        verdict = "block"
    counts = {}
    if change_set is not None:
        counts = {r.value: len(v) for r, v in change_set.by_class().items()}
    return GateDecision(verdict=verdict, findings=findings, base_sha=base_sha, head_sha=head_sha,
                        files_checked=files, counts=counts)
