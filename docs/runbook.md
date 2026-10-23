# Runbook: from zero to a recorded demo

Steps in order. Each ends with a check; don't move on until it passes.

## 1. GitLab project and access
1. Work in the project the hackathon provisioned for you (your **Developer + AI** role and the
   500 monthly GitLab Credits live there). If you created a personal project, either move this
   code into the provisioned project or confirm that flows run in the personal one; `rewind spike` will tell you.
2. Create a personal access token with the `api` scope (User settings > Access tokens).
3. Deploy > Feature flags > **Configure**: copy the **Instance ID**.
4. Fill `.env` from `.env.example`. Run `rewind keys >> .env` for the three secrets.

✅ `rewind doctor` is all green for the GitLab section.

## 2. Platform spike (≈15 min, a few credits)
```bash
rewind spike               # no credits: privileges, flow creation, callback support
rewind spike --trigger     # one real developer/v1 flow: status polling, trace, MR
```
Read `docs/spike-results.md`, then:
- Status polling failed? → tell Claude; the GraphQL field names may differ in your instance.
- `code_review/v1` or `fix_pipeline/v1` rejected? → set `REWIND_FLOW_REVIEW=none` / `REWIND_FLOW_FIX=none`.
- Close the spike MR and delete its branch.

✅ A flow ran and opened an MR, and status polling worked.

## 3. Custom flows and agent (UI, ≈10 min)
1. **AI > Flows > New flow**: paste `flows/rewind_planner.yml`. Save, then **Enable** in the project.
   Find its consumer ID (GraphQL explorer: `aiCatalogConfiguredItems(projectId: "gid://gitlab/Project/<id>") { nodes { id item { name } } }`)
   and set `REWIND_FLOW_PLAN=catalog:<n>`, where `<n>` is the numeric suffix of
   `gid://gitlab/AiCatalogItemConsumer/<n>`.
2. Optional: same for `flows/rewind_release.yml` → `REWIND_FLOW_DEVELOP=catalog:<id>`.
3. `flows/rewind_explainer.yml` → enable with a **Mention** trigger.
4. **AI > Agents > New agent**: paste `agents/rewind-explainer.md`.

✅ Mentioning the explainer on an issue gets a reply.

## 4. Google Cloud (≈15 min)
```bash
gcloud auth login && gcloud config set project <id>
GCP_PROJECT_ID=<id> GITLAB_PROJECT_PATH=<group/.../project> ./infra/gcp-setup.sh
# paste the printed values into .env
./infra/deploy-rewind.sh      # prints REWIND_PUBLIC_URL → add to .env
rewind bootstrap              # labels, flag, CI/CD variables, FLAGS_TOKEN, webhook
```
Set a budget alert on the GCP project (Billing > Budgets): a $10 cap is plenty.

✅ `rewind doctor` is all green, and the dashboard opens at `REWIND_PUBLIC_URL`.
Re-run `rewind bootstrap` whenever you upgrade Rewind: it also subscribes the webhook to comments
(for `/rewind`) and creates new labels.

## 5. First pipeline
Push to the default branch. The pipeline deploys the demo app to staging and production.

✅ Environments page shows both deployments, `monitor:production` passed, and a release exists.

## 6. Rehearse (each run costs credits: budget ~3–4 flows per run)
```bash
make demo-rewind   # do this FIRST on a fresh state: new_pricing must be off in production
make demo-ship
make demo-block
```
Each one opens the issue and follows the run live in your terminal until it finishes.
`make rerun ISSUE=5` starts a fresh run on an existing issue; `make stop-all` stops every run in
progress (it works even when the Rewind service is down). To ship
changes to Rewind itself, use `make ship`: it refuses while a run is in progress.
✅ Rewind scenario: ledger shows **zero drift**, production serves the checkpoint revision,
`new_pricing` is off, issue reopened. Ship scenario: release created. Block scenario: `rewind:blocked`.

Don't push to `main` while a run is in progress: Rewind stops the run rather than guess which
changes were the agent's. Try a comment command on a finished run: `/rewind status`, then
`/rewind dry-run`.

Agents are told how to ship (flags, tests) but not the monitor's invariants, so the promo
normally reaches production and breaks it. If an agent caps the discount anyway, the run ships
instead: that's a careful agent, not a Rewind bug. Re-label the issue, or use `/rewind` on camera.

## 7. Record (see docs/demo-script.md), then freeze
After submitting, do not push to the project until winners are announced (~16 Nov). Keep the
Rewind service and both Cloud Run environments running.
