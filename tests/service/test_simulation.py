"""The offline simulation is an end-to-end test of the three demo scenarios."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("simulate", Path(__file__).parents[2] / "scripts" / "simulate.py")
simulate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulate)


def test_ship_rewind_block(tmp_path):
    app, gl, cr = simulate.build(str(tmp_path))
    main_before_promo = None

    shipped = simulate.run_scenario(app, gl, cr, "Bulk discount", "return subtotal(lines) - subtotal(lines) // 10",
                                    monitor_ok=True)
    assert shipped.state.value == "shipped"

    main_before_promo = gl.tree("main")
    prod_before_promo = cr.serving("production")["serving"]
    rewound = simulate.run_scenario(app, gl, cr, "Promo", "return subtotal(lines) - 1500", monitor_ok=False,
                                    touch_legacy=True)
    assert rewound.state.value == "rewound" and rewound.reroutes == 1
    report = app.state.engine.get_report(rewound.rewind_id)
    assert report.zero_drift
    assert gl.tree("main") == main_before_promo
    assert cr.serving("production")["serving"] == prod_before_promo

    blocked = simulate.run_scenario(app, gl, cr, "Receipts", "return subtotal(lines)", monitor_ok=True,
                                    extra={"app/receipts.py": "import smtplib\n"})
    assert blocked.state.value == "blocked"

    html = app.state.repo.summaries()
    assert [r["state"] for r in html] == ["blocked", "rewound", "shipped"]
