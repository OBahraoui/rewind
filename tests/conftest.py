from __future__ import annotations

import pytest

from rewind.engine import Rewind
from rewind.policy import Policy
from rewind.resources import Context
from rewind.store import FileStore
from rewind.testing import FakeCloudRun, FakeGitLab

APP_FILES = {
    "README.md": "# shop\n",
    "app/pricing.py": "def legacy_total(items):\n    return sum(items)\n",
    "config/flags.yaml": "new_pricing:\n  staging: true\n  production: false\n",
}


@pytest.fixture
def gl() -> FakeGitLab:
    g = FakeGitLab(files=dict(APP_FILES))
    g.labels["bug"] = {"color": "#d9534f", "description": "Something is broken"}
    g.add_flag("new_pricing", active=False, scopes=("production",))
    g.set_variable("API_URL", "https://api.internal")
    return g


@pytest.fixture
def cr() -> FakeCloudRun:
    return FakeCloudRun()


@pytest.fixture
def policy() -> Policy:
    return Policy.load()


@pytest.fixture
def ctx(gl, cr, policy) -> Context:
    return Context(gitlab=gl, cloudrun=cr, policy=policy, hash_key=b"k" * 32, sleep=lambda _s: None)


@pytest.fixture
def engine(ctx, tmp_path) -> Rewind:
    return Rewind(ctx, FileStore(tmp_path / "store"), "42")
