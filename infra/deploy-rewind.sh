#!/usr/bin/env bash
# Build and deploy the Rewind service to Cloud Run.
# Deliberately NOT done by the project pipeline: the guard is never deployed by the
# pipeline it guards, so an agent's change can't alter Rewind itself.
#
# Reads .env. Secrets go to Secret Manager; the service reads them at startup.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
: "${GCP_PROJECT_ID:?}" "${GITLAB_TOKEN:?}" "${REWIND_HASH_KEY:?}" "${REWIND_API_TOKEN:?}" "${GITLAB_WEBHOOK_SECRET:?}"
REGION="${GCP_REGION:-europe-west1}"
SERVICE="${CLOUD_RUN_SERVICE_REWIND:-rewind}"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GCP_ARTIFACT_REPO:-rewind}/rewind:${TAG}"
RUNTIME_SA="${REWIND_SERVICE_ACCOUNT:-rewind-service@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
gc() { gcloud --project "$GCP_PROJECT_ID" --quiet "$@"; }

echo "→ secrets"
put_secret() { printf '%s' "$2" | gc secrets versions add "$1" --data-file=- >/dev/null; }
put_secret gitlab-token "$GITLAB_TOKEN"
put_secret rewind-hash-key "$REWIND_HASH_KEY"
put_secret rewind-api-token "$REWIND_API_TOKEN"
put_secret gitlab-webhook-secret "$GITLAB_WEBHOOK_SECRET"

echo "→ building $IMAGE with Cloud Build (linux/amd64)"
CFG="$(mktemp)"
cat > "$CFG" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build, -f, service/Dockerfile, -t, "$IMAGE", .]
images: ["$IMAGE"]
YAML
gc builds submit --config "$CFG" .
rm -f "$CFG"

echo "→ deploying $SERVICE"
ENV_VARS="GITLAB_URL=${GITLAB_URL:-https://gitlab.com},GITLAB_PROJECT_ID=${GITLAB_PROJECT_ID},GITLAB_NAMESPACE_ID=${GITLAB_NAMESPACE_ID:-}"
ENV_VARS+=",REWIND_STORE=gs://${GCS_BUCKET:-${GCP_PROJECT_ID}-rewind},REWIND_TRIGGER_LABEL=${REWIND_TRIGGER_LABEL:-rewind:auto}"
ENV_VARS+=",REWIND_FLOW_PLAN=${REWIND_FLOW_PLAN:-none},REWIND_FLOW_DEVELOP=${REWIND_FLOW_DEVELOP:-definition:developer/v1}"
ENV_VARS+=",REWIND_FLOW_REVIEW=${REWIND_FLOW_REVIEW:-definition:code_review/v1},REWIND_FLOW_FIX=${REWIND_FLOW_FIX:-definition:fix_pipeline/v1}"
ENV_VARS+=",REWIND_MAX_FIX_ATTEMPTS=${REWIND_MAX_FIX_ATTEMPTS:-2},REWIND_TICK_SECONDS=${REWIND_TICK_SECONDS:-15}"
ENV_VARS+=",REWIND_MAX_FLOWS_PER_DAY=${REWIND_MAX_FLOWS_PER_DAY:-25}"
ENV_VARS+=",GCP_PROJECT_ID=${GCP_PROJECT_ID},GCP_REGION=${REGION}"
ENV_VARS+=",CLOUD_RUN_SERVICE_STAGING=${CLOUD_RUN_SERVICE_STAGING:-rewind-demo-staging},CLOUD_RUN_SERVICE_PRODUCTION=${CLOUD_RUN_SERVICE_PRODUCTION:-rewind-demo-production}"
[ -n "${REWIND_PUBLIC_URL:-}" ] && ENV_VARS+=",REWIND_PUBLIC_URL=${REWIND_PUBLIC_URL}"

# One always-on instance with CPU outside requests: the orchestrator's tick keeps runs moving.
gc run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --service-account "$RUNTIME_SA" \
  --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 1 --memory 512Mi --timeout 300 \
  --allow-unauthenticated --set-env-vars "$ENV_VARS" \
  --set-secrets "GITLAB_TOKEN=gitlab-token:latest,REWIND_HASH_KEY=rewind-hash-key:latest,REWIND_API_TOKEN=rewind-api-token:latest,GITLAB_WEBHOOK_SECRET=gitlab-webhook-secret:latest"

URL="$(gc run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
if [ -z "${REWIND_PUBLIC_URL:-}" ]; then
  gc run services update "$SERVICE" --region "$REGION" --update-env-vars "REWIND_PUBLIC_URL=${URL}" >/dev/null
  echo "REWIND_PUBLIC_URL=${URL}   ← add this to .env, then run: rewind bootstrap (registers the webhook)"
fi
echo "✓ Rewind is live at ${URL}"
