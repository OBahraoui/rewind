"""Goals handed to Duo flows. The reversibility rules are stated up front so agents
take the undoable path first; the gate enforces them either way."""

from __future__ import annotations

RULES = """Rules (Rewind's reversibility gate checks these before anything merges):
- Ship user-visible behaviour behind a GitLab feature flag. In app/pricing.py never change legacy_total();
  put new pricing logic in new_pricing_total(), which runs only when flag `new_pricing` is on, and turn the
  flag on for production in config/flags.yaml (new_pricing: production: true). The pipeline applies it.
- Add or update tests in app/tests for every behaviour change.
- Do not edit rewind/, service/, flows/, agents/, ci/, infra/, .gitlab/, .gitlab-ci.yml, AGENTS.md or CLAUDE.md.
- Do not add code that calls external services or sends messages, and no migration without downgrade().
Everything you change within these rules can be undone by Rewind in one command."""


def plan(iid: int, title: str, checkpoint: str) -> str:
    return f"""Plan the work for issue #{iid}: {title}

Read the issue and the reversibility policy in rewind/policy.yaml. Post exactly one comment on issue #{iid}
with: (1) a short implementation plan listing the files to change, (2) for each planned change, its
reversibility class from the policy (reversible / compensatable / irreversible) and why, and (3) how any
user-visible change will ship behind a feature flag. Do not change code, create branches or open merge
requests. Rewind checkpoint: {checkpoint}.

{RULES}"""


def develop(iid: int, title: str, description: str, default_branch: str, checkpoint: str) -> str:
    return f"""Implement issue #{iid}: {title}

{description.strip()}

Open exactly one merge request targeting {default_branch} with "Closes #{iid}" in its description.
Rewind checkpoint: {checkpoint}.

{RULES}"""


def reroute(mr_iid: int, branch: str, findings: list[str], instructions: list[str]) -> str:
    found = "\n".join(f"- {f}" for f in findings)
    how = "\n".join(f"- {i}" for i in instructions)
    return f"""Rewind's reversibility gate rerouted merge request !{mr_iid}. Findings:
{found}

Rework the change on branch {branch} (push to the same branch, do not open a new merge request):
{how}

{RULES}"""


def fix(mr_iid: int, branch: str, pipeline_url: str) -> str:
    return f"""The pipeline for merge request !{mr_iid} failed: {pipeline_url}
Find the failing jobs, fix the cause on branch {branch} (push to the same branch), and keep tests meaningful:
never delete or skip a test to make it pass.

{RULES}"""


def review(mr_iid: int) -> str:
    return f"Review merge request !{mr_iid} for correctness, security and test coverage."
