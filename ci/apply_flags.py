#!/usr/bin/env python3
"""Apply config/flags.yaml to GitLab feature flags for one environment (the `configure` stage).

config/flags.yaml maps flag -> environment -> on/off. Only the given environment's
scope is touched, so staging can be switched before production is deployed.
Idempotent: no API write when the flag already matches.

  FLAGS_TOKEN=... python ci/apply_flags.py --environment production
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    base = os.environ["CI_API_V4_URL"]
    project = urllib.parse.quote(os.environ["CI_PROJECT_ID"], safe="")
    req = urllib.request.Request(
        f"{base}/projects/{project}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"PRIVATE-TOKEN": os.environ["FLAGS_TOKEN"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read()[:300]!r}") from None


def enabled_scopes(flag: dict) -> set[str]:
    if not flag.get("active"):
        return set()
    return {sc["environment_scope"] for s in flag.get("strategies", []) if s["name"] == "default"
            for sc in s.get("scopes", [])}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--environment", required=True)
    p.add_argument("--file", default="config/flags.yaml")
    args = p.parse_args()
    env = args.environment
    with open(args.file, encoding="utf-8") as fh:
        desired = yaml.safe_load(fh) or {}

    for name, per_env in desired.items():
        want = bool((per_env or {}).get(env, False))
        flag = api("GET", f"/feature_flags/{urllib.parse.quote(name)}")
        if flag is None:
            scopes = [env] if want else ["*"]
            api("POST", "/feature_flags", {"name": name, "version": "new_version_flag", "active": want,
                                           "strategies": [{"name": "default", "parameters": {},
                                                           "scopes": [{"environment_scope": s} for s in scopes]}]})
            print(f"created {name}: {env}={'on' if want else 'off'}")
            continue
        scopes = enabled_scopes(flag)
        on = (set(per_env or {}) | {env}) if "*" in scopes else scopes  # "*" = everywhere; made explicit
        target = (on | {env}) if want else (on - {env})
        if target == on:
            print(f"{name}: {env} already {'on' if want else 'off'}")
            continue
        payload: dict = {"active": bool(target)}
        if target:
            destroy = [{"id": s["id"], "_destroy": True} for s in flag.get("strategies", [])]
            payload["strategies"] = [*destroy, {"name": "default", "parameters": {},
                                                "scopes": [{"environment_scope": s} for s in sorted(target)]}]
        api("PUT", f"/feature_flags/{urllib.parse.quote(name)}", payload)
        print(f"{name}: {env} -> {'on' if want else 'off'} (on in: {', '.join(sorted(target)) or 'nowhere'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
