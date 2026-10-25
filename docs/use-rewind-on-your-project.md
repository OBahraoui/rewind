# Use Rewind on your project

Rewind works on any GitLab project through GitLab's public APIs. This guide covers what your project needs, the setup,
and what to adapt. If you only want the reversibility gate in your pipeline, skip to [Only the gate](#only-the-gate).

## What your project needs

| You need | Why | If you don't have it |
|---|---|---|
| GitLab Premium or Ultimate with the Duo Agent Platform | The hands-off loop runs Duo flows (developer, code review, fix pipeline) | Use [only the gate](#only-the-gate) |
| The Maintainer role on the project | `rewind bootstrap` creates the webhook, labels and CI/CD variables | Ask a Maintainer to run the setup once |
| A pipeline on the default branch that deploys | Rewind watches the pipeline of the commit it merged | Rewind still gates and undoes repository state, but has no release to watch |
| A job in that pipeline that fails when production is wrong | A failed release pipeline is what triggers the rewind | Add one: smoke tests or synthetic checks after deploy ([ours](../ci/monitor.py) checks business invariants) |
| Somewhere to run Rewind's service | Webhooks and the orchestrator need an always-on process | The provided scripts use Cloud Run; it's a FastAPI app with a Dockerfile, so any container host works |
| Optional: Cloud Run for your app | Rewind moves traffic back to the checkpoint revision in seconds | Without it, Rewind undoes flags, code, merge requests and issues, and your pipeline redeploys the restored code |
| Optional: GitLab feature flags | Ship behaviour behind a flag, and Rewind switches it back | Changes still ship and are undone through code |

One Rewind instance serves one project today.

## Set it up

```bash
git clone https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43319182/showcase.git rewind && cd rewind
make setup
cp .env.example .env
.venv/bin/rewind keys >> .env      # REWIND_HASH_KEY, REWIND_API_TOKEN, GITLAB_WEBHOOK_SECRET
# edit .env: GITLAB_TOKEN (scope: api), GITLAB_PROJECT_ID, and the Google Cloud values below
.venv/bin/rewind doctor            # checks the token, your role, Duo access and the APIs

GCP_PROJECT_ID=my-proj GITLAB_PROJECT_PATH=group/project ./infra/gcp-setup.sh   # paste the printed values into .env
./infra/deploy-rewind.sh           # the Rewind service; prints REWIND_PUBLIC_URL for .env
.venv/bin/rewind bootstrap         # webhook, labels, feature flag, CI/CD variables
```

Then label an issue `rewind:auto` and open the dashboard at `REWIND_PUBLIC_URL`.

## The contract with your pipeline

- **The merged commit's pipeline on the default branch is the release.** Rewind merges the exact commit it gated and
  watches that pipeline. Green means shipped. A failed job that isn't `allow_failure` means production regressed, and
  Rewind undoes the run.
- **Name stages so the evidence lands in the right place.** Jobs in stages `verify`, `test`, `package`, `secure`,
  `staging`, `production`, `configure`, `monitor` and `release` appear on the dashboard's DevSecOps lifecycle.
- **Restore commits carry `[rewind]`.** If you deploy to Cloud Run, skip deploys for them, as
  [ours](../.gitlab-ci.yml) does: Rewind already moved traffic back. If you don't, let them deploy: that's how
  production gets the restored code.
- **Keep scanning restore commits**, so the next merge request's security report has a baseline to compare against.

## Adapt the policy

Copy [`docs/starter-policy.yaml`](starter-policy.yaml) into your project, for example as `rewind-policy.yaml`, and set
`REWIND_POLICY_PATH` to it.

- **Keep the classification section.** It covers every kind of state Rewind snapshots and fails closed on anything
  else.
- **Protect your guardrails:** the policy file itself, your pipeline, deploy and infra paths go in
  `gate.protected_paths`.
- **Adapt the gate rules to your stack.** `external-side-effect` lists common Python and JavaScript clients; add yours.
  Exclude test paths, where clients are only mocked.
- **Choose the review policy.** `review.unresolved_when_reversible: ship` lets a fully undoable change ship over review
  concerns the agent couldn't address (recorded, production watched); `human` waits for `rewind approve`.

## Tell your agents

- Add an `AGENTS.md` like [ours](../AGENTS.md): ship behind feature flags, write tests, stay out of the protected
  paths. Agents read the policy in the repository and design around it.
- Commit `.gitlab/duo/mcp.json` pointing at `REWIND_PUBLIC_URL/mcp/`, so agents can call `classify_action` and
  `check_paths` before they act. The dashboard's Connect page shows the exact file.

## Only the gate

The gate is a CI/CD component. It needs no token and no service:

```yaml
include:
  - component: gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43319182/showcase/gate@main
    inputs:
      policy: rewind-policy.yaml   # optional; default is Rewind's built-in policy
      fail_on: block               # or reroute
```

On merge request pipelines, `rewind-gate` fails when a change contains something that can't be undone, with the file,
line and evidence in the test report.

## Limits today

- **One project per Rewind instance.**
- **The hands-off loop needs the Duo Agent Platform.** The gate works anywhere.
- **Cloud Run is the only deploy target with traffic rollback.** Each kind of state is a handler with a snapshot, an
  inverse and a verification ([`rewind/resources/base.py`](../rewind/resources/base.py)), each with a round-trip test
  against the fakes, so another target is a new handler.
- **What can't be undone is gated, not undone:** messages that left the system, destroyed data, secret values.
