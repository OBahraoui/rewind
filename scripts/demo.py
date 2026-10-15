#!/usr/bin/env python3
"""Open a demo scenario as a GitLab issue, labelled for the hands-off loop.

  python scripts/demo.py ship      # bulk discount: ships to production
  python scripts/demo.py rewind    # launch promo: breaks production, gets rewound
  python scripts/demo.py block     # email receipts: blocked by the reversibility gate
  python scripts/demo.py ship --no-label   # create the issue, label it yourself on camera
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rewind.config import Settings
from rewind.gitlab import GitLabClient

SCENARIOS = {"ship": "01-bulk-discount.md", "rewind": "02-launch-promo.md", "block": "03-email-receipts.md"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=SCENARIOS)
    p.add_argument("--no-label", action="store_true")
    args = p.parse_args()
    s = Settings.from_env()
    s.require("gitlab_token", "project_id")
    text = (Path(__file__).parents[1] / "demo" / "issues" / SCENARIOS[args.scenario]).read_text()
    title, _, body = text.partition("\n")
    labels = [] if args.no_label else [s.trigger_label]
    issue = GitLabClient(s.gitlab_url, s.gitlab_token, s.project_id).create_issue(
        title.lstrip("# ").strip(), body.strip(), labels)
    print(f"#{issue['iid']} {issue['title']}\n{issue['web_url']}")


if __name__ == "__main__":
    main()
