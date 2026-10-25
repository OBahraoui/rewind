"""The dashboard and storefront: valid HTML in every state, accessible, no surprise dependencies."""

from __future__ import annotations

import re
import tempfile
from datetime import timedelta

import html5lib
import pytest
from fastapi.testclient import TestClient

from rewind.models import Outcome, RewindReport, StepResult, Verification, utcnow
from rewind.runs import Run, RunState, TimelineEvent
from scripts import simulate
from service import views

ALLOWED_EXTERNAL = ("https://fonts.googleapis.com", "https://fonts.gstatic.com")


@pytest.fixture(scope="module")
def world():
    tmp = tempfile.mkdtemp()
    app, gl, cr = simulate.build(tmp)
    bulk = "n = sum(q for _, q in lines)\n    s = subtotal(lines)\n    return s - s // 10 if n >= 10 else s"
    simulate.run_scenario(app, gl, cr, "Bulk discount", bulk, monitor_ok=True)
    simulate.run_scenario(app, gl, cr, "Launch promo", "return subtotal(lines) - 1500", monitor_ok=False)
    simulate.run_scenario(app, gl, cr, "Receipts", "return subtotal(lines)", monitor_ok=True,
                          extra={"app/receipts.py": "import httpx\n"})
    simulate.in_progress(app, gl, "Seasonal pricing")
    with TestClient(app) as client:
        yield client


def strict_parse(html: str) -> None:
    parser = html5lib.HTMLParser(strict=True)
    parser.parse(html)  # raises on any HTML5 parse error


def pages(client) -> dict[str, str]:
    runs = client.get("/api/runs").json()
    out = {"overview": "/", "policy": "/policy"}
    for r in runs:
        out[f"run-{r['state']}"] = f"/runs/{r['id']}"
    return out


def test_every_page_renders_valid_html(world):
    found = pages(world)
    assert {"run-shipped", "run-rewound", "run-blocked", "run-developing"} <= set(found)
    for name, path in found.items():
        r = world.get(path, headers={"accept": "text/html"})
        assert r.status_code == 200, name
        strict_parse(r.text)
        assert '<html lang="en">' in r.text and "<title>" in r.text and 'name="viewport"' in r.text, name


def test_no_unexpected_external_resources(world):
    for name, path in pages(world).items():
        html = world.get(path).text
        for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', html):
            if url.startswith(("https://gitlab.example", "http://127.0.0.1")):
                continue  # links to the project, not resources
            assert url.startswith(ALLOWED_EXTERNAL), (name, url)
        assert not re.search(r'<script[^>]+src="https?://', html), name  # scripts are all first-party


def test_interactive_elements_have_accessible_names(world):
    for name, path in pages(world).items():
        html = world.get(path).text
        for button in re.findall(r"<button[^>]*>", html):
            assert "aria-label=" in button, (name, button)
        for img in re.findall(r"<img[^>]*>", html):
            assert "alt=" in img, (name, img)


def test_live_updates_only_where_something_can_change(world):
    found = pages(world)
    assert "data-live" in world.get(found["run-developing"]).text
    for done in ("run-shipped", "run-rewound", "run-blocked"):
        assert "data-live" not in world.get(found[done]).text, done
    assert "data-live" in world.get("/").text  # a run is in progress


def test_version_endpoints_track_changes(world):
    runs = world.get("/api/runs").json()
    v1 = world.get("/api/version").json()["version"]
    r = world.get(f"/api/runs/{runs[0]['id']}/version").json()
    assert r["version"] and r["state"] == runs[0]["state"]
    assert world.get("/api/version").json()["version"] == v1
    assert world.get("/api/runs/nope/version").status_code == 404


def test_html_404_and_json_404(world):
    page = world.get("/runs/nope", headers={"accept": "text/html"})
    assert page.status_code == 404 and "Nothing here" in page.text and "<code>nope</code>" in page.text
    strict_parse(page.text)
    assert world.get("/api/rewinds/nope").json() == {"detail": "rewind not found"}


def test_rewound_page_shows_the_arc_timeline_and_zero_drift(world):
    html = world.get(pages(world)["run-rewound"]).text
    assert 'class="track has-arc rolled"' in html and "Back to the checkpoint in" in html
    assert 'class="tl"' in html and "production mitigated" in html and "Zero drift" in html
    # where production broke is on the track, and what came after the checkpoint reads as rolled back
    assert 'class="station regressed"' in html and "production regressed" in html
    assert 'class="station undone"' in html and 'class="station restored"' in html
    assert "Undo this rewind" in html and "rewind to pre_" in html


def test_shipped_page_says_how_to_undo(world):
    html = world.get(pages(world)["run-shipped"]).text
    assert "Undo this run" in html and 'data-copy="/rewind"' in html
    assert "Undo only part of this run" in html and "rewind to step_" in html


def test_live_page_says_how_to_stop(world):
    html = world.get(pages(world)["run-developing"]).text
    assert "Stop this run" in html and 'data-copy="/rewind stop"' in html and 'data-announce=' in html


