#!/usr/bin/env bash
# Deploy the demo app image to Cloud Run for one GitLab environment.
# Every deploy is a new immutable revision; Rewind undoes a deploy by moving traffic back.
# Writes deploy.env (DEPLOY_URL, DEPLOY_REVISION) as a dotenv artifact for the environment page.
set -euo pipefail
ENVIRONMENT="$1"
case "$ENVIRONMENT" in
  staging) SERVICE="${CLOUD_RUN_SERVICE_STAGING:-rewind-demo-staging}" ;;
  production) SERVICE="${CLOUD_RUN_SERVICE_PRODUCTION:-rewind-demo-production}" ;;
  *) echo "unknown environment $ENVIRONMENT" >&2; exit 2 ;;
esac
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GCP_ARTIFACT_REPO:-rewind}/app:${CI_COMMIT_SHA}"
SUFFIX="$(echo "${CI_COMMIT_SHORT_SHA}-j${CI_JOB_ID}" | tr '[:upper:]' '[:lower:]')"
ENV_VARS="APP_VERSION=${CI_COMMIT_SHORT_SHA},UNLEASH_URL=${CI_API_V4_URL}/feature_flags/unleash/${CI_PROJECT_ID},UNLEASH_APP_NAME=${ENVIRONMENT},UNLEASH_INSTANCE_ID=${UNLEASH_INSTANCE_ID:-},FLAGS_REFRESH_SECONDS=5,REWIND_URL=${REWIND_PUBLIC_URL:-}"

gcloud run deploy "$SERVICE" \
  --image "$IMAGE" --region "$GCP_REGION" --project "$GCP_PROJECT_ID" \
  --revision-suffix "$SUFFIX" --set-env-vars "$ENV_VARS" \
  --allow-unauthenticated --cpu 1 --memory 256Mi --min-instances 0 --max-instances 3 \
  --labels "managed-by=rewind,environment=${ENVIRONMENT}" \
  ${CLOUD_RUN_APP_SERVICE_ACCOUNT:+--service-account "$CLOUD_RUN_APP_SERVICE_ACCOUNT"} --quiet

# If Rewind pinned traffic to an older revision, a new deploy takes over again.
gcloud run services update-traffic "$SERVICE" --to-latest --region "$GCP_REGION" --project "$GCP_PROJECT_ID" --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$GCP_REGION" --project "$GCP_PROJECT_ID" --format 'value(status.url)')"
REVISION="$(gcloud run services describe "$SERVICE" --region "$GCP_REGION" --project "$GCP_PROJECT_ID" --format 'value(status.latestReadyRevisionName)')"
echo "DEPLOY_URL=${URL}" > deploy.env
echo "DEPLOY_REVISION=${REVISION}" >> deploy.env
echo "Deployed ${REVISION} → ${URL}"
