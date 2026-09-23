"""The four support tools, plus their registration as an in-process SDK MCP server.

Each tool is implemented as a plain function first (the actual lookup/mutation
logic against mock_data), then wrapped with @tool so it can be handed to the
agent via create_sdk_mcp_server(). Keeping the plain function and the SDK
wrapper separate makes the business logic easy to unit test without spinning
up the SDK.

Descriptions on get_customer and lookup_order are deliberately thin — that's
the Stage 2 "before" state from PROJECT_BRIEF.md, not an oversight.

Stage 4 adds two more error cases on top of the validation/permission ones
from earlier stages: a randomly-simulated transient backend timeout on the
two lookups, and a business-rule error in process_refund for amounts over
policy. See _simulate_transient_failure and the amount check inside
process_refund.
"""

import json
import random
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from support_bot import data

REFUND_AMOUNT_LIMIT = 200
"""Single source of truth for the refund policy limit. hooks.py's
PreToolUse enforcement imports this same constant rather than hardcoding
its own copy, so the two can't drift apart."""

TRANSIENT_ERROR_RATE = 0.15
"""Chance that any single get_customer/lookup_order call simulates a
flaky backend, per PROJECT_BRIEF.md Stage 4. Deliberately not tied to any
specific mock data row — a real timeout can happen on any request."""


TEST_ANNOTATION_FIELDS = {"notes", "note"}
"""Fields in mock_data/*.json that are notes to *us* about what each record
is for (e.g. "straightforward resolve, do not escalate"). They are not
part of what a real backend would return, and leaving them in would hand
the model the expected answer to every test case."""


def _without_test_annotations(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in TEST_ANNOTATION_FIELDS}


def _error(error_category: str, is_retryable: bool, message: str) -> dict[str, Any]:
    """Structured error shape used by every tool (see CLAUDE.md conventions)."""
    return {
        "error": {
            "errorCategory": error_category,
            "isRetryable": is_retryable,
            "message": message,
        }
    }


def _simulate_transient_failure() -> dict[str, Any] | None:
    """Randomly simulates a backend service timeout. Returns a
    transient/retryable error some of the time, None the rest — transient
    is the only category where retrying the exact same call, unchanged,
    is ever the right move.
    """
    if random.random() < TRANSIENT_ERROR_RATE:
        return _error(
            "transient",
            True,
            "The backend service timed out. Please try again.",
        )
    return None


# --- Plain functions (the actual business logic) ---------------------------


def get_customer(email: str, customer_id: str) -> dict[str, Any]:
    if (timeout := _simulate_transient_failure()) is not None:
        return timeout
    customer = data.find_customer(email, customer_id)
    if customer is None:
        return _error(
            "validation",
            False,
            "That email and customer ID don't match the same account on "
            "file — please double-check both.",
        )
    return _without_test_annotations(customer)


def lookup_order(order_id: str, customer_id: str) -> dict[str, Any]:
    if (timeout := _simulate_transient_failure()) is not None:
        return timeout
    order = data.find_order(order_id)
    if order is None:
        return _error(
            "validation",
            False,
            f"No order found with id '{order_id}'.",
        )
    if order["customer_id"] != customer_id:
        return _error(
            "permission",
            False,
            f"Order '{order_id}' does not belong to customer '{customer_id}'.",
        )
    return _without_test_annotations(order)


def process_refund(
    order_id: str, customer_id: str, amount: float, reason: str
) -> dict[str, Any]:
    order = data.find_order(order_id)
    if order is None:
        return _error(
            "validation",
            False,
            f"No order found with id '{order_id}'.",
        )
    if order["customer_id"] != customer_id:
        return _error(
            "permission",
            False,
            f"Order '{order_id}' does not belong to customer '{customer_id}'.",
        )
    # Verification (was get_customer called first?) is still enforced only
    # by the Stage 3 hook, per CLAUDE.md — nothing here checks it. The
    # amount limit is different: the hook already blocks an over-limit call
    # before this function ever runs, but this check stays as a defense in
    # depth backstop (Stage 4's business-error case) for any path that
    # reaches this function without going through that hook.
    if amount > REFUND_AMOUNT_LIMIT:
        return _error(
            "business",
            False,
            f"Refunds over ${REFUND_AMOUNT_LIMIT} need manager approval and "
            "can't be auto-processed.",
        )
    return {
        "refund_id": f"REF-{order_id}",
        "order_id": order_id,
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "status": "processed",
    }


