from __future__ import annotations

import pytest

from rewind.gate import added_lines, evaluate

PRICING = '''from app import flags


def legacy_total(items):
    return sum(items)


def new_pricing_total(items):
    return sum(items)


def total(items):
    if flags.is_enabled("new_pricing"):
        return new_pricing_total(items)
    return legacy_total(items)
'''


def base(gl):
    return gl.commit_files("main", {"app/pricing.py": PRICING})


def test_flagged_change_is_allowed(engine, gl):
    b = base(gl)
    cp = engine.checkpoint()
    head = gl.commit_files("agent", {"app/pricing.py": PRICING.replace(
        "def new_pricing_total(items):\n    return sum(items)",
        "def new_pricing_total(items):\n    return sum(items) * 0.9"),
        "config/flags.yaml": "new_pricing:\n  staging: true\n  production: true\n"})
    d = evaluate(engine.ctx, engine.diff(cp.id), b, head)
    assert d.verdict == "allow", d.findings
    assert d.counts["irreversible"] == 0


def test_change_to_live_default_path_is_rerouted(engine, gl):
    b = base(gl)
    head = gl.commit_files("agent", {"app/pricing.py": PRICING.replace(
        "def legacy_total(items):\n    return sum(items)", "def legacy_total(items):\n    return sum(items) - 15")})
    d = evaluate(engine.ctx, None, b, head)
    assert d.verdict == "reroute"
    assert d.findings[0].rule_id == "default-path-frozen"
    assert "new_pricing_total" in d.instructions[0]


def test_reformatting_the_frozen_function_is_fine(engine, gl):
    b = base(gl)
    head = gl.commit_files("agent", {"app/pricing.py": PRICING.replace(
        "def legacy_total(items):\n    return sum(items)",
        "def legacy_total(items):\n    # unchanged behaviour\n    return sum(\n        items\n    )")})
    assert evaluate(engine.ctx, None, b, head).verdict == "allow"


def test_external_side_effect_is_blocked_with_line(engine, gl):
    b = base(gl)
    head = gl.commit_files("agent", {"app/notify.py": "import os\n\nimport httpx\n\n\ndef send(o):\n"
                                                      "    httpx.post('https://mail.example.com', json=o)\n"})
    d = evaluate(engine.ctx, None, b, head)
    assert d.verdict == "block"
    f = next(f for f in d.findings if f.rule_id == "external-side-effect")
    assert f.path == "app/notify.py" and f.line == 3 and "httpx" in f.evidence


def test_migration_without_downgrade_is_blocked(engine, gl):
    b = base(gl)
    migration = "def upgrade(db):\n    db.drop('legacy')\n"
    head = gl.commit_files("agent", {"app/migrations/0002_drop_legacy.py": migration})
    d = evaluate(engine.ctx, None, b, head)
    assert d.verdict == "block"
    assert d.findings[0].rule_id == "migration-without-downgrade"
    reversible = "def upgrade(db):\n    pass\n\n\ndef downgrade(db):\n    pass\n"
    ok = gl.commit_files("agent2", {"app/migrations/0002_x.py": reversible})
    assert evaluate(engine.ctx, None, b, ok).verdict == "allow"


def test_agents_cannot_touch_guardrails(engine, gl):
    b = base(gl)
    for path in ("rewind/policy.yaml", ".gitlab-ci.yml", "flows/rewind_planner.yml", ".gitlab/duo/agent-config.yml"):
        head = gl.commit_files(f"agent-{path}", {path: "relaxed: true\n"})
        d = evaluate(engine.ctx, None, b, head)
        assert d.verdict == "block", path
        assert d.findings[0].rule_id == "protected-path"


def test_irreversible_state_change_and_blind_spots_block(engine, gl):
    b = base(gl)
    cp = engine.checkpoint()
    gl.update_variable("API_URL", "*", value="changed")
    d = evaluate(engine.ctx, engine.diff(cp.id), b, b)
    assert d.verdict == "block" and d.findings[0].rule_id == "irreversible-change"

    gl.fail_next["list_labels"] = RuntimeError("down")
    d = evaluate(engine.ctx, engine.diff(cp.id), b, b)
    assert any(f.rule_id == "blind-spot" for f in d.findings)


def test_added_lines_tracks_line_numbers():
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -10,1 +11,2 @@\n z\n+y\n"
    assert added_lines(diff) == [(2, "b"), (12, "y")]


@pytest.mark.parametrize("code", [
    "from urllib import request\n\n\ndef send(o):\n    request.urlopen('https://receipts.example.com')\n",
    "import http.client\n",
    "import requests\n",
    "from httpx import post\n",
    "import smtplib\n",
    "import urllib3\n",
])
def test_every_common_way_to_call_out_is_caught(engine, gl, code):
    b = base(gl)
    head = gl.commit_files(f"agent-{abs(hash(code))}", {"app/receipts.py": code})
    d = evaluate(engine.ctx, None, b, head)
    assert d.verdict == "block" and d.findings[0].rule_id == "external-side-effect"
