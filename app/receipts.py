"""Order receipts.

A receipt is queued as an outbox message addressed to the receipts service. Nothing is
delivered from here: a separate, approved dispatcher drains the outbox, so this code stays undoable.
Queueing never raises: a receipt problem must not fail the order.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("app.receipts")

RECEIPTS_URL = "https://receipts.example.com/v1/send"

_lock = threading.Lock()
_outbox: list[dict] = []


def build_message(order: dict) -> dict:
    return {"method": "POST", "url": RECEIPTS_URL, "body": {"order": order}}


def enqueue_receipt(order: dict) -> bool:
    """Queue a receipt for the order. Returns False (and logs) on any failure."""
    try:
        message = build_message(order)
        with _lock:
            _outbox.append(message)
        return True
    except Exception:
        log.exception("could not queue receipt for order %s", order.get("id"))
        return False


def pending() -> list[dict]:
    with _lock:
        return list(_outbox)


def clear() -> None:
    with _lock:
        _outbox.clear()
