"""The four support tools, plus their registration as an in-process SDK MCP server.

Each tool is implemented as a plain function first (the actual lookup/mutation
logic against mock_data), then wrapped with @tool so it can be handed to the
agent via create_sdk_mcp_server(). Keeping the plain function and the SDK
wrapper separate makes the business logic easy to unit test without spinning
up the SDK.

Descriptions on get_customer and lookup_order are deliberately thin — that's
the Stage 2 "before" state from PROJECT_BRIEF.md, not an oversight.
"""

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from support_bot import data


def _error(error_category: str, is_retryable: bool, message: str) -> dict[str, Any]:
    """Structured error shape used by every tool (see CLAUDE.md conventions)."""
    return {
        "error": {
            "errorCategory": error_category,
            "isRetryable": is_retryable,
            "message": message,
        }
    }


# --- Plain functions (the actual business logic) ---------------------------


def get_customer(identifier: str) -> dict[str, Any]:
    customer = data.find_customer(identifier)
    if customer is None:
        return _error(
            "validation",
            False,
            f"No customer found matching '{identifier}'.",
        )
    return customer


def lookup_order(order_id: str, customer_id: str) -> dict[str, Any]:
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
    return order


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
    # No business-rule enforcement here (e.g. refund amount limits, prior
    # verification). Per CLAUDE.md, that's enforced by hooks (Stage 3), not
    # inside the tool itself.
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
    "Looks up a customer's own account (name, email, signup date, loyalty "
    "tier) by their email address or customer_id (format 'CUST-XXX'). "
    "Example queries: 'my email is jane@example.com, what's my loyalty "
    "tier?', 'look up customer CUST-002', 'do you have an account for "
    "this email?'. If the identifier doesn't match any customer, this "
    "returns a structured error — ask the customer to confirm it rather "
    "than guessing another value. Does NOT look up orders — an order ID "
    "(e.g. 'ORD-1004') is never a valid input here; use lookup_order for "
    "anything about a specific order.",
    {
        "type": "object",
        "properties": {
            "identifier": {
                "type": "string",
                "description": "Customer email or customer_id",
            }
        },
        "required": ["identifier"],
    },
)
async def get_customer_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = get_customer(args["identifier"])
    return {"content": [{"type": "text", "text": _to_json(result)}]}


@tool(
    "lookup_order",
    "Looks up one specific order's details (item, amount, status, "
    "delivery date) by its order_id (format 'ORD-XXXX'), and requires a "
    "customer_id already verified via get_customer to confirm the order "
    "belongs to that customer. Example queries: 'what's the status of "
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
