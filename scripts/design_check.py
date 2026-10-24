#!/usr/bin/env python3
"""Run impeccable's design detector over every rendered page, in every state.

  python scripts/design_check.py             # needs node; fetches the detector with npx
  make design-check

Serves the offline simulation (all run states) and the storefront (healthy, and with the promo bug)
locally, then scans each rendered page for anti-patterns, contrast failures and layout problems.
Exits non-zero if any page has findings, so it can gate CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def wait(url: str, seconds: int = 120) -> None:
    for _ in range(seconds * 4):
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except OSError:
            time.sleep(0.25)
    raise SystemExit(f"{url} did not come up in {seconds}s")


def serve_app(port: int, buggy: bool) -> None:
    """Run a copy of the storefront in a subprocess; `buggy` plants the demo's promo bug."""
    work = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "app", work / "app")
    if buggy:
        pricing = work / "app" / "pricing.py"
        text = pricing.read_text()
        text = text.replace("def new_pricing_total(lines: list[Line]) -> int:\n    return subtotal(lines)",
                            "def new_pricing_total(lines: list[Line]) -> int:\n    return subtotal(lines) - 1500")
        pricing.write_text(text.replace('if flags.is_enabled("new_pricing", customer_id):', "if True:"))
    code = f"import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', port={port}, log_level='error')"
    subprocess.Popen([sys.executable, "-c", code], cwd=work, env={**os.environ, "PYTHONPATH": str(work)})


def main() -> int:
    if not shutil.which("npx"):
        print("npx not found: install Node.js to run the design check")
        return 2
    from scripts import screenshots

    screenshots.serve_simulation(8781)
    serve_app(8782, buggy=False)
    serve_app(8783, buggy=True)
    for port, path in ((8781, "/api/runs"), (8782, "/health"), (8783, "/health")):
        wait(f"http://127.0.0.1:{port}{path}")
    runs = json.load(urllib.request.urlopen("http://127.0.0.1:8781/api/runs"))
    pages = {"overview": "http://127.0.0.1:8781/", "policy": "http://127.0.0.1:8781/policy",
             "storefront (healthy)": "http://127.0.0.1:8782/", "storefront (promo bug)": "http://127.0.0.1:8783/"}
    for r in runs:
        pages[f"run: {r['state']}"] = f"http://127.0.0.1:8781/runs/{r['id']}"
    failed = 0
    for name, url in pages.items():
        out = subprocess.run(["npx", "--yes", "impeccable", "detect", url], capture_output=True, text=True,
                             timeout=180)
        text = (out.stdout + out.stderr).strip()
        findings = [ln for ln in text.splitlines() if ln.strip().startswith("[")]
        counted = text.split("── Advisory")[0]
        issues = [ln for ln in counted.splitlines() if ln.strip().startswith("[")]
        status = "clean" if not issues else f"{len(issues)} finding(s)"
        advisory = len(findings) - len(issues)
        print(f"{name:<26} {status}" + (f"  (+{advisory} advisory)" if advisory else ""))
        for ln in issues:
            print("    ", ln.strip())
        if "Error:" in text and not findings:
            print("    ", text.splitlines()[-1])
            failed += 1
        failed += bool(issues)
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the simulation's server thread and the app subprocesses die with us
