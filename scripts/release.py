#!/usr/bin/env python3
"""Tag and publish the submitted version as a GitLab release, after checking it's ready to be judged.

  make release                    # rewind-1.0.0 on the commit GitLab has on main
  make release VERSION=1.0.1      # a later fix

Refuses if the working tree isn't clean or pushed, a run is in progress, the README has no demo video
link yet, or the checks fail. The demo app's own pipeline tags app releases v1.0.N, so Rewind's tag is
prefixed: rewind-<version>.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    raise SystemExit(1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--storefront", default="https://rewind-demo-production-twegh4u3wa-ew.a.run.app")
    args = p.parse_args()
    tag = f"rewind-{args.version}"

    from rewind.config import Settings
    from rewind.gitlab import GitLabClient

    s = Settings.from_env()
    s.require("gitlab_token", "project_id")
    gl = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id)

    print("→ checking the repository")
    if sh("git", "status", "--porcelain"):
        fail("uncommitted changes: commit and `make ship` first")
    sh("git", "fetch", "--quiet", "origin")
    head, remote = sh("git", "rev-parse", "HEAD"), sh("git", "rev-parse", "origin/main")
    if head != remote:
        fail("this commit isn't what GitLab has on main: `make ship` first")
    if gl.get_release(tag):
        fail(f"release {tag} already exists")

    print("→ checking for runs in progress")
    if s.public_url:
        runs = json.load(urllib.request.urlopen(f"{s.public_url}/api/runs", timeout=20))
        active = [r["id"] for r in runs if r["state"] not in {"shipped", "rewound", "blocked", "failed"}]
        if active:
            fail(f"a run is in progress ({', '.join(active)}): a release now would land in its change set")

    print("→ checking the README is ready to be judged")
    readme = (ROOT / "README.md").read_text()
    video = re.search(r"https://(?:www\.)?(?:youtu\.be|youtube\.com)/[^\s\"')]+", readme)
    if not video or "DEMO-VIDEO:" in readme:
        fail("add the demo video link to the README (replace the DEMO-VIDEO comment at the top)")
    for img in ("docs/readme/rewound-run.png", "docs/readme/architecture.png"):
        if not (ROOT / img).exists():
            fail(f"{img} is missing (`make readme-images`)")

    print("→ lint and tests")
    subprocess.run([str(ROOT / ".venv/bin/ruff"), "check", "."], cwd=ROOT, check=True)
    subprocess.run([str(ROOT / ".venv/bin/pytest"), "-q", "-p", "no:logging"], cwd=ROOT, check=True)

    notes = (ROOT / "docs" / "release-notes.md").read_text().replace("# Rewind 1.0.0", f"# Rewind {args.version}", 1)
    notes = notes.format(dashboard=s.public_url or "(see README)", storefront=args.storefront, video=video.group(0))
    print(f"→ tagging {head[:8]} as {tag} and publishing the release")
    gl.create_tag(tag, head, message=f"Rewind {args.version}: hackathon submission")
    release = gl.create_release(tag, f"Rewind {args.version}", notes)
    sh("git", "fetch", "--quiet", "--tags", "origin")
    print(f"✓ {release.get('_links', {}).get('self') or tag}")


if __name__ == "__main__":
    main()
