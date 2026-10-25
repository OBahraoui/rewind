from unittest import mock

from app import receipts
from app.flags import flags

ORDER = {"customer_id": "c1", "items": [{"sku": "beans", "quantity": 1}]}


def test_receipt_queued_for_successful_order(client):
    flags.override("order_receipts", True)
    r = client.post("/orders", json=ORDER)
    assert r.status_code == 201
    [msg] = receipts.pending()
    assert msg["method"] == "POST"
    assert msg["url"] == "https://receipts.example.com/v1/send"
    assert msg["body"] == {"order": r.json()}


def test_no_receipt_when_flag_off(client):
    assert client.post("/orders", json=ORDER).status_code == 201
    assert receipts.pending() == []


def test_no_receipt_for_rejected_order(client):
    flags.override("order_receipts", True)
    assert client.post("/orders", json={"items": [{"sku": "nope", "quantity": 1}]}).status_code == 422
    assert receipts.pending() == []


def test_receipt_failure_does_not_fail_order(client):
    flags.override("order_receipts", True)
    with mock.patch.object(receipts, "build_message", side_effect=RuntimeError("boom")):
        r = client.post("/orders", json=ORDER)
    assert r.status_code == 201
    assert client.get(f"/orders/{r.json()['id']}").status_code == 200
    assert receipts.pending() == []
