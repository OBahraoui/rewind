"""Shared HTTP plumbing: retries with backoff, structured errors."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("rewind.http")

RETRY_STATUS = {429, 500, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"}


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.method, self.url, self.status, self.body = method, url, status, body
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")


class NotFound(ApiError):
    pass


def _retry_after(resp: httpx.Response | None) -> float | None:
    if resp is None:
        return None
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None


class RetryingClient:
    """httpx.Client wrapper that retries transient failures with jittered backoff.

    A 5xx can arrive *after* the server acted, so repeating a POST could start a second Duo
    flow or post a second note. POSTs therefore retry only 429 (rejected, not processed) and
    connection failures (never sent). Pass `idempotent=True` for POSTs that are safe to repeat
    (GraphQL queries, content-addressed uploads).
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | Callable[[], dict[str, str]],
        *,
        timeout: float = 30.0,
        attempts: int = 5,
        backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._headers = headers
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self.http.close()

    def _hdrs(self) -> dict[str, str]:
        return self._headers() if callable(self._headers) else self._headers

    def request(self, method: str, url: str, *, idempotent: bool | None = None, **kw: Any) -> httpx.Response:
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        extra_headers = kw.pop("headers", None) or {}
        if idempotent is None:
            idempotent = method.upper() in SAFE_METHODS
        for attempt in range(1, self._attempts + 1):
            try:
                resp = self.http.request(method, url, headers={**self._hdrs(), **extra_headers}, **kw)
            except httpx.TransportError as exc:  # connection reset, timeout, DNS
                if not idempotent and not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
                    raise  # the request may have been processed; don't repeat it blindly
                last_exc, resp = exc, None
            else:
                retryable = resp.status_code == 429 or (resp.status_code in RETRY_STATUS and idempotent)
                if not retryable:
                    break
            if attempt == self._attempts:
                break
            delay = _retry_after(resp) or self._backoff * (2 ** (attempt - 1)) * (0.5 + random.random())  # noqa: S311
            log.warning(
                "retrying",
                extra={"method": method, "url": url, "attempt": attempt,
                       "status": resp.status_code if resp is not None else None, "delay_s": round(delay, 2)},
            )
            self._sleep(delay)
        if resp is None:
            assert last_exc is not None
            raise last_exc
        if resp.status_code == 404:
            raise NotFound(method, url, resp.status_code, resp.text)
        if resp.status_code >= 400:
            raise ApiError(method, url, resp.status_code, resp.text)
        return resp

    def json(self, method: str, url: str, *, idempotent: bool | None = None, **kw: Any) -> Any:
        resp = self.request(method, url, idempotent=idempotent, **kw)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
