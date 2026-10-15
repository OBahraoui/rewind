"""Minimal client for GitLab Feature Flags (GitLab serves the Unleash client API).

GitLab scopes strategies by environment using the Unleash app name, so the app
passes its environment name (staging / production) as UNLEASH_APP_NAME.

Fails safe: if GitLab is unreachable the last known state is kept, and a flag
that was never fetched is off.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import zlib

import httpx

log = logging.getLogger("app.flags")


class FlagClient:
    def __init__(self, url: str = "", instance_id: str = "", app_name: str = "", refresh_s: float = 5.0):
        self.url = url.rstrip("/")
        self.instance_id = instance_id
        self.app_name = app_name
        self.refresh_s = refresh_s
        self._features: dict[str, dict] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._overrides: dict[str, bool] = {}

    @classmethod
    def from_env(cls) -> FlagClient:
        return cls(os.environ.get("UNLEASH_URL", ""), os.environ.get("UNLEASH_INSTANCE_ID", ""),
                   os.environ.get("UNLEASH_APP_NAME", ""), float(os.environ.get("FLAGS_REFRESH_SECONDS", "5")))

    @property
    def configured(self) -> bool:
        return bool(self.url and self.instance_id and self.app_name)

    def override(self, name: str, value: bool | None) -> None:
        """For tests: force a flag on/off (None removes the override)."""
        if value is None:
            self._overrides.pop(name, None)
        else:
            self._overrides[name] = value

    def _refresh(self) -> None:
        if not self.configured or time.monotonic() - self._fetched_at < self.refresh_s:
            return
        with self._lock:
            if time.monotonic() - self._fetched_at < self.refresh_s:
                return
            self._fetched_at = time.monotonic()
            try:
                resp = httpx.get(f"{self.url}/client/features", timeout=2.0, headers={
                    "UNLEASH-INSTANCEID": self.instance_id, "UNLEASH-APPNAME": self.app_name})
                resp.raise_for_status()
                self._features = {f["name"]: f for f in resp.json().get("features", [])}
            except Exception as exc:
                log.warning("feature flag refresh failed: %s", exc)

    def is_enabled(self, name: str, user_id: str = "") -> bool:
        if name in self._overrides:
            return self._overrides[name]
        self._refresh()
        feature = self._features.get(name)
        if not feature or not feature.get("enabled"):
            return False
        return any(self._strategy(s, name, user_id) for s in feature.get("strategies") or [])

    @staticmethod
    def _strategy(s: dict, flag: str, user_id: str) -> bool:
        params = s.get("parameters") or {}
        kind = s.get("name")
        if kind == "default":
            return True
        if kind == "userWithId":
            return user_id in {u.strip() for u in params.get("userIds", "").split(",")}
        if kind in ("gradualRolloutUserId", "flexibleRollout"):
            pct = int(params.get("percentage") or params.get("rollout") or 0)
            bucket = zlib.crc32(f"{params.get('groupId', flag)}:{user_id}".encode()) % 100 + 1
            return bool(user_id) and bucket <= pct
        return False  # unknown strategy: fail closed

    def snapshot(self) -> dict[str, bool]:
        self._refresh()
        names = set(self._features) | set(self._overrides)
        return {n: self.is_enabled(n) for n in sorted(names)}


flags = FlagClient.from_env()
