#!/usr/bin/env bash
# One-time Google Cloud setup for Rewind. Idempotent: safe to re-run.
#
# Creates:
#   - Artifact Registry Docker repo (Cloud Run pulls images from it)
#   - GCS bucket for Rewind's snapshots, checkpoints, runs and reports (versioned)
#   - deploy service account for GitLab CI, reached keylessly through Workload
#     Identity Federation, trusted only for this GitLab project
#   - runtime service accounts for the demo app and for Rewind (least privilege)
#   - Secret Manager secrets for Rewind's tokens
#
# Usage: GCP_PROJECT_ID=... GITLAB_PROJECT_PATH=group/sub/project ./infra/gcp-setup.sh
set -euo pipefail

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${GITLAB_PROJECT_PATH:?set GITLAB_PROJECT_PATH (e.g. my-group/rewind)}"
REGION="${GCP_REGION:-europe-west1}"
REPO="${GCP_ARTIFACT_REPO:-rewind}"
BUCKET="${GCS_BUCKET:-${GCP_PROJECT_ID}-rewind}"
GITLAB_URL="${GITLAB_URL:-https://gitlab.com}"
POOL=gitlab
PROVIDER=gitlab-com

gc() { gcloud --project "$GCP_PROJECT_ID" --quiet "$@"; }
exists() { "$@" >/dev/null 2>&1; }

echo "→ enabling APIs"
gc services enable run.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com cloudresourcemanager.googleapis.com
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)')"

echo "→ Artifact Registry repo $REPO"
exists gc artifacts repositories describe "$REPO" --location "$REGION" ||
  gc artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --description "Rewind demo images"

echo "→ GCS bucket gs://$BUCKET"
exists gcloud storage buckets describe "gs://$BUCKET" ||
  gc storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access
gc storage buckets update "gs://$BUCKET" --versioning >/dev/null

sa() {  # name, description
  exists gc iam service-accounts describe "$1@$GCP_PROJECT_ID.iam.gserviceaccount.com" ||
    gc iam service-accounts create "$1" --display-name "$2"
  echo "$1@$GCP_PROJECT_ID.iam.gserviceaccount.com"
}
DEPLOY_SA="$(sa gitlab-deployer 'GitLab CI deployer (WIF)')"
APP_SA="$(sa rewind-demo-app 'Rewind demo app runtime')"
REWIND_SA="$(sa rewind-service 'Rewind service runtime')"

echo "→ IAM: deployer can deploy the demo app and push images"
for role in roles/run.admin roles/artifactregistry.writer; do
  gc projects add-iam-policy-binding "$GCP_PROJECT_ID" --member "serviceAccount:$DEPLOY_SA" --role "$role" \
    --condition=None >/dev/null
done
gc iam service-accounts add-iam-policy-binding "$APP_SA" --member "serviceAccount:$DEPLOY_SA" \
  --role roles/iam.serviceAccountUser >/dev/null
# gcloud run deploy uses the default compute SA unless told otherwise; allow both.
gc iam service-accounts add-iam-policy-binding "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/iam.serviceAccountUser >/dev/null || true

echo "→ IAM: Rewind can move Cloud Run traffic and use its bucket, nothing else"
gc projects add-iam-policy-binding "$GCP_PROJECT_ID" --member "serviceAccount:$REWIND_SA" \
  --role roles/run.developer --condition=None >/dev/null
for runtime in "$APP_SA" "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"; do
  gc iam service-accounts add-iam-policy-binding "$runtime" --member "serviceAccount:$REWIND_SA" \
    --role roles/iam.serviceAccountUser >/dev/null || true
done
gc storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$REWIND_SA" \
  --role roles/storage.objectAdmin >/dev/null

echo "→ Workload Identity Federation for $GITLAB_URL project $GITLAB_PROJECT_PATH"
exists gc iam workload-identity-pools describe "$POOL" --location global ||
  gc iam workload-identity-pools create "$POOL" --location global --display-name "GitLab"
WIF_MAPPING="google.subject=assertion.sub,attribute.project_path=assertion.project_path,attribute.ref=assertion.ref,attribute.ref_protected=assertion.ref_protected"
WIF_CONDITION="assertion.project_path == '${GITLAB_PROJECT_PATH}'"
if exists gc iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL"; then
  # Re-running with a different GITLAB_PROJECT_PATH must re-point the trust, not keep the old one.
  gc iam workload-identity-pools providers update-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --attribute-mapping "$WIF_MAPPING" --attribute-condition "$WIF_CONDITION"
else
  gc iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --issuer-uri "$GITLAB_URL" \
    --attribute-mapping "$WIF_MAPPING" --attribute-condition "$WIF_CONDITION"
fi
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
gc iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.project_path/${GITLAB_PROJECT_PATH}" >/dev/null

echo "→ Secret Manager secrets (values are added by infra/deploy-rewind.sh)"
for s in gitlab-token rewind-hash-key rewind-api-token gitlab-webhook-secret; do
  exists gc secrets describe "$s" || gc secrets create "$s" --replication-policy automatic
  gc secrets add-iam-policy-binding "$s" --member "serviceAccount:$REWIND_SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

cat <<EOF

✓ Google Cloud is ready. Add these to .env, then run: rewind bootstrap

GCP_PROJECT_ID=${GCP_PROJECT_ID}
GCP_REGION=${REGION}
GCP_ARTIFACT_REPO=${REPO}
GCS_BUCKET=${BUCKET}
GCP_WIF_PROVIDER=${WIF_PROVIDER}
GCP_DEPLOY_SERVICE_ACCOUNT=${DEPLOY_SA}
CLOUD_RUN_APP_SERVICE_ACCOUNT=${APP_SA}
REWIND_SERVICE_ACCOUNT=${REWIND_SA}
EOF
