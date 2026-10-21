"""The two repository reads the gate needs, answered by a local git checkout.

This lets the reversibility gate run as a plain CI job (the CI/CD component) with no
API token: GitLab already cloned the repository for the job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class LocalGit:
    def __init__(self, repo: str | Path = "."):
        self.repo = str(repo)

    def _git(self, *args: str) -> bytes:
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, check=True).stdout

    def compare(self, base: str, head: str) -> dict:
        """Same shape as GitLab's compare API, as far as the gate reads it."""
        raw = self._git("diff", "--raw", "-M", "--no-color", "--no-abbrev", base, head).decode()
        diffs = []
        for line in raw.splitlines():
            if not line.startswith(":"):
                continue
            meta, *paths = line.split("\t")
            a_mode, b_mode, _a_sha, _b_sha, status = meta[1:].split(" ")
            old = paths[0]
            new = paths[1] if len(paths) > 1 else paths[0]
            text = self._git("diff", "--no-color", "-M", base, head, "--", *({old, new})).decode(errors="replace")
            diffs.append({
                "old_path": old, "new_path": new, "a_mode": a_mode, "b_mode": b_mode,
                "new_file": status.startswith("A"), "deleted_file": status.startswith("D"),
                "renamed_file": status.startswith("R"), "diff": text,
            })
        return {"diffs": diffs, "commits": [], "compare_timeout": False}

    def get_file_raw(self, path: str, ref: str) -> bytes | None:
        try:
            return self._git("show", f"{ref}:{path}")
        except subprocess.CalledProcessError:
            return None
