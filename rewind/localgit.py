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
        # -z: NUL-separated, so paths with spaces, quotes or non-ASCII characters arrive verbatim.
        tokens = self._git("diff", "--raw", "-z", "-M", "--no-color", "--no-abbrev", base, head).split(b"\0")
        diffs, i = [], 0
        while i < len(tokens) and tokens[i].startswith(b":"):
            a_mode, b_mode, _a_sha, _b_sha, status = tokens[i][1:].decode().split(" ")
            two_paths = status[:1] in ("R", "C")
            old = tokens[i + 1].decode("utf-8", errors="surrogateescape")
            new = tokens[i + 2].decode("utf-8", errors="surrogateescape") if two_paths else old
            i += 3 if two_paths else 2
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
