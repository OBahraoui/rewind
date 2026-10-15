# Rewind — project context

Rewind is Ctrl+Z for autonomous software delivery on GitLab, built for the GitLab
"Life After Code" hackathon (Path A, Hands-off; also Most Stages Covered).
Deadline 27 Oct 2026 13:00 UTC. After that, the repository must not change until winners are announced (~16 Nov).

Core idea: let agents do anything they can undo; stop them only for what they can't.
Rewind checkpoints project state before Duo flows act, diffs it into a change set,
classifies every change by reversibility, gates the MR before merge, and undoes the
whole change set in mitigation order if production regresses.

## Layout
- `rewind/` engine (Python 3.12): snapshot, diff, policy, gate, inverse ops, verify, ledger, flows client, CLI
  - `resources/` one handler per kind of state (repository, branch, tag, release, MR, issue, label,
    feature flag, CI variable, Cloud Run)
  - `policy.yaml` the reversibility policy (classification, privileges, gate rules)
  - `testing/fakes.py` in-memory GitLab and Cloud Run with real git-tree semantics
- `service/` FastAPI on Cloud Run: webhooks, orchestrator (durable state machine), dashboard, MCP server
- `app/` demo orders service the agents change and deploy
- `ci/` pipeline helpers (monitor, apply_flags, Cloud Run deploy, GCP auth), stdlib where possible
- `flows/` Duo custom flows (Flow Registry v1); `agents/` custom agent definition
- `.gitlab/duo/agent-config.yml`, `AGENTS.md` agent runtime and rules
- `infra/` Google Cloud setup and Rewind service deploy
- `tests/` engine, gate, property, orchestrator, HTTP and CI-script tests

## Rules
- All GitLab access goes through `rewind/gitlab.py` (plain REST/GraphQL over httpx). Tokens come from env, never committed.
- Every inverse is idempotent (checks live state first) and conflict-safe (acts only if the
  live object still matches the plan). Every handler has a round-trip test against the fakes.
- CI/CD variable values are never stored: only a keyed HMAC fingerprint (REWIND_HASH_KEY).
  So value changes and deletions are *irreversible* and gated, not "restored".
- Verification compares trees, not SHAs (a restore is a new commit). Compensated objects
  (merged MRs, closed issues) are checked with handler-specific `is_restored`.
- `client_reference` only works with `callback_hook_id` (feature-flagged), so runs track
  flows by workflow ID in Rewind's own store and poll status via GraphQL.
- Runs are serialized per project so every change set is attributable to one run.
- Rewind's restore commits carry `[rewind]`; CI verifies them but never redeploys.
- The gate runs before merge, on the exact head SHA; merge passes `sha=` so an ungated commit can't slip in.
- Snapshots and ledgers are JSON in GCS (`gs://`) or a local dir (`file://`).
- Prefer small, verified steps. Run `pytest` after each change. Keep README setup steps accurate.
- License: MIT.
