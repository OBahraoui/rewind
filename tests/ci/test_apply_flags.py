"""ci/apply_flags.py touches only the requested environment and is idempotent."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("apply_flags", Path(__file__).parents[2] / "ci" / "apply_flags.py")
apply_flags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_flags)


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"flag": {"name": "new_pricing", "active": False,
                      "strategies": [{"id": 1, "name": "default", "scopes": [{"environment_scope": "*"}]}]}}
    writes = []

    def api(method, path, body=None):
        if method == "GET":
            return state["flag"]
        writes.append((method, body))
        flag = state["flag"]
        flag["active"] = body["active"]
        if "strategies" in body:
            flag["strategies"] = [{"id": 9, "name": s["name"], "scopes": s["scopes"]}
                                  for s in body["strategies"] if not s.get("_destroy")]
        return flag

    monkeypatch.setattr(apply_flags, "api", api)
    cfg = tmp_path / "flags.yaml"

    def run(env, desired):
        cfg.write_text("new_pricing:\n" + "".join(f"  {k}: {str(v).lower()}\n" for k, v in desired.items()))
        monkeypatch.setattr("sys.argv", ["apply_flags", "--environment", env, "--file", str(cfg)])
        apply_flags.main()
        return apply_flags.enabled_scopes(state["flag"])

    return run, writes


def test_staging_then_production(world):
    run, writes = world
    desired = {"staging": True, "production": True}
    assert run("staging", desired) == {"staging"}       # production untouched before its deploy
    assert run("production", desired) == {"staging", "production"}
    n = len(writes)
    assert run("production", desired) == {"staging", "production"}
    assert len(writes) == n                              # idempotent: no write when already right


def test_switch_off_one_environment(world):
    run, _ = world
    run("staging", {"staging": True, "production": True})
    run("production", {"staging": True, "production": True})
    assert run("production", {"staging": True, "production": False}) == {"staging"}
    assert run("staging", {"staging": False, "production": False}) == set()
