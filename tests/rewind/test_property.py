"""Property: any sequence of undoable agent actions, then rewind, returns to the checkpoint.

Run longer with:  REWIND_PROPERTY_EXAMPLES=500 pytest tests/rewind/test_property.py
"""

from __future__ import annotations

import os
import tempfile

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rewind.engine import Rewind
from rewind.models import comparable
from rewind.policy import Policy
from rewind.resources import Context
from rewind.store import FileStore
from rewind.testing import FakeCloudRun, FakeGitLab

POLICY = Policy.load()
PATHS = ["README.md", "app/pricing.py", "app/new.py", "config/flags.yaml", "docs/x.md"]
NAMES = ["a", "b", "c"]

action = st.one_of(
    st.tuples(st.just("commit"), st.sampled_from(PATHS), st.one_of(st.none(), st.text(max_size=12))),
    st.tuples(st.just("branch"), st.sampled_from(NAMES), st.booleans()),
    st.tuples(st.just("flag"), st.sampled_from(["new_pricing", "agent_flag"]), st.booleans()),
    st.tuples(st.just("label"), st.sampled_from(["bug", "x", "y"]), st.booleans()),
    st.tuples(st.just("issue_new"), st.sampled_from(NAMES), st.booleans()),
    st.tuples(st.just("issue_edit"), st.sampled_from(["bug", "x"]), st.booleans()),
    st.tuples(st.just("release"), st.sampled_from(["v1", "v2"]), st.booleans()),
    st.tuples(st.just("deploy"), st.sampled_from(["staging", "production"]), st.booleans()),
    st.tuples(st.just("var"), st.sampled_from(["NEW_VAR", "API_URL"]), st.booleans()),
    st.tuples(st.just("mr"), st.sampled_from(NAMES), st.booleans()),
)


def world():
    gl = FakeGitLab(files={"README.md": "r\n", "app/pricing.py": "p\n", "config/flags.yaml": "f\n"})
    gl.labels["bug"] = {"color": "#d9534f", "description": ""}
    gl.add_flag("new_pricing", active=False)
    gl.set_variable("API_URL", "https://api.internal")
    gl.add_issue("existing", labels=["bug"])
    cr = FakeCloudRun()
    ctx = Context(gitlab=gl, cloudrun=cr, policy=POLICY, hash_key=b"k" * 32, sleep=lambda _s: None)
    return gl, cr, ctx


def perform(gl, cr, act) -> None:
    kind, name, flag = act
    try:
        if kind == "commit":
            gl.commit_files("main", {name: flag if flag is None else flag + "\n"})
        elif kind == "branch":
            if flag:
                gl.commit_files(f"br-{name}", {"README.md": f"{name}\n"})
            elif f"br-{name}" in gl.branches:
                gl.delete_branch(f"br-{name}")
        elif kind == "flag":
            if name in gl.flags:
                gl.update_feature_flag(name, {"active": flag})
            else:
                gl.add_flag(name, flag)
        elif kind == "label":
            if flag and name not in gl.labels:
                gl.create_label(name, "#00ff00")
            elif not flag and name in gl.labels:
                gl.delete_label(name)
        elif kind == "issue_new":
            gl.add_issue(f"agent {name}", labels=["x"] if flag else [])
        elif kind == "issue_edit":
            if name in gl.labels:
                gl.update_issue(1, **({"add_labels": name} if flag else {"remove_labels": name}))
            gl.update_issue(1, state_event="close" if flag else "reopen")
        elif kind == "release":
            if flag and name not in gl.tags:
                gl.create_tag(name, "main")
                gl.create_release(name, name, "notes")
            elif not flag and name in gl.releases:
                gl.delete_release(name)
        elif kind == "deploy":
            cr.deploy(name)
        elif kind == "var":
            if name == "NEW_VAR":
                if flag:
                    gl.set_variable(name, "v")
                else:
                    gl.variables.pop((name, "*"), None)
            else:
                gl.update_variable(name, "*", protected=flag)
        elif kind == "mr":
            branch = f"feat-{name}"
            gl.commit_files(branch, {"app/new.py": f"{name}\n"})
            mr = gl.create_merge_request(branch, "main", f"feat {name}", description="Closes #1")
            if flag:
                gl.merge_merge_request(mr["iid"])
    except Exception:
        pass


@settings(max_examples=int(os.environ.get("REWIND_PROPERTY_EXAMPLES", "60")), deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(action, min_size=1, max_size=12))
def test_rewind_always_returns_to_checkpoint(actions):
    gl, cr, ctx = world()
    with tempfile.TemporaryDirectory() as tmp:
        engine = Rewind(ctx, FileStore(tmp), "42")
        cp = engine.checkpoint()
        before = engine.load_snapshot(cp.snapshot_id)
        for act in actions:
            perform(gl, cr, act)

        report = engine.rewind(cp.id)

        assert report.zero_drift, (actions, report.verification, [s for s in report.steps if s.detail])
        # Reversible state is back exactly.
        after = engine.snapshot()
        for kind in ("feature_flag", "label", "branch", "tag", "release", "cloud_run"):
            assert {k: comparable(v) for k, v in after.resources[kind].items()} == \
                   {k: comparable(v) for k, v in before.resources[kind].items()}, kind
        assert gl.tree("main") == {p: c[0].decode() for p, c in
                                   gl.commits[cp.default_sha]["tree"].items()}

        # And a second rewind changes nothing.
        writes = len(gl.calls)
        again = engine.rewind(cp.id)
        assert again.zero_drift
        assert [c for c in gl.calls[writes:] if c[0] != "create_branch"] == []
