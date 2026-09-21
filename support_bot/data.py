"""Loads the mock backend (mock_data/*.json) into memory.

Stand-in for a real customer/order database. Everything here is read-only
lookups against data that's fixed at process start.
"""

import json
from pathlib import Path

MOCK_DATA_DIR = Path(__file__).resolve().parent.parent / "mock_data"


def _load(filename: str) -> list[dict]:
    with open(MOCK_DATA_DIR / filename) as f:
        return json.load(f)


CUSTOMERS: list[dict] = _load("customers.json")
ORDERS: list[dict] = _load("orders.json")


def find_customer(identifier: str) -> dict | None:
    """Looks up a customer by customer_id or email (case-insensitive)."""
    identifier = identifier.strip().lower()
    for customer in CUSTOMERS:
        if customer["customer_id"].lower() == identifier:
            return customer
        if customer["email"].lower() == identifier:
            return customer
    return None


def find_order(order_id: str) -> dict | None:
    """Looks up an order by order_id."""
    order_id = order_id.strip().lower()
    for order in ORDERS:
        if order["order_id"].lower() == order_id:
            return order
    return None
