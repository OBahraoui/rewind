"""Cloud Run Admin API v2: read which revision serves traffic, and pin traffic.

Auth needs no Google SDK: on Cloud Run the token comes from the metadata
server; locally from GOOGLE_OAUTH_ACCESS_TOKEN or `gcloud auth print-access-token`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any

import httpx

from rewind.http import NotFound, RetryingClient

METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"  # noqa: S105


class GoogleToken:
    """Caches an OAuth access token until shortly before it expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def __call__(self) -> str:
        with self._lock:
            if time.time() < self._expires - 60:
                return self._token
            self._token, ttl = self._fetch()
            self._expires = time.time() + ttl
            return self._token

    @staticmethod
    def _fetch() -> tuple[str, float]:
        if tok := os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip():
            return tok, 300
        if os.environ.get("K_SERVICE"):  # running on Cloud Run
            resp = httpx.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
            resp.raise_for_status()
            body = resp.json()
            return body["access_token"], float(body.get("expires_in", 300))
        gcloud = shutil.which("gcloud")
        if not gcloud:
            raise RuntimeError("no Google credentials: set GOOGLE_OAUTH_ACCESS_TOKEN or install gcloud")
        out = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True,
                             check=True, timeout=30)
        return out.stdout.strip(), 600


def short_revision(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def serving_revisions(service: dict[str, Any]) -> list[list[Any]]:
    """Resolved [revision, percent] pairs actually receiving traffic, sorted."""
    latest = short_revision(service.get("latestReadyRevision", ""))
    statuses = service.get("trafficStatuses") or service.get("traffic") or []
    out: dict[str, int] = {}
    for t in statuses:
        pct = int(t.get("percent", 0))
        if pct <= 0:
            continue
        rev = short_revision(t.get("revision", "")) if t.get("revision") else latest
        if t.get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST":
            rev = latest
        out[rev] = out.get(rev, 0) + pct
    return sorted([[r, p] for r, p in out.items()])


class CloudRunClient:
    def __init__(self, project: str, region: str, services: dict[str, str], *,
                 transport: httpx.BaseTransport | None = None, token: GoogleToken | None = None,
                 sleep=time.sleep):
        self.project, self.region, self.services = project, region, services
        self._sleep = sleep
        tok = token or GoogleToken()
        self._api = RetryingClient("https://run.googleapis.com/v2",
                                   lambda: {"Authorization": f"Bearer {tok()}"},
                                   transport=transport, sleep=sleep)

    def _name(self, environment: str) -> str:
        return f"projects/{self.project}/locations/{self.region}/services/{self.services[environment]}"

    def get_service(self, environment: str) -> dict | None:
        try:
            return self._api.json("GET", f"/{self._name(environment)}")
        except NotFound:
            return None

    def serving(self, environment: str) -> dict[str, Any] | None:
        svc = self.get_service(environment)
        if svc is None:
            return None
        return {
            "service": self.services[environment],
            "serving": serving_revisions(svc),
            "_latest_ready": short_revision(svc.get("latestReadyRevision", "")),
            "_uri": svc.get("uri", ""),
        }

    def list_revisions(self, environment: str) -> list[str]:
        """Ready revision names, newest first."""
        body = self._api.json("GET", f"/{self._name(environment)}/revisions")
        revs = sorted(body.get("revisions", []), key=lambda r: r.get("createTime", ""), reverse=True)
        ready = [r for r in revs if any(c.get("type") == "Ready" and c.get("state") == "CONDITION_SUCCEEDED"
                                        for c in r.get("conditions", []))]
        return [short_revision(r["name"]) for r in ready]

    def pin_traffic(self, environment: str, split: list[list[Any]], *, timeout_s: float = 120) -> None:
        """Route traffic exactly as `split` ([[revision, percent], ...]) and wait until it is live."""
        svc = self.get_service(environment)
        if svc is None:
            raise RuntimeError(f"Cloud Run service for {environment!r} not found")
        svc["traffic"] = [
            {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": rev, "percent": int(pct)}
            for rev, pct in split
        ]
        # Send the etag back so a concurrent deploy makes this fail instead of being clobbered.
        op = self._api.json("PATCH", f"/{self._name(environment)}", json=svc)
        self._wait(op, timeout_s)

    def _wait(self, op: dict, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while not op.get("done"):
            if time.monotonic() > deadline:
                raise TimeoutError(f"Cloud Run operation {op.get('name')} did not finish")
            self._sleep(1.0)
            op = self._api.json("GET", f"/{op['name']}")
        if op.get("error"):
            raise RuntimeError(f"Cloud Run operation failed: {op['error']}")
