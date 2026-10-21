"""Engine behaviour: every inverse round-trips, rewinds are idempotent and conflict-safe."""

from __future__ import annotations

import pytest

from rewind.models import Outcome, Reversibility
from rewind.store import LockHeld


def agent_ships_bad_release(gl, cr, issue_iid: int) -> int:
    """What an autonomous run does end to end: MR, merge, flag on, tag+release, deploy."""
    gl.commit_files("feature/promo", {"app/pricing.py": "def legacy_total(items):\n    return sum(items) - 15\n",
                                      "config/flags.yaml": "new_pricing:\n  staging: true\n  production: true\n"})
    mr = gl.create_merge_request("feature/promo", "main", "Launch promo pricing",
                                 description=f"Closes #{issue_iid}")
    gl.update_merge_request(mr["iid"], add_labels="pricing")
    gl.merge_merge_request(mr["iid"])
    gl.update_feature_flag("new_pricing", {"active": True})
    gl.create_tag("v1.1.0", "main")
    gl.create_release("v1.1.0", "v1.1.0", "Promo pricing")
    cr.deploy("staging")
    cr.deploy("production")
    return mr["iid"]


def test_full_agent_run_is_rewound_with_zero_drift(engine, gl, cr):
    issue = gl.add_issue("Launch promo pricing", labels=["rewind:auto"])
    main_before = gl.tree("main")
    prod_before = cr.serving("production")["serving"]
    cp = engine.checkpoint("before run", issue_iid=issue)

    mr_iid = agent_ships_bad_release(gl, cr, issue)
    assert gl.issues[issue]["state"] == "closed"  # "Closes #n" closed it

    report = engine.rewind(cp.id, reason="monitor failed")

    assert report.zero_drift, report.verification
    assert gl.tree("main") == main_before
    assert cr.serving("production")["serving"] == prod_before
    assert gl.flags["new_pricing"]["active"] is False
    assert "v1.1.0" not in gl.tags and "v1.1.0" not in gl.releases
    assert gl.issues[issue]["state"] == "opened"
    assert gl.mrs[mr_iid]["state"] == "merged"  # merges can't be undone...
    assert any("Rewound" in n["body"] for n in gl.mr_notes[mr_iid])  # ...so the MR is annotated
    assert report.mitigated_after_s is not None
    outcomes = {s.kind: s.outcome for s in report.steps}
    assert outcomes["cloud_run"] is Outcome.RESTORED
    assert outcomes["repository"] is Outcome.COMPENSATED
    # Traffic moves before anything else.
    assert report.steps[0].kind == "cloud_run"
    # The rollback shows up on the GitLab environment page.
    assert {d["environment"] for d in gl.deployments} == {"staging", "production"}


def test_rewind_twice_is_a_noop(engine, gl, cr):
    issue = gl.add_issue("x")
    cp = engine.checkpoint()
    agent_ships_bad_release(gl, cr, issue)
    engine.rewind(cp.id)
    calls_before = len(gl.calls)
    commits_before = len(gl.commits)

    second = engine.rewind(cp.id)

    assert second.zero_drift
    writes = [c for c in gl.calls[calls_before:] if c[0] not in {"create_branch"}]
    assert writes == [], writes
    assert len(gl.commits) == commits_before
    assert all(s.outcome in (Outcome.ALREADY, Outcome.IRREVERSIBLE) for s in second.steps)


def test_dry_run_changes_nothing(engine, gl, cr):
    cp = engine.checkpoint()
    agent_ships_bad_release(gl, cr, gl.add_issue("x"))
    calls = len(gl.calls)
    plan = engine.plan(cp.id)
    assert len(gl.calls) == calls
    assert all(s.outcome in (Outcome.PLANNED, Outcome.IRREVERSIBLE) for s in plan.steps)
    assert plan.steps[0].action.startswith("route production traffic") or plan.steps[0].kind == "cloud_run"


def test_conflict_is_reported_not_overwritten(engine, gl, cr):
    cp = engine.checkpoint()
    gl.update_feature_flag("new_pricing", {"active": True})
    changes = engine.diff(cp.id).changes
    # A human flips the flag again between plan and apply.
    flag_change = next(c for c in changes if c.kind == "feature_flag")
    gl.update_feature_flag("new_pricing", {"description": "edited by a human"})
    step = engine._apply(engine.ctx, flag_change)
    assert step.outcome is Outcome.CONFLICT
    assert gl.flags["new_pricing"]["description"] == "edited by a human"


def test_irreversible_changes_are_declared_never_undone(engine, gl):
    cp = engine.checkpoint()
    gl.update_variable("API_URL", "*", value="https://evil.example")  # value change: no way back
    report = engine.rewind(cp.id)
    step = next(s for s in report.steps if s.kind == "ci_variable")
    assert step.outcome is Outcome.IRREVERSIBLE
    assert gl.variables[("API_URL", "*")]["value"] == "https://evil.example"
    assert report.verification.ok  # declared, therefore explained


