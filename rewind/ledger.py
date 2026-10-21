"""Render the run ledger posted (and kept up to date) on the originating issue."""

from __future__ import annotations

from rewind.gate import GateDecision
from rewind.models import ChangeSet, Outcome, Reversibility, RewindReport
from rewind.runs import Run, RunState

LEDGER_MARKER = "<!-- rewind:ledger -->"

STATUS = {
    RunState.QUEUED: "⏳ Queued behind the project's current run",
    RunState.PLANNING: "🧭 Planning",
    RunState.DEVELOPING: "🛠️ Developing",
    RunState.GATING: "🚦 Reversibility gate",
    RunState.REROUTING: "↪️ Rerouting behind a feature flag",
    RunState.TESTING: "🧪 Verifying the merge request",
    RunState.FIXING: "🔧 Fixing the pipeline",
    RunState.REVIEWING: "👀 Code review",
    RunState.MERGING: "🔀 Merging",
    RunState.RELEASING: "🚀 Releasing: staging → production → monitor",
    RunState.REWINDING: "⏪ Rewinding",
    RunState.SHIPPED: "🟢 Shipped to production",
    RunState.REWOUND: "⏪ Rewound — production restored to the checkpoint",
    RunState.BLOCKED: "⛔ Blocked — needs a human",
    RunState.FAILED: "🔴 Failed",
}

CLASS_TEXT = {
    Reversibility.REVERSIBLE: "Reversible — Rewind restores these exactly",
    Reversibility.COMPENSATABLE: "Compensatable — neutralised by a recorded action",
    Reversibility.IRREVERSIBLE: "Irreversible — cannot be undone, so they are gated",
}

OUTCOME_ICON = {
    Outcome.RESTORED: "✅ restored", Outcome.COMPENSATED: "✅ compensated", Outcome.ALREADY: "✅ already",
    Outcome.CONFLICT: "⚠️ conflict", Outcome.FAILED: "❌ failed", Outcome.IRREVERSIBLE: "⛔ irreversible",
    Outcome.PLANNED: "📝 planned",
}

STAGES = ["plan", "create", "verify", "package", "secure", "release", "configure", "monitor", "govern"]


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def render(run: Run, *, change_set: ChangeSet | None = None, report: RewindReport | None = None,
           dashboard_url: str = "") -> str:
    lines = [LEDGER_MARKER, "## ⏪ Rewind ledger", ""]
    lines.append(f"**{STATUS[run.state]}**")
    meta = [f"run `{run.id}`"]
    if run.checkpoint_id:
        meta.append(f"checkpoint `{run.checkpoint_id}`")
    if run.mr_iid:
        meta.append(_link(f"!{run.mr_iid}", run.mr_url))
    if run.incident_iid:
        meta.append(_link(f"incident #{run.incident_iid}", run.incident_url))
    if dashboard_url:
        meta.append(_link("dashboard", f"{dashboard_url}/runs/{run.id}"))
    lines += [" · ".join(meta), ""]
    if run.error and run.state in (RunState.FAILED, RunState.BLOCKED):
        lines += [f"> {run.error}", ""]

    if run.evidence:
        lines += ["### Lifecycle evidence", "", "| Stage | Evidence | Status |", "|---|---|---|"]
        for e in sorted(run.evidence, key=lambda e: (STAGES.index(e.stage) if e.stage in STAGES else 99, e.name)):
            lines.append(f"| {e.stage} | {_link(_esc(e.name), e.url)} | {_esc(e.status)} |")
        lines.append("")

    if run.flows:
        lines += render_sessions(run)

    if run.gate:
        lines += render_gate(run.gate)

    if run.security_findings:
        lines += ["### Security findings introduced by this change", ""]
        lines += [f"- [{f['severity']}] {_esc(f['name'])} at "
                  f"`{f['file']}{':' + str(f['line']) if f.get('line') else ''}`"
                  for f in run.security_findings]
        lines.append("")

    if run.approvals:
        lines += ["### Human approvals", ""]
        lines += [f"- **{_esc(a.by)}** accepted the risk for `{a.sha[:8]}` at {a.at:%Y-%m-%d %H:%M} UTC: "
                  f"{_esc(a.reason)}"
                  for a in run.approvals]
        lines.append("")

    if report is not None:
        lines += render_report(report)

    if change_set is not None:
        body = render_change_set(change_set, run.provenance)
        if report is not None:  # the rewind report comes first; keep the full change set one click away
            lines += ["<details><summary>Change set and who made each change</summary>", "", *body, "</details>", ""]
        else:
            lines += body

    if run.timeline:
        lines += ["<details><summary>Timeline</summary>", ""]
        for ev in run.timeline[-40:]:
            lines.append(f"- `{ev.at.strftime('%H:%M:%S')}` **{ev.state}** — {_esc(ev.message)}")
        lines += ["", "</details>", ""]
    lines.append("<sub>Rewind lets agents do anything it can undo, and stops them only for what it can't.</sub>")
    return "\n".join(lines)


