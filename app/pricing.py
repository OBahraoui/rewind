"""Order pricing.

legacy_total() is the live default path. Rewind's reversibility gate refuses any
change to it: new behaviour goes in new_pricing_total(), which only runs when the
GitLab feature flag `new_pricing` is on, so a release can always be undone by
switching the flag off.
"""

from __future__ import annotations

from app.flags import flags

Line = tuple[int, int]  # (unit price in cents, quantity)


def subtotal(lines: list[Line]) -> int:
    return sum(price * qty for price, qty in lines)


def legacy_total(lines: list[Line]) -> int:
    return subtotal(lines)


FLASH_SALE_DISCOUNT_CENTS = 1500  # weekend flash sale: $15 off every order, no minimum spend


def new_pricing_total(lines: list[Line]) -> int:
    return max(0, subtotal(lines) - FLASH_SALE_DISCOUNT_CENTS)


def total(lines: list[Line], customer_id: str = "") -> tuple[int, str]:
    """Return (total in cents, pricing model used)."""
    if flags.is_enabled("new_pricing", customer_id):
        return new_pricing_total(lines), "new"
    return legacy_total(lines), "legacy"
