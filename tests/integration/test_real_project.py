"""Against the real GitLab project (and Cloud Run, if configured). Opt-in:

    pytest -m integration tests/integration

Changes a feature flag, a label and staging traffic, rewinds, and requires zero drift.
"""

from __future__ import annotations

import secrets

import pytest

from rewind.cli.main import _engine
from rewind.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():
    s = Settings.from_env()
    if not (s.gitlab_token and s.project_id and s.hash_key):
        pytest.skip("needs GITLAB_TOKEN, GITLAB_PROJECT_ID and REWIND_HASH_KEY in .env")
    return _engine(s)


def test_rewind_restores_flag_label_and_traffic(engine):
    gl, cr = engine.ctx.gitlab, engine.ctx.cloudrun
    flag = "rewind_integration_test"
    if not gl.get_feature_flag(flag):
        gl.create_feature_flag({"name": flag, "version": "new_version_flag", "active": False, "strategies": [
            {"name": "default", "parameters": {}, "scopes": [{"environment_scope": "*"}]}]})
    label = f"rewind-it-{secrets.token_hex(3)}"
    old_revision = None
    if cr and "staging" in cr.services:
        revisions = cr.list_revisions("staging")
        if len(revisions) >= 2:
            old_revision = revisions[1]

    cp = engine.checkpoint("integration test")
    serving_before = cp.serving.get("staging")
    try:
        gl.update_feature_flag(flag, {"active": True})
        gl.create_label(label, "#123456", "created by the integration test")
        if old_revision:
            cr.pin_traffic("staging", [[old_revision, 100]])

        plan = engine.plan(cp.id)
        kinds = {s.kind for s in plan.steps}
        assert {"feature_flag", "label"} <= kinds

        report = engine.rewind(cp.id, reason="integration test")
        assert report.zero_drift, report.verification
        assert gl.get_feature_flag(flag)["active"] is False
        assert gl.get_label(label) is None
        if old_revision:
            assert cr.serving("staging")["serving"][0][0] == serving_before

        again = engine.rewind(cp.id, reason="integration test (idempotence)")
        assert again.zero_drift
        assert all(s.outcome.value in ("already", "irreversible") for s in again.steps)
    finally:
        gl.delete_feature_flag(flag)
        if gl.get_label(label):
            gl.delete_label(label)
