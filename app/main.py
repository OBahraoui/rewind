"""Orders service: the application Rewind's agents change and deploy."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import pricing
from app.catalog import PRODUCTS
from app.flags import flags
from app.store import orders

app = FastAPI(title="Rewind demo shop", version=os.environ.get("APP_VERSION", "dev"))


class Item(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=100)


class OrderRequest(BaseModel):
    customer_id: str = Field(default="", max_length=64)
    items: list[Item] = Field(min_length=1, max_length=50)


def _price(req: OrderRequest) -> dict:
    lines = []
    for item in req.items:
        product = PRODUCTS.get(item.sku)
        if product is None:
            raise HTTPException(422, f"unknown sku {item.sku!r}")
        lines.append((product["price_cents"], item.quantity))
    total, model = pricing.total(lines, req.customer_id)
    return {
        "customer_id": req.customer_id,
        "items": [i.model_dump() for i in req.items],
        "subtotal_cents": pricing.subtotal(lines),
        "total_cents": total,
        "pricing": model,
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": os.environ.get("APP_VERSION", "dev"),
        "revision": os.environ.get("K_REVISION", "local"),
        "environment": os.environ.get("UNLEASH_APP_NAME", "local"),
        "flags": flags.snapshot(),
        "flags_status": flags.status(),
    }


@app.get("/products")
def products() -> dict:
    return PRODUCTS


@app.post("/orders/quote")
def quote(req: OrderRequest) -> dict:
    """Price an order without placing it (used by synthetic monitoring)."""
    return _price(req)


@app.post("/orders", status_code=201)
def create_order(req: OrderRequest) -> dict:
    return orders.add({**_price(req), "created_at": time.time()})


@app.get("/orders")
def list_orders(limit: int = 100) -> list[dict]:
    return orders.list(limit)


@app.get("/orders/{order_id}")
def get_order(order_id: int) -> dict:
    order = orders.get(order_id)
    if order is None:
        raise HTTPException(404, "order not found")
    return order
