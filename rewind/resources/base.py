"""Resource handler contract.

A handler owns one *kind* of state. It knows how to capture it, fetch one live
object, and put one object back. The engine handles ordering, idempotency,
conflict detection and reporting, so each handler stays small.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from rewind.models import Change, comparable
from rewind.policy import Policy


@dataclass
class Context:
    gitlab: Any  # GitLabClient or FakeGitLab
    policy: Policy
    hash_key: bytes
    cloudrun: Any | None = None  # CloudRunClient or FakeCloudRun
    meta: dict[str, Any] = field(default_factory=dict)  # default_branch, web_url, ...
    # Set while rewinding:
    rewind_id: str = ""
    checkpoint_id: str = ""
    checkpoint_sha: str = ""
    restore_mode: str = "mr"  # "mr": restore via a merged MR; "direct": commit to the default branch
    # When set, the code restore reverts only what changed between the checkpoint and this commit (the
    # run's own merge) and keeps every later commit by anyone else.
    code_scope: str = ""
    sleep: Callable[[float], None] = time.sleep
    # Objects Rewind created itself (restore branches and MRs, incidents). Tracked by identity, not by
    # label or name, so an agent can't hide its own changes by dressing them up as Rewind's.
    own: dict[str, set[str]] = field(default_factory=lambda: {"issue": set(), "merge_request": set(),
                                                              "branch": set()})

    def is_own(self, kind: str, key: str | int) -> bool:
        return str(key) in self.own.get(kind, set())

    def register_own(self, kind: str, key: str | int) -> None:
        self.own.setdefault(kind, set()).add(str(key))

    def fingerprint(self, *parts: str) -> str:
        msg = "\x1f".join(parts).encode()
        return hmac.new(self.hash_key, msg, hashlib.sha256).hexdigest()[:32]

    def marker(self, change: Change) -> str:
        """Hidden idempotency key for compensating notes: same checkpoint + change => one note."""
        return f"<!-- rewind:compensation {self.checkpoint_id} {change.id} -->"


def ts(value: str | None) -> str:
    """Normalise GitLab timestamps for comparisons against snapshot times."""
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


class Handler:
    kind: ClassVar[str]

    def enabled(self, ctx: Context) -> bool:
        return True

    def snapshot(self, ctx: Context) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    def fetch(self, ctx: Context, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def summarize(self, change: Change) -> str:
        return f"{self.kind} {change.key} {change.op}"

    def action(self, change: Change) -> str:
        """Human description of the inverse, used in plans and dry runs."""
        raise NotImplementedError

    def priority(self, change: Change) -> int:
        return 50

    def is_restored(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> bool:
        return comparable(live) == comparable(change.before)

    def can_apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> bool:
        """Optimistic concurrency: only act if the object is still as the plan saw it."""
        return comparable(live) == comparable(change.after)

    def apply(self, ctx: Context, change: Change, live: dict[str, Any] | None) -> str:
        raise NotImplementedError


def label_delta(current: list[str], target: list[str]) -> tuple[list[str], list[str]]:
    add = sorted(set(target) - set(current))
    remove = sorted(set(current) - set(target))
    return add, remove