def test_lifecycle_strip_shows_the_failed_monitor_on_a_rewound_run(world):
    html = world.get(pages(world)["run-rewound"]).text
    assert 'class="stage tone-critical"' in html


def test_no_unicode_status_glyphs_in_pages(world):
    for name, path in pages(world).items():
        html = world.get(path).text
        assert not re.search("[⟲⊘↺⇄◷◎↪]", html), name  # icons are drawn, not borrowed from a font


def test_blocked_page_explains_the_finding(world):
    html = world.get(pages(world)["run-blocked"]).text
    assert "external-side-effect" in html and "app/receipts.py" in html
    assert 'class="station stopped"' in html
    # reassurance, a way forward, and no "irreversible: none" under an irreversible block
    assert "production is untouched" in html and "What happens next" in html and "rewind approve run_" in html
    assert "Stopped before it happened" in html and "How to ship it safely" in html


def test_static_assets_are_cache_busted(world):
    html = world.get("/").text
    v = re.search(r'/static/style\.css\?v=([0-9a-f]{10})', html).group(1)
    assert f"/static/app.js?v={v}" in html
    assert world.get("/static/style.css").status_code == 200
    assert world.get("/static/app.js").status_code == 200
    assert f'href="/static/icon.svg?v={v}"' in html and world.get("/static/icon.svg").status_code == 200


def test_storefront_is_valid_and_self_contained():
    from app.storefront import PAGE

    strict_parse(PAGE)
    assert not re.search(r'(?:src|href)="https?://', PAGE)  # no external resources at all
    assert "orders/quote" in PAGE and "below cost" in PAGE
    assert "Guardrail: no order is ever charged less than half its subtotal." in PAGE
    assert 'id="announce" aria-live' in PAGE and 'class="ribbon" role="status"' not in PAGE  # announce changes only


# -- view models ---------------------------------------------------------------
def make_run(states: list[str], final: str, **kw) -> Run:
    t = utcnow()
    run = Run(project_id="1", issue_iid=1, issue_title="x", checkpoint_id="cp_1", **kw)
    run.timeline = [TimelineEvent(at=t + timedelta(seconds=i), state=s, message=s) for i, s in enumerate(states)]
    run.state = RunState(final)
    return run


def statuses(track) -> list[str]:
    return [s.status for s in track.stations]


def test_track_in_progress():
    t = views.run_track(make_run(["queued", "planning", "developing"], "developing"))
    assert statuses(t) == ["done", "done", "current", "pending", "pending", "pending", "pending", "pending"]
    assert t.outcome is None and not t.rewound


def test_track_blocked_stops_at_the_gate():
    t = views.run_track(make_run(["queued", "planning", "developing", "gating", "blocked"], "blocked"))
    assert statuses(t)[:4] == ["done", "done", "done", "stopped"] and statuses(t)[4:] == ["pending"] * 4
    assert t.outcome.label == "Blocked" and t.outcome.tone == "critical"


def test_track_rewound_draws_the_arc():
    states = ["queued", "planning", "developing", "gating", "testing", "reviewing", "merging", "releasing",
              "rewinding", "rewound"]
    t0 = utcnow()
    report = RewindReport(id="rw", project_id="1", checkpoint_id="cp_1", started_at=t0,
                          finished_at=t0 + timedelta(seconds=7.4), verification=Verification(ok=True, snapshot_id="s"))
    t = views.run_track(make_run(states, "rewound"), report)
    assert statuses(t) == ["restored"] + ["undone"] * 6 + ["regressed"] and t.rewound
    assert t.returns_from == 7 and t.stations[7].note == "production regressed"
    assert t.arc_label == "Back to the checkpoint in 7.4s · zero drift"
    assert t.arc_x == pytest.approx((7.5 / 9 * 100, .5 / 9 * 100))


def test_track_manual_undo_after_ship_rolls_back_everything():
    states = ["queued", "planning", "developing", "gating", "testing", "reviewing", "merging", "releasing",
              "shipped", "rewinding", "rewound"]
    t = views.run_track(make_run(states, "rewound"))
    assert statuses(t) == ["restored"] + ["undone"] * 7 and t.returns_from == 8  # the arc starts at the outcome


def test_stage_cells_flag_failed_evidence():
    from rewind.runs import Evidence
    ev = [Evidence(stage="monitor", name="monitor:production", status="failed"),
          Evidence(stage="monitor", name="Rewind rw", status="zero drift"),
          Evidence(stage="secure", name="scan", status="incomplete"), Evidence(stage="plan", name="p", status="done")]
    cells = {c.stage: c for c in views.stage_cells(["plan", "secure", "monitor", "govern"], ev)}
    assert cells["monitor"].attention.tone == "critical" and cells["secure"].attention.tone == "warning"
    assert cells["plan"].attention is None and len(cells["plan"].items) == 1 and not cells["govern"].items


