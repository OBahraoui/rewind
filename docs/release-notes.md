# Rewind 1.0.0

Ctrl+Z for autonomous software delivery on GitLab. This is the version submitted to the GitLab
"Life After Code" hackathon (Path A, Hands-off).

**Rewind lets agents do anything it can undo, and stops them only for what it can't.**

## What it does

- **Checkpoint** before Duo flows act: code, branches, tags, releases, merge requests, issues, labels,
  feature flags, CI/CD variables (fingerprints only) and the Cloud Run revision serving each environment.
- **Gate** every merge request before merge, on the exact commit that would ship: each change is reversible,
  compensatable or irreversible. Irreversible changes are blocked or rerouted, with the line that caused it.
- **Hands-off loop**: plan, develop, gate, tests and security scans, Duo code review, merge, release. Failing
  pipelines, introduced security findings and review requests for changes go back to an agent, and every new
  commit is gated again.
- **Rewind** when production regresses: the whole change set is undone in mitigation order (traffic and flags in
  seconds, then code and records), Rewind opens and resolves its own incident, and it verifies zero drift against
  the checkpoint. A rewind can itself be undone.
- **Humans where it matters**: `/rewind` and `/rewind stop` as comments; accepting the irreversible only through the
  authenticated API, bound to one commit and its findings.
- **Dashboard** with the run track, the rewind timeline, the change set with provenance, nine-stage DevSecOps
  evidence and the policy in force; an MCP server; and the gate as a reusable CI/CD component.

## Try it

- Live dashboard: {dashboard}
- Demo storefront: {storefront}
- Demo video: {video}
- Setup from a fresh GitLab and Google Cloud account: see the README.

MIT licensed.
