"""Durable JSON storage for snapshots, checkpoints, runs and reports.

Two backends with one interface:
  file://<dir>     local development and tests
  gs://<bucket>    Cloud Storage (JSON API over httpx, no SDK)

`create` is atomic create-if-absent in both, which is what the project lock is
built on: only one rewind per project can hold it.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from rewind.cloudrun import GoogleToken
from rewind.http import ApiError, NotFound, RetryingClient


class AlreadyExists(RuntimeError):
    pass


class LockHeld(RuntimeError):
    pass


class Store(ABC):
    @abstractmethod
    def put(self, key: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def create(self, key: str, data: dict[str, Any]) -> None:
        """Write only if `key` does not exist; raise AlreadyExists otherwise."""

    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @contextmanager
    def lock(self, name: str, *, ttl_s: int = 900, owner: str = ""):
        """Mutual exclusion across processes. A lock older than ttl_s is stale and is broken."""
        key = f"locks/{name}.json"
        record = {"owner": owner, "acquired_at": time.time(), "ttl_s": ttl_s}
        try:
            self.create(key, record)
        except AlreadyExists:
            held = self.get(key) or {}
            if time.time() - float(held.get("acquired_at", 0)) < float(held.get("ttl_s", ttl_s)):
                raise LockHeld(f"{name} is locked by {held.get('owner') or 'another process'}") from None
            self.delete(key)
            self.create(key, record)
        try:
            yield
        finally:
            self.delete(key)


class FileStore(Store):
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"invalid key {key!r}")
        return p

    def put(self, key: str, data: dict[str, Any]) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, default=str, indent=1), encoding="utf-8")
        tmp.replace(p)  # atomic on POSIX

    def create(self, key: str, data: dict[str, Any]) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise AlreadyExists(key) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str)

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list(self, prefix: str) -> list[str]:
        base = self.root.resolve()
        out = []
        for p in base.rglob("*.json"):
            key = p.relative_to(base).as_posix()
            if key.startswith(prefix):
                out.append(key)
        return sorted(out)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class GCSStore(Store):
    def __init__(self, bucket: str, *, transport: httpx.BaseTransport | None = None,
                 token: GoogleToken | None = None):
        tok = token or GoogleToken()
        self.bucket = bucket
        self._api = RetryingClient("https://storage.googleapis.com",
                                   lambda: {"Authorization": f"Bearer {tok()}"}, transport=transport)

    def _upload(self, key: str, data: dict[str, Any], **params: Any) -> None:
        self._api.request(
            "POST", f"/upload/storage/v1/b/{self.bucket}/o",
            params={"uploadType": "media", "name": key, **params},
            content=json.dumps(data, default=str).encode(),
            headers={"Content-Type": "application/json"},
        )

    def put(self, key: str, data: dict[str, Any]) -> None:
        self._upload(key, data)

    def create(self, key: str, data: dict[str, Any]) -> None:
        try:
            self._upload(key, data, ifGenerationMatch=0)
        except ApiError as exc:
            if exc.status == 412:
                raise AlreadyExists(key) from None
            raise

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            resp = self._api.request("GET", f"/storage/v1/b/{self.bucket}/o/{quote(key, safe='')}",
                                     params={"alt": "media"})
        except NotFound:
            return None
        return resp.json()

    def list(self, prefix: str) -> list[str]:
        out: list[str] = []
        token = None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            body = self._api.json("GET", f"/storage/v1/b/{self.bucket}/o", params=params)
            out.extend(i["name"] for i in body.get("items", []))
            token = body.get("nextPageToken")
            if not token:
                return sorted(out)

    def delete(self, key: str) -> None:
        with suppress(NotFound):
            self._api.request("DELETE", f"/storage/v1/b/{self.bucket}/o/{quote(key, safe='')}")


def open_store(url: str) -> Store:
    if url.startswith("gs://"):
        return GCSStore(url.removeprefix("gs://").strip("/"))
    if url.startswith("file://"):
        return FileStore(url.removeprefix("file://"))
    raise ValueError(f"unsupported store url {url!r} (use file://<dir> or gs://<bucket>)")
