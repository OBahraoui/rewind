#!/usr/bin/env python3
"""Synthetic monitoring for a deployed environment (stdlib only).

Sends realistic quote traffic for a time window and checks:
  * availability and latency (error rate),
  * business invariants on every response:
      - subtotal matches the catalog
      - 0 <= total <= subtotal
      - no order is discounted more than MAX_DISCOUNT (margin guardrail)
      - legacy pricing charges exactly the subtotal

Any invariant violation fails the job: wrong prices are worse than errors.
Writes monitor-report.json and a JUnit report so results show in the pipeline.

  python ci/monitor.py --url https://... --window 180            # production monitor
  python ci/monitor.py --url https://... --mode smoke              # staging smoke test
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

FULL_CASES = {
    "single-tea": [("tea", 1)],
    "mug-and-tea": [("mug", 2), ("tea", 3)],
    "beans": [("beans", 1)],
    "barista-kit": [("grinder", 1), ("kettle", 1), ("beans", 2)],
    "bulk-tea": [("tea", 20)],
}
SMOKE_CASES = {"mug-and-beans": [("mug", 1), ("beans", 1)]}


def call(method: str, url: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, dict, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, {}, time.monotonic() - t0
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, {"error": str(exc)}, time.monotonic() - t0


def check(case: str, items, status: int, body: dict, latency: float, catalog: dict, args) -> list[str]:
    if status != 200:
        return [f"HTTP {status} {body.get('error', '')}".strip()]
    problems = []
    expected = sum(catalog[sku]["price_cents"] * qty for sku, qty in items)
    sub, total, model = body.get("subtotal_cents"), body.get("total_cents"), body.get("pricing")
    if sub != expected:
        problems.append(f"subtotal {sub} != catalog {expected}")
    if not isinstance(total, int) or total < 0:
        problems.append(f"negative or invalid total {total}")
    elif total > expected:
        problems.append(f"total {total} exceeds subtotal {expected}")
    elif total < expected * (1 - args.max_discount):
        problems.append(f"total {total} is a discount of more than {args.max_discount:.0%} on {expected}")
    if model == "legacy" and total != expected:
        problems.append(f"legacy pricing charged {total}, expected {expected}")
    if latency > args.max_latency:
        problems.append(f"slow response {latency:.2f}s")
    return problems


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--mode", choices=["full", "smoke"], default="full")
    p.add_argument("--window", type=float, default=180, help="seconds of synthetic traffic (full mode)")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--warmup", type=float, default=8.0, help="wait for flag changes to propagate")
    p.add_argument("--max-error-rate", type=float, default=0.02)
    p.add_argument("--max-discount", type=float, default=0.5)
    p.add_argument("--max-latency", type=float, default=2.0)
    p.add_argument("--report", default="monitor-report.json")
    p.add_argument("--junit", default="monitor-junit.xml")
    p.add_argument("--require-flags", action="store_true",
                   help="fail unless the app has loaded GitLab feature flags (use in CI)")
    args = p.parse_args()
    url = args.url.rstrip("/")

    status, health, _ = call("GET", f"{url}/health")
    if status != 200:
        print(f"health check failed: HTTP {status}")
    flags_status = str(health.get("flags_status", "ok"))
    print(f"monitoring {url} revision={health.get('revision')} flags={health.get('flags')} flags_status={flags_status}")
    time.sleep(args.warmup if args.mode == "full" else 0)
    _, catalog, _ = call("GET", f"{url}/products")

    cases = FULL_CASES if args.mode == "full" else SMOKE_CASES
    deadline = time.monotonic() + (args.window if args.mode == "full" else 0)
    results: dict[str, dict] = {c: {"requests": 0, "failures": 0, "problems": []} for c in cases}
    rounds = 0
    while True:
        rounds += 1
        for case, items in cases.items():
            body = {"customer_id": f"synthetic-{rounds}", "items": [{"sku": s, "quantity": q} for s, q in items]}
            st, resp, lat = call("POST", f"{url}/orders/quote", body)
            problems = check(case, items, st, resp, lat, catalog, args) if catalog else ["catalog unavailable"]
            r = results[case]
            r["requests"] += 1
            if problems:
                r["failures"] += 1
                if len(r["problems"]) < 5:
                    r["problems"].append({"round": rounds, "problems": problems, "response": resp})
        if time.monotonic() >= deadline:
            break
        time.sleep(args.interval)

    requests = sum(r["requests"] for r in results.values()) + 1
    failures = sum(r["failures"] for r in results.values()) + (status != 200)
    invariant_breaks = [(c, pr) for c, r in results.items() for v in r["problems"] for pr in v["problems"]
                        if not pr.startswith(("HTTP", "slow"))]
    error_rate = failures / requests
    if flags_status.startswith("error") or (args.require_flags and flags_status != "ok"):
        # Flags silently "off" would hide what's really released.
        invariant_breaks.append(("feature-flags", f"app cannot read feature flags: {flags_status}"))
    verdict = "pass" if error_rate <= args.max_error_rate and not invariant_breaks and status == 200 else "fail"
    report = {"url": url, "mode": args.mode, "revision": health.get("revision"), "flags": health.get("flags"),
              "flags_status": flags_status,
              "requests": requests, "failures": failures, "error_rate": round(error_rate, 4),
              "invariant_violations": len(invariant_breaks), "verdict": verdict, "cases": results}
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(args.junit, "w", encoding="utf-8") as fh:
        fh.write(f'<testsuite name="monitor-{args.mode}" tests="{len(results)}" '
                 f'failures="{sum(1 for r in results.values() if r["failures"])}">\n')
        for case, r in results.items():
            fh.write(f'  <testcase classname="monitor.{args.mode}" name="{case}">\n')
            if r["failures"]:
                detail = escape(json.dumps(r["problems"][:2]))
                fh.write(f'    <failure message="{r["failures"]}/{r["requests"]} failed">{detail}</failure>\n')
            fh.write("  </testcase>\n")
        fh.write("</testsuite>\n")

    print(f"{requests} requests, {failures} failed ({error_rate:.1%}), "
          f"{len(invariant_breaks)} invariant violation(s) → {verdict.upper()}")
    for case, problem in invariant_breaks[:10]:
        print(f"  ✗ {case}: {problem}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