def test_queued_without_checkpoint_is_current():
    run = make_run(["queued"], "queued")
    run.checkpoint_id = None
    assert statuses(views.run_track(run))[0] == "current"


def test_timeline_geometry():
    t0 = utcnow()
    steps = [StepResult(change_id="a", kind="cloud_run", key="production", action="route traffic",
                        outcome=Outcome.RESTORED, duration_ms=2000, finished_at=t0 + timedelta(seconds=3)),
             StepResult(change_id="b", kind="repository", key="default", action="restore code",
                        outcome=Outcome.FAILED, duration_ms=2000, finished_at=t0 + timedelta(seconds=5))]
    report = RewindReport(id="rw", project_id="1", checkpoint_id="cp", started_at=t0,
                          finished_at=t0 + timedelta(seconds=8), steps=steps, mitigated_after_s=3.0)
    tl = views.rewind_timeline(report)
    labels = [s.label for s in tl.segments]
    assert labels == ["Pre-rewind checkpoint", "route traffic", "restore code", "Verification"]
    assert [s.tone for s in tl.segments] == ["neutral", "good", "critical", "neutral"]
    assert tl.segments[1].start_pct == pytest.approx(12.5) and tl.mitigated_pct == pytest.approx(37.5)
    assert all(0 <= s.start_pct <= 100 and s.start_pct + s.width_pct <= 100.01 for s in tl.segments)
    assert tl.ticks[0] == (0.0, "0s") and tl.ticks[-1][0] <= 100


def test_overview_numbers():
    rows = [{"state": "rewound", "rewind": {"mitigated_after_s": 6.4, "total_s": 26.0, "zero_drift": True}},
            {"state": "rewound", "rewind": {"mitigated_after_s": None, "total_s": 20.0, "zero_drift": True}},
            {"state": "shipped"}, {"state": "developing"}]
    ov = views.overview(rows, flows_today=20, flow_limit=25)
    assert (ov.runs, ov.rewound, ov.shipped, len(ov.active)) == (4, 2, 1, 1)
    assert ov.fastest_mitigation_s == 6.4 and ov.median_restore_s == 23.0 and ov.zero_drift == 2
    assert ov.flow_pct == 80.0


def test_formatters():
    assert views.duration(4.56) == "4.6s" and views.duration(26) == "26s" and views.duration(125) == "2m 05s"
    assert views.duration(None) == "—"
    assert views.conds({"state": "merged", "protected": True}) == "state = merged, protected = true"
    assert views.ago(utcnow()) == "just now"


def test_track_undo_of_a_failed_run_rolls_back_only_what_happened():
    states = ["queued", "planning", "developing", "gating", "testing", "reviewing", "merging", "failed",
              "rewinding", "rewound"]
    t = views.run_track(make_run(states, "rewound"))
    assert statuses(t) == ["restored"] + ["undone"] * 6 + ["pending"]  # release was never reached
    assert t.returns_from == 8 and not any(s.note == "production regressed" for s in t.stations)


def test_an_undone_blocked_run_shows_its_failure_as_history_not_an_alert():
    tmp = tempfile.mkdtemp()
    app, gl, cr = simulate.build(tmp)
    run = simulate.run_scenario(app, gl, cr, "Receipts", "return subtotal(lines)", monitor_ok=True,
                                extra={"app/receipts.py": "import httpx\n"})
    assert run.state is RunState.BLOCKED
    gl.members[42] = 30
    reply = app.state.orchestrator.command(text="/rewind", author_id=42, author="dev", issue_iid=run.issue_iid)
    assert "Rewound run" in reply
    with TestClient(app) as client:
        html = client.get(f"/runs/{run.id}").text
    strict_parse(html)
    assert 'class="alert tone-critical"' not in html and "stays open and unmerged" not in html
    assert "Before the rewind, the run had stopped: Reversibility gate blocked" in html
    assert 'class="station regressed"' not in html  # an undo someone asked for: nothing "regressed"


def test_run_pages_with_human_approvals_render():
    """Both kinds of approval: accepting what Rewind can't undo, and accepting a code review's concerns."""
    from rewind.runs import Approval

    tmp = tempfile.mkdtemp()
    app, gl, cr = simulate.build(tmp)
    run = simulate.run_scenario(app, gl, cr, "Receipts", "return subtotal(lines)", monitor_ok=True,
                                extra={"app/receipts.py": "import httpx\n"})
    orch = app.state.orchestrator
    run = orch.approve(run.id, by="Othmane", reason="receipts are worth it")
    run.approvals.append(Approval(by="Othmane", reason="finance confirmed", sha="ee912981", covers=["review|ee912981"]))
    orch.repo.save(run)
    with TestClient(app) as client:
        r = client.get(f"/runs/{run.id}")
    assert r.status_code == 200, r.text[:300]
    strict_parse(r.text)
    assert "accepted the risk for" in r.text and "accepted the code review's concerns for" in r.text
