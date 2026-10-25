import pytest
from fastapi.testclient import TestClient

from app.flags import flags
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def flags_off():
    flags.override("new_pricing", False)
    yield
    flags.override("new_pricing", None)


@pytest.fixture(autouse=True)
def receipts_outbox():
    from app import receipts

    flags.override("order_receipts", False)
    receipts.clear()
    yield
    receipts.clear()
    flags.override("order_receipts", None)
