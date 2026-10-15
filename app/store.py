"""Thread-safe in-memory order store (the demo keeps no database)."""

from __future__ import annotations

import itertools
import threading


class OrderStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._orders: dict[int, dict] = {}

    def add(self, order: dict) -> dict:
        with self._lock:
            order = {"id": next(self._ids), **order}
            self._orders[order["id"]] = order
            return order

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._orders.values())[-limit:]

    def get(self, order_id: int) -> dict | None:
        with self._lock:
            return self._orders.get(order_id)


orders = OrderStore()
