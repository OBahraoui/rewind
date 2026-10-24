#!/usr/bin/env python3
"""Screenshot every dashboard page (light, dark, phone) for design review and the README.

  python scripts/screenshots.py                       # offline simulator with realistic timings
  python scripts/screenshots.py --url https://rewind-....run.app --out docs/screenshots

Needs Playwright (dev only): pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def serve_simulation(port: int) -> None:
    import uvicorn

    from scripts import simulate

    tmp = tempfile.mkdtemp()
    app, gl, cr = simulate.build(tmp)
    simulate.add_latency(gl, cr)
    bulk = "n = sum(q for _, q in lines)\n    s = subtotal(lines)\n    return s - s // 10 if n >= 10 else s"
    receipts = ("import httpx\n\n\ndef send(order):\n"
                "    httpx.post('https://receipts.example.com/v1/send', json=order)\n")
    simulate.run_scenario(app, gl, cr, "Bulk discount: 10% off 10+ items", bulk, monitor_ok=True)
    simulate.run_scenario(app, gl, cr, "Launch promo: $15 off every order", "return subtotal(lines) - 1500",
                          monitor_ok=False)
    simulate.run_scenario(app, gl, cr, "Email a receipt when an order is placed", "return subtotal(lines)",
                          monitor_ok=True, extra={"app/receipts.py": receipts})
    simulate.in_progress(app, gl, "Seasonal pricing: 5% off kettles")
    threading.Thread(target=uvicorn.run, args=(app,), kwargs={"host": "127.0.0.1", "port": port,
                                                             "log_level": "warning"}, daemon=True).start()


def wait(url: str) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/api/runs", timeout=2)
            return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"{url} did not come up")


def readme_shots(url: str, out: Path) -> None:
    """The two images at the top of the README: the newest rewind that rescued production, and the overview."""
    from playwright.sync_api import sync_playwright

    runs = json.load(urllib.request.urlopen(f"{url}/api/runs"))
    rewound = [r for r in runs if r["state"] == "rewound"]
    if not rewound:
        raise SystemExit("no rewound run on this service yet")
    best = next((r for r in rewound if (r.get("rewind") or {}).get("mitigated_after_s") is not None), rewound[0])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 860}, device_scale_factor=2,
                                   color_scheme="light").new_page()
        for name, path in (("rewound-run", f"/runs/{best['id']}"), ("overview", "/")):
            page.goto(url + path, wait_until="networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / f"{name}.png"))
        browser.close()
    print(f"README images from run {best['id']} (#{best['issue_iid']}) in {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="")
    p.add_argument("--out", default="")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--readme", action="store_true", help="only the README images, into docs/readme")
    args = p.parse_args()
    url = args.url.rstrip("/")
    if args.readme:
        if not url:
            raise SystemExit("--readme needs --url (the live service)")
        readme_shots(url, Path(args.out or Path(__file__).parents[1] / "docs" / "readme"))
        return
    if not url:
        serve_simulation(args.port)
        url = f"http://127.0.0.1:{args.port}"
    wait(url)
    out = Path(args.out or tempfile.mkdtemp(prefix="rewind-shots-"))
    out.mkdir(parents=True, exist_ok=True)
    runs = json.load(urllib.request.urlopen(f"{url}/api/runs"))
    pages = {"overview": "/", "policy": "/policy", "not-found": "/runs/nope"}
    for r in runs:
        pages.setdefault(f"run-{r['state']}", f"/runs/{r['id']}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme, viewport in (("light", {"width": 1360, "height": 900}), ("dark", {"width": 1360, "height": 900}),
                                ("phone", {"width": 390, "height": 844})):
            ctx = browser.new_context(viewport=viewport, color_scheme="dark" if theme == "dark" else "light",
                                      device_scale_factor=2 if theme == "phone" else 1)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("console", lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            for name, path in pages.items():
                page.goto(url + path, wait_until="networkidle")
                page.wait_for_timeout(300)
                file = out / f"{name}-{theme}.png"
                page.screenshot(path=str(file), full_page=True)
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                print(f"{file.name:<34} {'HORIZONTAL OVERFLOW ' if overflow else ''}")
            errors = [e for e in errors if "404" not in e]  # the not-found page answers 404 on purpose
            if errors:
                print(f"  console errors ({theme}):", errors[:5])
            ctx.close()
        browser.close()
    print(f"\nScreenshots in {out}")


if __name__ == "__main__":
    main()