def escalate_to_human(
    summary: str, reason: str, customer_id: str | None = None
) -> dict[str, Any]:
    return {
        "escalation_id": f"ESC-{_generate_ticket_id(summary)}",
        "status": "escalated",
        "summary": summary,
        "customer_id": customer_id,
        "reason": reason,
    }


def _generate_ticket_id(summary: str) -> str:
    """Deterministic-ish stand-in for a real ticket ID generator."""
    return str(abs(hash(summary)) % 100000)


# --- SDK tool wrappers -------------------------------------------------------


@tool(
    "get_customer",
    "Verifies a customer's identity and looks up their own account (name, "
    "signup date, loyalty tier). Requires BOTH their email address AND "
    "their customer_id (format 'CUST-XXX') — a single identifier alone is "
    "not enough to verify someone; both must be typed by the customer "
    "themselves and must match the same account. Example queries: 'my "
    "email is jane@example.com and my customer ID is CUST-002, what's my "
    "loyalty tier?', 'verify me — CUST-001, priya.nair@example.com'. If "
    "the customer has only given you one of the two, ask for the other "
    "one instead of calling this tool or guessing the missing value. If "
    "the email and customer_id don't both match the same account, this "
    "returns a structured error — ask the customer to double-check both "
    "rather than guessing new values. Does NOT look up orders — an order "
    "ID (e.g. 'ORD-1004') is never a valid input here; use lookup_order "
    "for anything about a specific order.",
    {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": "Customer's email address, as typed by the customer",
            },
            "customer_id": {
                "type": "string",
                "description": "Customer's customer_id (format 'CUST-XXX'), as typed by the customer",
            },
        },
        "required": ["email", "customer_id"],
    },
)
async def get_customer_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = get_customer(args["email"], args["customer_id"])
    return {"content": [{"type": "text", "text": _to_json(result)}]}


@tool(
    "lookup_order",
    "Looks up one specific order's details (item, amount, status, "
    "delivery date) by its order_id (format 'ORD-XXXX'), and requires a "
    "customer_id already verified via get_customer to confirm the order "
    "belongs to that customer. The status field is either 'in_transit' "
    "(already shipped, on its way, just not yet delivered) or 'delivered' "
    "(received by the customer) — when explaining 'in_transit' to a "
    "customer, say the order has shipped, not that it hasn't. Example "
    "queries: 'what's the status of "
    "order ORD-1002?', 'has my order shipped yet?', 'can I return "
    "ORD-1005?'. If the order doesn't exist, or exists but belongs to a "
    "different customer, this returns a structured error — don't retry "
    "by guessing another order_id or customer_id. Does NOT look up "
    "customer account info (name, email, loyalty tier) — use "
    "get_customer for that.",
    {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "customer_id": {
                "type": "string",
                "description": "Must be a verified customer_id from get_customer",
            },
        },
        "required": ["order_id", "customer_id"],
    },
)
async def lookup_order_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = lookup_order(args["order_id"], args["customer_id"])
    return {"content": [{"type": "text", "text": _to_json(result)}]}


@tool(
    "process_refund",
    "Issues a refund for an order",
    {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "customer_id": {"type": "string"},
            "amount": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["order_id", "customer_id", "amount", "reason"],
    },
)
async def process_refund_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = process_refund(
        args["order_id"], args["customer_id"], args["amount"], args["reason"]
    )
    return {"content": [{"type": "text", "text": _to_json(result)}]}


@tool(
    "escalate_to_human",
    "Hands off the conversation to a human support agent. The summary should be structured: root cause, what was tried, recommended action. Only use this tool when the agent is unable to resolve the issue, or when the customer explicitly requests human support.",
    {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Structured handoff: root cause, what was tried, recommended action",
            },
            "customer_id": {"type": "string"},
            "reason": {
                "type": "string",
                "enum": [
                    "customer_requested",
                    "policy_gap",
                    "unable_to_progress",
                    "policy_exception_needed",
                ],
            },
        },
        "required": ["summary", "reason"],
    },
)
async def escalate_to_human_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = escalate_to_human(
        args["summary"], args["reason"], args.get("customer_id")
    )
    return {"content": [{"type": "text", "text": _to_json(result)}]}


def _to_json(result: dict[str, Any]) -> str:
    return json.dumps(result)


SUPPORT_BOT_TOOLS = create_sdk_mcp_server(
    name="support_bot_tools",
    tools=[
        get_customer_tool,
        lookup_order_tool,
        process_refund_tool,
        escalate_to_human_tool,
    ],
)
