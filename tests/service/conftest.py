from __future__ import annotations

import pytest

from rewind.config import FlowRef, Settings
from rewind.flows import Flows
from service.orchestrator import Orchestrator
from service.repo import RunRepository
from tests.service.helpers import PRICING


@pytest.fixture
def settings() -> Settings:
    refs = {s: FlowRef.parse(v) for s, v in {"plan": "catalog:7", "develop": "definition:developer/v1",
                                             "review": "definition:code_review/v1",
                                             "fix": "definition:fix_pipeline/v1"}.items()}
    return Settings(project_id="42", hash_key="k" * 32, flows=refs, max_fix_attempts=1,
                    public_url="https://rewind.example")


@pytest.fixture
def orch(engine, settings, gl) -> Orchestrator:
    gl.commit_files("main", {"app/pricing.py": PRICING})
    repo = RunRepository(engine.store, "42")
    return Orchestrator(settings, engine, repo, Flows(gl, "42", settings.flows, engine.ctx.policy))
