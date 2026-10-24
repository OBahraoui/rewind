"""View models for the dashboard: everything the templates show, computed and testable here.

Templates only lay out what these functions return. Status meaning is never carried by
color alone: every tone comes with an icon and a label.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rewind.models import Outcome, Reversibility, RewindReport, utcnow
from rewind.runs import Run

TERMINAL = {"shipped", "rewound", "blocked", "failed"}


@dataclass
class Badge:
    tone: str  # good | warning | critical | info | neutral | accent
    icon: str  # a name from the icon set in _macros.html
    label: str


OUTCOME_BADGE = {
    "queued": Badge("neutral", "clock", "Queued"),
    "planning": Badge("info", "dot", "Planning"),
    "developing": Badge("info", "dot", "Developing"),
    "gating": Badge("info", "dot", "Gating"),
    "rerouting": Badge("warning", "reroute", "Rerouting"),
    "testing": Badge("info", "dot", "Verifying"),
    "fixing": Badge("warning", "dot", "Fixing"),
    "reviewing": Badge("info", "dot", "In review"),
    "merging": Badge("info", "dot", "Merging"),
    "releasing": Badge("info", "dot", "Releasing"),
    "rewinding": Badge("accent", "rewind", "Rewinding"),
    "shipped": Badge("good", "check", "Shipped"),
    "rewound": Badge("accent", "rewind", "Rewound"),
    "blocked": Badge("critical", "ban", "Blocked"),
    "failed": Badge("critical", "x", "Failed"),
}

CLASS_BADGE = {
    Reversibility.REVERSIBLE: Badge("good", "undo", "Reversible"),
    Reversibility.COMPENSATABLE: Badge("warning", "swap", "Compensatable"),
    Reversibility.IRREVERSIBLE: Badge("critical", "ban", "Irreversible"),
}
CLASS_HELP = {
    Reversibility.REVERSIBLE: "Rewind restores these exactly.",
    Reversibility.COMPENSATABLE: "Neutralised by a new, recorded action; history is kept.",
    Reversibility.IRREVERSIBLE: "Cannot be undone, so the gate stops them.",
}

OUTCOME_TONE = {
    Outcome.RESTORED: Badge("good", "check", "restored"),
    Outcome.COMPENSATED: Badge("good", "check", "compensated"),
    Outcome.ALREADY: Badge("neutral", "equal", "already"),
    Outcome.CONFLICT: Badge("warning", "alert", "conflict"),
    Outcome.FAILED: Badge("critical", "x", "failed"),
    Outcome.IRREVERSIBLE: Badge("critical", "ban", "irreversible"),
    Outcome.PLANNED: Badge("info", "dot", "planned"),
}

GATE_BADGE = {"allow": Badge("good", "check", "Allow"), "reroute": Badge("warning", "reroute", "Reroute"),
              "block": Badge("critical", "ban", "Block")}

EVIDENCE_TONE = {
    "failed": Badge("critical", "x", "failed"), "block": Badge("critical", "ban", "block"),
    "found": Badge("critical", "alert", "found"), "drift": Badge("warning", "alert", "drift"),
    "incomplete": Badge("warning", "alert", "incomplete"), "reroute": Badge("warning", "reroute", "reroute"),
    "canceled": Badge("warning", "alert", "canceled"),
}


def evidence_badge(status: str) -> Badge | None:
    """A tone for evidence that needs attention; passing evidence stays plain text."""
    return EVIDENCE_TONE.get(status.lower())


@dataclass
class StageCell:
    stage: str
    items: list[Any]
    attention: Badge | None  # the most serious evidence in this stage, if any needs attention


def stage_cells(stages: list[str], evidence: list[Any]) -> list[StageCell]:
    """The lifecycle strip: a stage with a failed check reads as failed, not as covered."""
    rank = {"critical": 2, "warning": 1}
    cells = []
    for st in stages:
        items = [e for e in evidence if e.stage == st]
        flagged = [b for b in (evidence_badge(e.status) for e in items) if b]
        cells.append(StageCell(st, items, max(flagged, key=lambda b: rank.get(b.tone, 0), default=None)))
    return cells

# The hands-off loop as stations on a track. A run state maps to the station it is at.
STATIONS = [
    ("checkpoint", "Checkpoint", "Project state captured before agents act"),
    ("plan", "Plan", "Agent posts its plan"),
    ("develop", "Develop", "Agent opens a merge request"),
    ("gate", "Gate", "Can everything it ships be undone?"),
    ("verify", "Verify", "Tests and security scans on the merge request"),
    ("review", "Review", "Duo code review"),
    ("merge", "Merge", "Rewind merges the exact gated commit"),
    ("release", "Release", "Staging, production, flags, monitoring"),
]
STATE_STATION = {
    "queued": "checkpoint", "planning": "plan", "developing": "develop", "gating": "gate",
    "rerouting": "gate", "testing": "verify", "fixing": "verify", "reviewing": "review",
    "merging": "merge", "releasing": "release",
}


@dataclass
class Station:
    key: str
    label: str
    help: str
    # done | current | stopped | pending, and after a rewind: restored (the checkpoint it went back to),
    # undone (happened, then rolled back) and regressed (where production broke)
    status: str = "pending"
    at: datetime | None = None
    note: str = ""


@dataclass
class Track:
    stations: list[Station] = field(default_factory=list)
    outcome: Badge | None = None
    outcome_at: datetime | None = None
    rewound: bool = False
    arc_label: str = ""
    returns_from: int | None = None  # station index the rewind arc starts from (len(stations) = the outcome)

    @property
    def arc_x(self) -> tuple[float, float]:
        """Start and end of the rewind arc, as % of the track width (station centres on a 9-column grid)."""
        cols = len(self.stations) + 1
        start = self.returns_from if self.returns_from is not None else cols - 1
        return (start + .5) / cols * 100, .5 / cols * 100


def _regressed_station(run: Run) -> str | None:
    """The station the run was at when the rewind began, or None if it had already shipped (a manual undo)."""
    events = run.timeline
    first = next((i for i, ev in enumerate(events) if ev.state == "rewinding"), None)
    if not first:
        return None
    for ev in reversed(events[:first]):
        if ev.state == "shipped":
            return None
        if ev.state in STATE_STATION:
            return STATE_STATION[ev.state]
    return None


def run_track(run: Run, report: RewindReport | None = None) -> Track:
    order = [s[0] for s in STATIONS]
    first_seen: dict[str, datetime] = {}
    for ev in run.timeline:
        st = STATE_STATION.get(ev.state)
        if st and st not in first_seen:
            first_seen[st] = ev.at
    reached = [s for s in order if s in first_seen]
    furthest = max((order.index(s) for s in reached), default=0)
    state = run.state.value
    track = Track()
    terminal = state in TERMINAL or state == "rewinding"
    for i, (key, label, help_text) in enumerate(STATIONS):
        st = Station(key, label, help_text, at=first_seen.get(key))
        if i < furthest:
            st.status = "done"
        elif i == furthest:
            if state in ("blocked", "failed"):
                st.status = "stopped"
            elif terminal:
                st.status = "done"
            else:
                st.status = "current"
        if key == "checkpoint" and run.checkpoint_id and st.status == "current" and state != "queued":
            st.status = "done"
        track.stations.append(st)
    if state == "queued" and not run.checkpoint_id:
        track.stations[0].status = "current"
    if terminal:
        track.outcome = OUTCOME_BADGE[state]
        track.outcome_at = run.updated_at
    if state in ("rewound", "rewinding"):
        _mark_rewind(track, run, order)
    if state == "rewound":
        track.rewound = True
        if report and report.finished_at:
            total = (report.finished_at - report.started_at).total_seconds()
            drift = "zero drift" if report.zero_drift else "drift found"
            track.arc_label = f"Back to the checkpoint in {total:.1f}s · {drift}"
        else:
            track.arc_label = "Back to the checkpoint"
    return track


def _mark_rewind(track: Track, run: Run, order: list[str]) -> None:
    """Show where production broke and what the rewind took back, so a rewound run never reads as shipped."""
    regressed = _regressed_station(run)
    stop = order.index(regressed) if regressed else len(order)
    for i, st in enumerate(track.stations):
        if i == 0:
            st.status, st.note = "restored", "restored here"
        elif i < stop:
            st.status = "undone"
        elif i == stop:
            st.status, st.note = "regressed", "production regressed"
        else:
            st.status = "pending"
    track.returns_from = stop


@dataclass
class Segment:
    label: str
    detail: str
    start_pct: float
    width_pct: float
    seconds: float
    tone: str
    icon: str


@dataclass
class Timeline:
    total_s: float
    segments: list[Segment]
    mitigated_pct: float | None
    mitigated_s: float | None
    ticks: list[tuple[float, str]]


def rewind_timeline(report: RewindReport) -> Timeline | None:
    """Phases of a rewind on one time axis: checkpoint, each inverse, then verification."""
    if not report.finished_at or not report.steps:
        return None
    t0 = report.started_at
    total = max((report.finished_at - t0).total_seconds(), 0.001)

    def pct(seconds: float) -> float:
        return max(0.0, min(100.0, seconds / total * 100))

    segments: list[Segment] = []
    first_start = None
    last_end = 0.0
    for s in report.steps:
        end = (s.finished_at - t0).total_seconds()
        start = max(0.0, end - s.duration_ms / 1000)
        first_start = start if first_start is None else min(first_start, start)
        last_end = max(last_end, end)
        tone = OUTCOME_TONE[s.outcome]
        segments.append(Segment(s.action, f"{tone.label} · {s.duration_ms / 1000:.1f}s", pct(start),
                                max(pct(end) - pct(start), 0.6), s.duration_ms / 1000, tone.tone, tone.icon))
    if first_start and first_start > 0.05:
        segments.insert(0, Segment("Pre-rewind checkpoint", f"snapshot so this rewind can be undone · "
                                   f"{first_start:.1f}s", 0.0, pct(first_start), first_start, "neutral", "clock"))
    if total - last_end > 0.05:
        segments.append(Segment("Verification", f"re-snapshot and compare with the checkpoint · "
                                f"{total - last_end:.1f}s", pct(last_end), pct(total) - pct(last_end),
                                total - last_end, "neutral", "target"))
    step = _nice_step(total)
    ticks = [(pct(t), f"{t:g}s") for t in _frange(0, total, step)]
    mit = report.mitigated_after_s
    return Timeline(total, segments, pct(mit) if mit is not None else None, mit, ticks)


def _nice_step(total: float) -> float:
    for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
        if total / step <= 6:
            return step
    return 600


def _frange(a: float, b: float, step: float):
    x = a
    while x <= b + 1e-9:
        yield round(x, 3)
        x += step


@dataclass
class Overview:
    runs: int
    shipped: int
    rewound: int
    blocked: int
    failed: int
    active: list[dict[str, Any]]
    zero_drift: int
    rewinds: int
    fastest_mitigation_s: float | None
    median_restore_s: float | None
    flows_today: int
    flow_limit: int

    @property
    def flow_pct(self) -> float:
        return min(100.0, self.flows_today / self.flow_limit * 100) if self.flow_limit else 0.0


def overview(summaries: list[dict[str, Any]], flows_today: int, flow_limit: int) -> Overview:
    rewinds = [s["rewind"] for s in summaries if s.get("rewind")]
    mitigations = [r["mitigated_after_s"] for r in rewinds if r.get("mitigated_after_s") is not None]
    restores = [r["total_s"] for r in rewinds if r.get("total_s") is not None]
    return Overview(
        runs=len(summaries),
        shipped=sum(s["state"] == "shipped" for s in summaries),
        rewound=sum(s["state"] == "rewound" for s in summaries),
        blocked=sum(s["state"] == "blocked" for s in summaries),
        failed=sum(s["state"] == "failed" for s in summaries),
        active=[s for s in summaries if s["state"] not in TERMINAL],
        zero_drift=sum(1 for r in rewinds if r.get("zero_drift")),
        rewinds=len(rewinds),
        fastest_mitigation_s=min(mitigations) if mitigations else None,
        median_restore_s=statistics.median(restores) if restores else None,
        flows_today=flows_today,
        flow_limit=flow_limit,
    )


def rewind_summary(report: RewindReport) -> dict[str, Any]:
    total = (report.finished_at - report.started_at).total_seconds() if report.finished_at else None
    return {"id": report.id, "mitigated_after_s": report.mitigated_after_s,
            "total_s": round(total, 2) if total is not None else None, "zero_drift": report.zero_drift,
            "steps": len(report.steps)}


# -- formatting filters --------------------------------------------------------
def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def ago(value: Any) -> str:
    dt = _parse(value)
    if not dt:
        return "—"
    s = (utcnow() - dt).total_seconds()
    if s < 45:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)} min ago"
    if s < 86400:
        return f"{round(s / 3600)} h ago"
    return dt.strftime("%d %b")


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
    m, s = divmod(round(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def run_duration(summary: dict[str, Any]) -> float | None:
    start, end = _parse(summary.get("created_at")), _parse(summary.get("updated_at"))
    if not start:
        return None
    if summary.get("state") not in TERMINAL:
        end = utcnow()
    return (end - start).total_seconds() if end else None


def conds(value: dict[str, Any] | None) -> str:
    """{'state': 'merged', 'protected': True} -> 'state = merged, protected = true'."""
    def fmt(v: Any) -> str:
        return str(v).lower() if isinstance(v, bool) else str(v)
    return ", ".join(f"{k} = {fmt(v)}" for k, v in (value or {}).items())


def iso(value: Any) -> str:
    dt = _parse(value)
    return dt.isoformat() if dt else ""
