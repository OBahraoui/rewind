# Common tasks. `make help` lists them.
.DEFAULT_GOAL := help
PY := .venv/bin/python
SHELL := /bin/bash

help: ## List tasks
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install everything
	python3 -m venv .venv && .venv/bin/pip install -q -e ".[service,dev]"

check: ## Lint and run every test
	.venv/bin/ruff check . && .venv/bin/pytest -q -p no:logging

property: ## Property test with 500 random agent-action sequences
	REWIND_PROPERTY_EXAMPLES=500 .venv/bin/pytest -q -p no:logging tests/rewind/test_property.py

integration: ## Rewind real state on the real project (flag, label, Cloud Run traffic)
	CLOUDSDK_ACTIVE_CONFIG_NAME=rewind .venv/bin/pytest -m integration tests/integration

simulate: ## Run all scenarios offline and serve the dashboard on :8080
	$(PY) scripts/simulate.py --realistic

screenshots: ## Screenshot every page (URL=https://... for the live service) into docs/screenshots
	$(PY) scripts/screenshots.py --out docs/screenshots $(if $(URL),--url $(URL),)

readme-images: ## Refresh the README images from the live service (the newest real rewind and the overview)
	set -a; . ./.env; set +a; $(PY) scripts/screenshots.py --readme --url "$${URL:-$$REWIND_PUBLIC_URL}"

design-check: ## Run impeccable's design detector over every rendered page and state
	$(PY) scripts/design_check.py

doctor: ## Check configuration and access
	.venv/bin/rewind doctor

demo-rewind: ## Open the flash-sale issue and follow the run (production breaks, Rewind restores it)
	$(PY) scripts/demo.py rewind --watch

demo-review: ## Open the launch-promo issue and follow the run (Duo review requests changes, the agent fixes them)
	$(PY) scripts/demo.py review --watch

demo-ship: ## Open the bulk-discount issue and follow the run (ships to production)
	$(PY) scripts/demo.py ship --watch

demo-block: ## Open the email-receipt issue and follow the run (the gate blocks it)
	$(PY) scripts/demo.py block --watch

rerun: ## Start a fresh run for an issue and follow it: make rerun ISSUE=5
	.venv/bin/rewind rerun $(ISSUE) --watch

stop-all: ## Emergency brake: stop every run in progress (works even if the service is down)
	CLOUDSDK_ACTIVE_CONFIG_NAME=rewind .venv/bin/rewind stop --all

watch: ## Follow a run: make watch RUN=<run id or issue number>
	.venv/bin/rewind watch $(RUN)

ship: ## Check, sync with GitLab, push and redeploy (refuses while a run is in progress)
	./scripts/ship.sh

release: ## Tag and publish the submitted version (after the video link is in the README): make release VERSION=1.0.0
	set -a; . ./.env; set +a; $(PY) scripts/release.py --version $${VERSION:-1.0.0}

deploy: ## Redeploy the Rewind service only
	CLOUDSDK_ACTIVE_CONFIG_NAME=rewind ./infra/deploy-rewind.sh

.PHONY: help setup check property integration simulate screenshots readme-images design-check doctor demo-rewind demo-review demo-ship demo-block rerun stop-all watch ship release deploy