def render_gate(gate: GateDecision) -> list[str]:
    icon = {"allow": "✅ allow", "reroute": "↪️ reroute", "block": "⛔ block"}[gate.verdict]
    out = [f"### Reversibility gate: {icon}", ""]
    if gate.head_sha:
        out.append(f"Checked `{gate.head_sha[:8]}` ({len(gate.files_checked)} files).")
    if not gate.findings:
        out.append("Everything this change ships can be undone by Rewind.")
    for f in gate.findings:
        where = f" `{f.path}{':' + str(f.line) if f.line else ''}`" if f.path else ""
        out.append(f"- **{f.rule_id}**{where} — {f.message}")
        if f.evidence:
            out.append(f"  `{_esc(f.evidence)}`")
    out.append("")
    return out


def render_sessions(run: Run) -> list[str]:
    out = ["### Agent sessions", "", "| Step | Session | Status | Undo just what came after it |", "|---|---|---|---|"]
    for f in run.flows:
        name = f"{f.step} #{f.workflow_id}"
        undo = f"`rewind to {f.checkpoint_id}`" if f.checkpoint_id else ""
        out.append(f"| {f.step} | {_link(name, f.web_url)} | {_esc(f.status)} | {undo} |")
    out.append("")
    return out


def _by(provenance: dict | None, change_id: str) -> str:
    who = (provenance or {}).get(change_id)
    if not who:
        return ""
    return " — _" + ", ".join(_link(label, url) for label, url in who) + "_"


def render_change_set(cs: ChangeSet, provenance: dict | None = None) -> list[str]:
    by = cs.by_class()
    out = [f"### Change set — {len(cs.changes)} change(s) since checkpoint `{cs.checkpoint_id}`", ""]
    if not cs.changes:
        out.append("No changes.")
    for cls in Reversibility:
        items = by[cls]
        if not items:
            continue
        out.append(f"**{CLASS_TEXT[cls]} ({len(items)})**")
        for c in items:
            out.append(f"- {_esc(c.summary)}{_by(provenance, c.id)}")
        out.append("")
    for kind, err in cs.blind_spots.items():
        out.append(f"- ⚠️ blind spot: {kind} — {_esc(err)}")
    out.append("")
    return out


def render_report(r: RewindReport) -> list[str]:
    total = (r.finished_at - r.started_at).total_seconds() if r.finished_at else None
    head = f"### ⏪ Rewind `{r.id}` — {r.reason}"
    out = [head, ""]
    facts = []
    if r.mitigated_after_s is not None:
        facts.append(f"production mitigated in **{r.mitigated_after_s:.1f}s**")
    if total is not None:
        facts.append(f"fully restored in **{total:.1f}s**")
    if r.verification:
        facts.append("verification: **zero drift ✅**" if r.zero_drift
                     else f"verification: **{len(r.verification.drift)} drift item(s) ⚠️**")
    if facts:
        out += [" · ".join(facts), ""]
    out += ["| # | Inverse | Outcome |", "|---|---|---|"]
    for i, s in enumerate(r.steps, 1):
        detail = f" — {_esc(s.detail)}" if s.outcome in (Outcome.FAILED, Outcome.CONFLICT, Outcome.IRREVERSIBLE) else ""
        out.append(f"| {i} | {_esc(s.action)} | {OUTCOME_ICON[s.outcome]}{detail} |")
    if r.verification and r.verification.drift:
        out += ["", "**Drift:**"] + [f"- {_esc(d)}" for d in r.verification.drift]
    if r.pre_rewind_checkpoint_id:
        out += ["", f"This rewind can itself be undone: `rewind undo-last` "
                    f"(pre-rewind checkpoint `{r.pre_rewind_checkpoint_id}`)."]
    out.append("")
    return out
