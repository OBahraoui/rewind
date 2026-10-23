#!/usr/bin/env bash
# Ship Rewind itself: check, sync with GitLab, push, redeploy the service.
# Refuses while an agent run is in flight: pushing to main mid-run stops that run.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
export CLOUDSDK_ACTIVE_CONFIG_NAME="${CLOUDSDK_ACTIVE_CONFIG_NAME:-rewind}"
PY=.venv/bin/python

echo "→ checking for runs in progress"
if [ -n "${REWIND_PUBLIC_URL:-}" ]; then
  active=$($PY - <<'PYEOF'
import json, os, urllib.request
runs = json.load(urllib.request.urlopen(os.environ["REWIND_PUBLIC_URL"] + "/api/runs", timeout=20))
done = {"shipped", "rewound", "blocked", "failed"}
print(" ".join(f"{r['id']}({r['state']})" for r in runs if r["state"] not in done))
PYEOF
)
  if [ -n "$active" ] && [ "${FORCE:-}" != "1" ]; then
    echo "✗ a run is in progress: $active"
    echo "  Wait for it to finish (rewind watch <run>) or stop it (/rewind stop). FORCE=1 overrides."
    exit 1
  fi
fi

echo "→ lint and tests"
.venv/bin/ruff check .
.venv/bin/pytest -q -p no:logging

echo "→ syncing with GitLab (Rewind and agents commit to main too)"
git pull --rebase --quiet
.venv/bin/pytest -q -p no:logging
git push

if [ "${DEPLOY:-1}" = "1" ]; then
  echo "→ deploying the Rewind service"
  ./infra/deploy-rewind.sh
fi
echo "✓ shipped"
