# Devpost submission draft

**Name:** Rewind

**Tagline:** Ctrl+Z for autonomous software delivery: let agents do anything they can undo, and stop them only for what they can't.

**Path / tier:** Path A (Start Fresh) · Hands-off · also entering Most Stages Covered

## Inspiration
Agents can now take an issue to production on GitLab. Teams hold back autonomy for one reason:
when an agent gets it wrong, there's no undo for what it did. The merge, the flag it switched
on, the release it cut, the revision it deployed: each has to be found and reverted by hand,
usually at 2 a.m. GitLab can roll back a *deployment*, but nothing undoes an agent's *whole change set*.

## What it does
Rewind sits outside the agents and makes their work undoable as one unit.
- **Checkpoint**: before Duo flows act, it snapshots everything they can touch: code, MRs,
  issues, labels, feature flags, CI/CD variables (as HMAC fingerprints, never values), tags,
  releases, and the Cloud Run revision serving each environment.
- **Gate by reversibility**: before merge, every change is classified as reversible,
  compensatable or irreversible. Undoable work ships hands-off. Irreversible work (calls to
  external systems, migrations with no downgrade, agents editing their own guardrails) is
  blocked, or the agent is rerouted to ship it behind a feature flag.
- **Rewind**: when production monitoring fails, Rewind undoes the change set in mitigation
  order. Traffic and flags come back in seconds, then the code, then the paper trail. It then
  re-snapshots and verifies **zero drift**. Every rewind can itself be undone.
- **Ledger**: one note on the issue, edited live: lifecycle evidence, links to every Duo agent
  session, the gate verdict, every change by class with the session that made it, every inverse and
  its outcome. A GitLab incident opens when a rewind starts and Rewind resolves it itself.
- **Humans only where it matters**: anyone on the project can undo a run with a `/rewind` comment.
  Accepting something irreversible needs Rewind's authenticated API, because agents comment with your
  identity. The approval is named, reasoned, and bound to one commit.
- **Guards**: agents fix the security findings they introduced; an agent commit that skips the gate
  and reaches `main` is rewound; a daily credit guard caps flows.
- **Reusable**: the gate ships as a GitLab CI/CD component any project can include with one line.

## How we built it
- **GitLab Duo Agent Platform**: flows started through the Flows API with least-privilege
  `agent_privileges` per step from policy (`start_flows` is never granted), and any approval stall fails the run. Three custom flows (planner with a reversibility
  table, a tighter developer, a mention-triggered explainer), a custom Duo Chat agent, an MCP
  server so agents can ask Rewind *before* they act, and `agent-config.yml` restricting the
  sandbox network.
- **The post-code lifecycle in GitLab CI/CD**: tests and coverage, container image to the
  GitLab registry, SAST, dependency, secret and container scanning gating production, staging
  then production on Cloud Run, feature flags as config applied per environment, a production
  monitor with business invariants, and a GitLab release only after a healthy monitor. All
  nine stages are covered.
- **Google Cloud**: Cloud Run for the demo app (every deploy is an immutable revision, so the
  deploy inverse is a traffic shift) and for Rewind itself (always-on orchestrator). GCS for
  snapshots and reports. Keyless CI deploys through Workload Identity Federation. Secret Manager.
- **Engineering**: Python 3.12, FastAPI, plain REST/GraphQL. Every inverse is idempotent and
  conflict-safe, and has a round-trip test. A property test runs 500 random agent-action
  sequences and requires every one to rewind to the checkpoint.

## Challenges
Being honest about what can't be undone. A merge can't be un-merged, so Rewind restores the
tree with a new commit and annotates the MR. Variable values are never stored, so changing
one is classified irreversible instead of pretending. Verification compares trees, not SHAs.
Runs are serialized per project so every change set is attributable.

## What's next
A cross-platform undo layer for agents: GitLab first, then GitHub, cloud and SaaS APIs. Inside
GitLab, this is the "agent speculative execution and universal rollback" capability built from outside the platform.

## Links
- GitLab project: https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43319182/showcase
- Live dashboard (Cloud Run): <REWIND_PUBLIC_URL>
- Demo app (production): <url>
- Video: <YouTube url>
