#!/usr/bin/env bash
# Authenticate gcloud in CI. Preferred: keyless Workload Identity Federation with the
# job's GitLab OIDC token. Fallback: a service account key in GCP_SA_KEY (base64).
set -euo pipefail
if [ -n "${GCP_WIF_PROVIDER:-}" ] && [ -n "${GITLAB_OIDC_TOKEN:-}" ]; then
  printf '%s' "$GITLAB_OIDC_TOKEN" > "$CI_BUILDS_DIR/.oidc_token"
  gcloud iam workload-identity-pools create-cred-config "$GCP_WIF_PROVIDER" \
    --service-account="$GCP_DEPLOY_SERVICE_ACCOUNT" \
    --credential-source-file="$CI_BUILDS_DIR/.oidc_token" \
    --output-file="$CI_BUILDS_DIR/.gcp_cred.json"
  gcloud auth login --cred-file="$CI_BUILDS_DIR/.gcp_cred.json" --quiet
elif [ -n "${GCP_SA_KEY:-}" ]; then
  echo "$GCP_SA_KEY" | base64 -d > "$CI_BUILDS_DIR/.gcp_key.json"
  gcloud auth activate-service-account --key-file="$CI_BUILDS_DIR/.gcp_key.json" --quiet
else
  echo "No Google Cloud credentials: set GCP_WIF_PROVIDER + GCP_DEPLOY_SERVICE_ACCOUNT (preferred) or GCP_SA_KEY" >&2
  exit 1
fi
gcloud config set project "$GCP_PROJECT_ID" --quiet
