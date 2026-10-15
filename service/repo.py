"""Run persistence with a summary index, so the dashboard needs one read."""

from __future__ import annotations

import threading

from rewind.models import ChangeSet
from rewind.runs import Run
from rewind.store import Store


class RunRepository:
    def __init__(self, store: Store, project_id: str):
        self.store = store
        self.prefix = f"projects/{project_id.replace('/', '__')}"
        self._lock = threading.Lock()

    def _key(self, run_id: str) -> str:
        return f"{self.prefix}/runs/{run_id}.json"

    def save(self, run: Run) -> None:
        self.store.put(self._key(run.id), run.model_dump(mode="json"))
        with self._lock:
            index = self.store.get(f"{self.prefix}/index.json") or {"runs": {}}
            index["runs"][run.id] = {
                "id": run.id, "issue_iid": run.issue_iid, "issue_title": run.issue_title, "state": run.state.value,
                "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat(),
                "checkpoint_id": run.checkpoint_id, "mr_iid": run.mr_iid, "rewind_id": run.rewind_id,
                "merge_sha": run.merge_sha, "counts": run.change_counts,
                "gate": run.gate.verdict if run.gate else None,
            }
            self.store.put(f"{self.prefix}/index.json", index)

    def get(self, run_id: str) -> Run | None:
        data = self.store.get(self._key(run_id))
        return Run.model_validate(data) if data else None

    def summaries(self) -> list[dict]:
        index = self.store.get(f"{self.prefix}/index.json") or {"runs": {}}
        return sorted(index["runs"].values(), key=lambda r: r["created_at"], reverse=True)

    def active(self) -> list[Run]:
        terminal = {"shipped", "rewound", "blocked", "failed"}
        runs = [self.get(s["id"]) for s in self.summaries() if s["state"] not in terminal]
        return sorted([r for r in runs if r], key=lambda r: r.created_at)

    def latest_for_issue(self, iid: int) -> Run | None:
        for s in self.summaries():
            if s["issue_iid"] == iid:
                return self.get(s["id"])
        return None

    def find(self, *, mr_iid: int | None = None, sha: str | None = None) -> list[Run]:
        out = []
        for s in self.summaries():
            if (mr_iid is not None and s.get("mr_iid") == mr_iid) or (sha and s.get("merge_sha") == sha):
                run = self.get(s["id"])
                if run:
                    out.append(run)
        return out

    def save_change_set(self, run_id: str, cs: ChangeSet) -> None:
        self.store.put(f"{self.prefix}/changesets/{run_id}.json", cs.model_dump(mode="json"))

    def change_set(self, run_id: str) -> ChangeSet | None:
        data = self.store.get(f"{self.prefix}/changesets/{run_id}.json")
        return ChangeSet.model_validate(data) if data else None
