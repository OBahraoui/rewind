"""Goals handed to Duo flows.

Agents get the general way to ship reversibly (feature flags, tests, out-of-bounds paths). The
specific checks live in Rewind's gate and in production monitoring, not in the prompt: Rewind
exists for the mistakes nobody anticipated, so it must not depend on agents knowing every rule."""

from __future__ import annotations

RULES = """How to ship here (see AGENTS.md):
- Ship user-visible behaviour behind a GitLab feature flag. Pricing changes go in new_pricing_total() in
  app/pricing.py, which runs only when flag `new_pricing` is on; turn the flag on per environment in
  config/flags.yaml (e.g. new_pricing: production: true). The pipeline applies it.
- Add or update tests in app/tests for every behaviour change.
- Do not edit rewind/, service/, flows/, agents/, ci/, infra/, .gitlab/, .gitlab-ci.yml, AGENTS.md or CLAUDE.md.
- Never push to the default branch directly: only a merge request reaches it.
Rewind checks your merge request before merge and can undo everything it lets through."""

PLAN_MARKER = "🧭 Plan"


def plan(iid: int, title: str, checkpoint: str) -> str:
    return f"""Plan the work for issue #{iid}: {title}

Read the issue and the reversibility policy in rewind/policy.yaml. Post exactly one comment on issue #{iid}
with: (1) a short implementation plan listing the files to change, (2) for each planned change, its
reversibility class from the policy (reversible / compensatable / irreversible) and why, and (3) how any
user-visible change will ship behind a feature flag. Do not change code, create branches or open merge
requests. Rewind checkpoint: {checkpoint}.

{RULES}"""


def develop(iid: int, title: str, description: str, default_branch: str, checkpoint: str,
            plan_first: bool = True) -> str:
    plan = (f"""First, before writing code, post exactly one comment on issue #{iid} that starts with
"### {PLAN_MARKER}": the files you will change, how the change ships (behind which feature flag, if any),
and how you will test it. Then implement it.

""" if plan_first else "")
    return f"""Implement issue #{iid}: {title}

{description.strip()}

{plan}Open exactly one merge request targeting {default_branch} with "Closes #{iid}" in its description.
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


def review_fix(mr_iid: int, branch: str, comments: list[str]) -> str:
    said = "\n\n".join("> " + c.replace("\n", "\n> ") for c in comments) or "> (see the merge request's review)"
    return f"""GitLab Duo code review requested changes on merge request !{mr_iid}. What the reviewer said:

{said}

Address every point on branch {branch} (push to the same branch, do not open a new merge request). Change the
code and the tests; never weaken a test to make it pass. The change will be gated, tested and reviewed again.

{RULES}"""


def fix(mr_iid: int, branch: str, pipeline_url: str) -> str:
    return f"""The pipeline for merge request !{mr_iid} failed: {pipeline_url}
Find the failing jobs, fix the cause on branch {branch} (push to the same branch), and keep tests meaningful:
never delete or skip a test to make it pass.

{RULES}"""


def review(mr_iid: int) -> str:
    """The code_review flow rejects any goal that isn't the merge request IID (or URL)."""
    return str(mr_iid)


def security_fix(mr_iid: int, branch: str, findings: list[str]) -> str:
    found = "\n".join(f"- {f}" for f in findings)
    return f"""Security scanning found issues introduced by merge request !{mr_iid}:
{found}

Fix each one on branch {branch} (push to the same branch, do not open a new merge request). Fix the code,
don't suppress the finding, and add a test where it makes sense.

{RULES}"""