def test_variable_values_never_stored(engine, gl, tmp_path):
    gl.set_variable("DB_PASSWORD", "hunter2-super-secret")
    engine.checkpoint()
    blob = "".join(p.read_text() for p in (tmp_path / "store").rglob("*.json"))
    assert "hunter2-super-secret" not in blob
    assert "DB_PASSWORD" in blob


def test_undo_last_redoes_the_rewound_state(engine, gl, cr):
    cp = engine.checkpoint()
    agent_ships_bad_release(gl, cr, gl.add_issue("x"))
    after_tree = gl.tree("main")
    bad_rev = cr.serving("production")["serving"]
    engine.rewind(cp.id)
    undo = engine.undo_last()
    assert undo.zero_drift, undo.verification
    assert gl.tree("main") == after_tree
    assert cr.serving("production")["serving"] == bad_rev
    assert gl.flags["new_pricing"]["active"] is True


def test_failed_step_does_not_stop_the_rest(engine, gl, cr):
    from rewind.http import ApiError

    cp = engine.checkpoint()
    agent_ships_bad_release(gl, cr, gl.add_issue("x"))
    gl.fail_next["delete_release"] = ApiError("DELETE", "releases", 500, "boom")
    report = engine.rewind(cp.id)
    by_kind = {s.kind: s.outcome for s in report.steps}
    assert by_kind["release"] is Outcome.FAILED
    assert by_kind["cloud_run"] is Outcome.RESTORED
    assert by_kind["repository"] is Outcome.COMPENSATED
    assert not report.zero_drift
    # Re-running finishes the job.
    again = engine.rewind(cp.id)
    assert again.zero_drift, again.verification


def test_only_one_rewind_per_project(engine):
    cp = engine.checkpoint()
    with engine.store.lock("rewind-42"), pytest.raises(LockHeld):
        engine.rewind(cp.id)


def test_snapshot_failure_is_a_blind_spot(engine, gl):
    cp = engine.checkpoint()
    gl.fail_next["list_feature_flags"] = RuntimeError("API down")
    cs = engine.diff(cp.id)
    assert "feature_flag" in cs.blind_spots


def test_unknown_baseline_fails_closed(engine, gl):
    old = gl.add_issue("ancient", created_at="2001-01-01T00:00:00+00:00")
    gl.issues[old].update(state="closed", updated_at="2001-01-02T00:00:00+00:00")
    cp = engine.checkpoint()
    gl.update_issue(old, state_event="reopen")
    change = next(c for c in engine.diff(cp.id).changes if c.kind == "issue")
    assert change.reversibility is Reversibility.IRREVERSIBLE


def test_rewind_labels_are_ignored(engine, gl):
    issue = gl.add_issue("x")
    cp = engine.checkpoint()
    gl.update_issue(issue, add_labels="rewind:running")
    gl.create_label("rewind:blocked", "#000000")
    assert engine.diff(cp.id).changes == []


def test_restore_handles_new_deleted_and_binary_files(engine, gl):
    import base64

    gl.commit_files("main", {"docs/a.md": "a\n"})
    cp = engine.checkpoint()
    gl.create_commit("main", "agent", [
        {"action": "create", "file_path": "new.txt", "content": "new\n"},
        {"action": "delete", "file_path": "docs/a.md"},
        {"action": "create", "file_path": "img.bin", "encoding": "base64",
         "content": base64.b64encode(b"\x89PNG\x00\xff").decode()},
        {"action": "update", "file_path": "README.md", "content": "changed\n"},
    ])
    before = engine.load_snapshot(cp.snapshot_id)
    report = engine.rewind(cp.id)
    assert report.zero_drift
    assert gl.commits[gl.branches["main"]["sha"]]["tree"] == \
        gl.commits[before.resources["repository"]["default"]["sha"]]["tree"]


def test_direct_restore_mode(engine, gl):
    from dataclasses import replace

    engine.ctx = replace(engine.ctx, restore_mode="direct")
    cp = engine.checkpoint()
    tree = gl.tree("main")
    gl.commit_files("main", {"README.md": "agent\n"})
    report = engine.rewind(cp.id)
    assert report.zero_drift
    assert gl.tree("main") == tree
    assert not gl.mrs  # no restore MR in direct mode


