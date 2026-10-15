"""Configuration state: feature flags, CI/CD variables, Cloud Run traffic."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from rewind.models import Change
from rewind.resources.base import Context, Handler


def _strategy(s: dict) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": s["name"],
        "parameters": s.get("parameters") or {},
        "scopes": sorted(sc["environment_scope"] for sc in s.get("scopes") or []),
    }
    if s.get("user_list"):
        out["user_list_id"] = s["user_list"]["id"]
    return out


class FeatureFlagHandler(Handler):
    kind = "feature_flag"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return {f["name"]: self._state(f) for f in ctx.gitlab.list_feature_flags()}

    @staticmethod
    def _state(f: dict) -> dict[str, Any]:
        strategies = sorted((_strategy(s) for s in f.get("strategies") or []),
                            key=lambda s: json.dumps(s, sort_keys=True))
        return {"active": bool(f.get("active")), "description": f.get("description") or "",
                "strategies": strategies}

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        f = ctx.gitlab.get_feature_flag(key)
        return self._state(f) if f else None

    def summarize(self, change: Change) -> str:
        if change.op == "updated" and change.fields == ["active"]:
            state = "on" if (change.after or {}).get("active") else "off"
            return f"feature flag {change.key} switched {state}"
        if change.op == "updated":
            return f"feature flag {change.key} changed {', '.join(change.fields)}"
        return f"feature flag {change.key} {change.op}"

    def action(self, change: Change) -> str:
        if change.op == "created":
            return f"delete feature flag {change.key}"
        if change.op == "deleted":
            return f"recreate feature flag {change.key}"
        if change.fields == ["active"]:
            return f"switch feature flag {change.key} {'on' if change.before['active'] else 'off'}"
        return f"restore feature flag {change.key} ({', '.join(change.fields)})"

    def priority(self, change: Change) -> int:
        return 20

    @staticmethod
    def _payload_strategies(strategies: list[dict]) -> list[dict]:
        out = []
        for s in strategies:
            item: dict[str, Any] = {"name": s["name"], "parameters": s["parameters"],
                                    "scopes": [{"environment_scope": e} for e in s["scopes"]]}
            if "user_list_id" in s:
                item["user_list_id"] = s["user_list_id"]
            out.append(item)
        return out

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        b = change.before
        if b is None:
            ctx.gitlab.delete_feature_flag(change.key)
            return f"deleted feature flag {change.key}"
        if live is None:
            ctx.gitlab.create_feature_flag({
                "name": change.key, "version": "new_version_flag", "active": b["active"],
                "description": b["description"], "strategies": self._payload_strategies(b["strategies"]),
            })
            return f"recreated feature flag {change.key}"
        payload: dict[str, Any] = {}
        if b["active"] != live["active"]:
            payload["active"] = b["active"]
        if b["description"] != live["description"]:
            payload["description"] = b["description"]
        if b["strategies"] != live["strategies"]:
            raw = ctx.gitlab.get_feature_flag(change.key) or {}
            destroy = [{"id": s["id"], "_destroy": True} for s in raw.get("strategies") or []]
            payload["strategies"] = destroy + self._payload_strategies(b["strategies"])
        ctx.gitlab.update_feature_flag(change.key, payload)
        return f"feature flag {change.key}: restored {', '.join(sorted(payload))}"


class VariableHandler(Handler):
    """CI/CD variables. Values are never stored: only a keyed HMAC fingerprint."""

    kind = "ci_variable"

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return {f"{v['key']}@{v.get('environment_scope', '*')}": self._state(ctx, v)
                for v in ctx.gitlab.list_variables()}

    @staticmethod
    def _state(ctx: Context, v: dict) -> dict[str, Any]:
        value = v.get("value")
        scope = v.get("environment_scope", "*")
        return {
            "variable_type": v.get("variable_type", "env_var"),
            "protected": bool(v.get("protected")),
            "masked": bool(v.get("masked")),
            "raw": bool(v.get("raw")),
            "value_fp": ctx.fingerprint(v["key"], scope, value) if value is not None else None,
        }

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        name, _, scope = key.rpartition("@")
        return name, scope or "*"

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        name, scope = self._split(key)
        v = ctx.gitlab.get_variable(name, scope)
        return self._state(ctx, v) if v else None

    def summarize(self, change: Change) -> str:
        name, scope = self._split(change.key)
        what = "value changed" if "value_fp" in change.fields else change.op
        if change.op == "updated" and "value_fp" not in change.fields:
            what = f"changed {', '.join(change.fields)}"
        return f"CI/CD variable {name} ({scope}) {what}"

    def action(self, change: Change) -> str:
        name, _ = self._split(change.key)
        return f"delete variable {name}" if change.op == "created" else f"restore settings of variable {name}"

    def priority(self, change: Change) -> int:
        return 80

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        name, scope = self._split(change.key)
        if change.before is None:
            ctx.gitlab.delete_variable(name, scope)
            return f"deleted variable {name}"
        # Metadata-only restore: GitLab requires the value, so resend the current one untouched.
        current = ctx.gitlab.get_variable(name, scope)
        if not current or current.get("value") is None:
            raise RuntimeError(f"variable {name} is hidden; settings cannot be restored without its value")
        b = change.before
        ctx.gitlab.update_variable(name, scope, value=current["value"], variable_type=b["variable_type"],
                                   protected=b["protected"], masked=b["masked"], raw=b["raw"])
        return f"restored settings of variable {name}"


class CloudRunHandler(Handler):
    """Which revision serves each environment. Undo = pin traffic to the checkpoint revision."""

    kind = "cloud_run"

    def enabled(self, ctx: Context) -> bool:
        return ctx.cloudrun is not None and bool(ctx.cloudrun.services)

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        out = {}
        for env in ctx.cloudrun.services:
            state = ctx.cloudrun.serving(env)
            if state:
                out[env] = state
        return out

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        return ctx.cloudrun.serving(key)

    @staticmethod
    def _fmt(state: dict | None) -> str:
        if not state:
            return "nothing"
        return ", ".join(f"{rev} ({pct}%)" for rev, pct in state["serving"])

    def summarize(self, change: Change) -> str:
        return f"{change.key} now serves {self._fmt(change.after)} (was {self._fmt(change.before)})"

    def action(self, change: Change) -> str:
        return f"route {change.key} traffic to {self._fmt(change.before)}"

    def priority(self, change: Change) -> int:
        return 10  # stop the bleeding first

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        assert change.before
        ctx.cloudrun.pin_traffic(change.key, change.before["serving"])
        if ctx.checkpoint_sha:
            # Record the rollback on the GitLab environment page; best effort.
            with suppress(Exception):
                ctx.gitlab.create_deployment(change.key, ctx.checkpoint_sha, ctx.meta["default_branch"])
        return f"{change.key} traffic → {self._fmt(change.before)}"
