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

doctor: ## Check configuration and access
	.venv/bin/rewind doctor

demo-rewind: ## Open the promo issue and follow the run (production breaks, Rewind restores it)
	$(PY) scripts/demo.py rewind --watch

demo-ship: ## Open the bulk-discount issue and follow the run (ships to production)
	$(PY) scripts/demo.py ship --watch

demo-block: ## Open the email-receipt issue and follow the run (the gate blocks it)
	$(PY) scripts/demo.py block --watch

watch: ## Follow a run: make watch RUN=<run id or issue number>
	.venv/bin/rewind watch $(RUN)

ship: ## Check, sync with GitLab, push and redeploy (refuses while a run is in progress)
	./scripts/ship.sh

deploy: ## Redeploy the Rewind service only
	CLOUDSDK_ACTIVE_CONFIG_NAME=rewind ./infra/deploy-rewind.sh

.PHONY: help setup check property integration simulate screenshots doctor demo-rewind demo-ship demo-block watch ship deploy