def test_branch_tag_label_issue_mr_round_trips(engine, gl):
    gl.commit_files("keep", {"k.txt": "1\n"})
    gl.create_tag("v1.0.0", "main", "first")
    gl.create_release("v1.0.0", "v1.0.0", "notes")
    existing = gl.add_issue("existing", labels=["bug"])
    cp = engine.checkpoint()

    gl.delete_release("v1.0.0")
    gl.delete_tag("v1.0.0")
    gl.delete_branch("keep")
    gl.commit_files("scratch", {"s.txt": "x\n"})
    gl.delete_label("bug")
    gl.create_label("agent-made", "#123456")
    gl.update_issue(existing, title="renamed", assignee_ids=[2], add_labels="agent-made")
    new_issue = gl.add_issue("agent-created")

    report = engine.rewind(cp.id)
    assert report.zero_drift, report.verification
    assert "v1.0.0" in gl.tags and gl.releases["v1.0.0"]["description"] == "notes"
    assert "keep" in gl.branches and "scratch" not in gl.branches
    assert "bug" in gl.labels and "agent-made" not in gl.labels
    assert gl.issues[existing]["title"] == "existing"
    assert gl.issues[existing]["labels"] == ["bug"]
    assert gl.issues[existing]["assignees"] == []
    assert gl.issues[new_issue]["state"] == "closed"


def test_flag_strategy_round_trip(engine, gl):
    cp = engine.checkpoint()
    gl.update_feature_flag("new_pricing", {"strategies": [
        {"name": "gradualRolloutUserId", "parameters": {"percentage": "50", "groupId": "default"},
         "scopes": [{"environment_scope": "*"}]}]})
    gl.create_feature_flag({"name": "agent_flag", "active": True, "strategies": []})
    report = engine.rewind(cp.id)
    assert report.zero_drift, report.verification
    assert "agent_flag" not in gl.flags
    assert [s["name"] for s in gl.flags["new_pricing"]["strategies"]] == ["default"]


def test_variable_created_and_metadata_round_trip(engine, gl):
    cp = engine.checkpoint()
    gl.set_variable("AGENT_VAR", "1")
    gl.update_variable("API_URL", "*", protected=True)
    report = engine.rewind(cp.id)
    assert report.zero_drift, report.verification
    assert ("AGENT_VAR", "*") not in gl.variables
    assert gl.variables[("API_URL", "*")]["protected"] is False
    assert gl.variables[("API_URL", "*")]["value"] == "https://api.internal"


def test_restore_falls_back_to_direct_commit_when_mr_cannot_merge(engine, gl, monkeypatch):
    from rewind.http import ApiError

    cp = engine.checkpoint()
    tree = gl.tree("main")
    gl.commit_files("main", {"README.md": "agent\n"})

    def never(*a, **k):
        raise ApiError("PUT", "merge", 405, "Method Not Allowed: pipeline must succeed")

    monkeypatch.setattr(gl, "merge_merge_request", never)
    report = engine.rewind(cp.id)
    assert report.zero_drift, report.verification
    assert gl.tree("main") == tree
    restore_mr = next(m for m in gl.mrs.values() if m["source_branch"].startswith("rewind/"))
    assert restore_mr["state"] == "closed"
    assert "MR merge unavailable" in next(s for s in report.steps if s.kind == "repository").detail


def test_scoped_restore_keeps_later_commits_by_others(engine, gl):
    cp = engine.checkpoint()
    gl.commit_files("agent", {"app/pricing.py": "def legacy_total(items):\n    return 0\n"})
    mr = gl.create_merge_request("agent", "main", "promo")
    merge_sha = gl.merge_merge_request(mr["iid"])["merge_commit_sha"]
    gl.commit_files("main", {"docs/human.md": "my work\n"}, author=("Othmane", "o@example.com"))

    report = engine.rewind(cp.id, code_scope=merge_sha)

    assert report.zero_drift, report.verification
    tree = gl.tree("main")
    assert tree["app/pricing.py"] == "def legacy_total(items):\n    return sum(items)\n"  # run's change undone
    assert tree["docs/human.md"] == "my work\n"  # the human's later commit kept
    assert "kept 1 later commit" in next(s for s in report.steps if s.kind == "repository").detail


def test_scoped_restore_never_overwrites_a_file_changed_again_later(engine, gl):
    cp = engine.checkpoint()
    gl.commit_files("agent", {"app/pricing.py": "agent version\n"})
    mr = gl.create_merge_request("agent", "main", "promo")
    merge_sha = gl.merge_merge_request(mr["iid"])["merge_commit_sha"]
    gl.commit_files("main", {"app/pricing.py": "human fixed it\n"}, author=("Othmane", "o@example.com"))

    report = engine.rewind(cp.id, code_scope=merge_sha)

    assert gl.tree("main")["app/pricing.py"] == "human fixed it\n"
    step = next(s for s in report.steps if s.kind == "repository")
    assert "changed again after the run: app/pricing.py" in step.detail
    assert not report.zero_drift  # honest: the run's change to that file is not undone
