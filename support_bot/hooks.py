"""Stage 3 (+ Stage 6): programmatic enforcement via PreToolUse/PostToolUse hooks.

Three rules the agent must never be able to talk its way around, no matter
what the system prompt says or what an adversarial customer message asks
for:

  1. process_refund is blocked unless get_customer has already returned a
     verified customer in this same session.
  2. process_refund is blocked for amounts over REFUND_AMOUNT_LIMIT.
  3. process_refund is blocked for orders outside the return window
     (delivered_date + RETURN_WINDOW_DAYS is before today), or not yet
     delivered at all. Added in Stage 6, after the model once refunded
     an order delivered 34 days ago that it had misread as in-window.

All three are enforced here, not in the system prompt (see CLAUDE.md "Do not").
A prompt-only version of rule 1 is demonstrably bypassable — that's the
whole point of Stage 3's adversarial test. When any of these hooks blocks a call,
its denial reason tells the model to use escalate_to_human instead of
retrying, but the *enforcement* is the block itself, which happens whether
or not the model reads or heeds that suggestion.

Kept in its own module, separate from tools.py, so the enforcement logic is
easy to find and reason about on its own.
"""

import json
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from support_bot import data
from support_bot.tools import (
    MOCK_TODAY,
    REFUND_AMOUNT_LIMIT,
    RETURN_WINDOW_DAYS,
    is_within_return_window,
    return_window_deadline,
)

GET_CUSTOMER_TOOL = "mcp__support_bot__get_customer"
PROCESS_REFUND_TOOL = "mcp__support_bot__process_refund"


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _parse_tool_result(tool_response: Any) -> dict[str, Any] | None:
    """Normalize a PostToolUse tool_response into the plain dict our tool
    functions return.

    Confirmed by inspection of the actual PostToolUse payload: for an SDK
    MCP tool, ``tool_response`` is the bare content-block list our @tool
    wrapper returned under its "content" key — i.e.
    ``[{"type": "text", "text": "<json>"}]`` — not that dict itself. This
    also accepts a dict-wrapped or bare-JSON-string form defensively, in
    case that shape differs for other tool types or SDK versions.
    """
    if isinstance(tool_response, str):
        try:
            tool_response = json.loads(tool_response)
        except ValueError:
            return None

    if isinstance(tool_response, dict):
        tool_response = tool_response.get("content", tool_response)

    if isinstance(tool_response, list):
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (KeyError, ValueError):
                    continue
        return None

    return tool_response if isinstance(tool_response, dict) else None


def _verified_customer_id(tool_response: Any) -> str | None:
    result = _parse_tool_result(tool_response)
    if not result or "error" in result:
        return None
    return result.get("customer_id")


class RefundEnforcement:
    """Holds the one piece of state these hooks need: which customer_ids
    have been verified via get_customer so far in this session.

    A fresh instance belongs to a single conversation (one
    `run_conversation` call, spanning every customer message in it) — it
    must not be reused or shared across conversations, since "verified
    earlier in the same session" is exactly what it tracks.
    """

    def __init__(self) -> None:
        self.verified_customer_ids: set[str] = set()

    def as_hook_config(self) -> dict[str, list[HookMatcher]]:
        return {
            "PostToolUse": [
                HookMatcher(matcher=GET_CUSTOMER_TOOL, hooks=[self._record_verification]),
            ],
            "PreToolUse": [
                HookMatcher(matcher=PROCESS_REFUND_TOOL, hooks=[self._require_verified_customer]),
                HookMatcher(matcher=PROCESS_REFUND_TOOL, hooks=[self._enforce_amount_limit]),
                HookMatcher(matcher=PROCESS_REFUND_TOOL, hooks=[self._enforce_return_window]),
            ],
        }

    async def _record_verification(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        customer_id = _verified_customer_id(input_data.get("tool_response"))
        if customer_id:
            self.verified_customer_ids.add(customer_id)
        return {}

    async def _require_verified_customer(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        customer_id = input_data.get("tool_input", {}).get("customer_id")
        if customer_id in self.verified_customer_ids:
            return {}
        return _deny(
            f"process_refund blocked: '{customer_id}' has not been verified "
            "via get_customer in this session. Do not retry process_refund "
            "on the customer's say-so — call escalate_to_human with "
            "reason='unable_to_progress' instead."
        )

    async def _enforce_amount_limit(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        amount = input_data.get("tool_input", {}).get("amount")
        if not isinstance(amount, (int, float)) or amount <= REFUND_AMOUNT_LIMIT:
            return {}
        return _deny(
            f"process_refund blocked: amount {amount} exceeds the "
            f"${REFUND_AMOUNT_LIMIT} policy limit and cannot be auto-approved. "
            "Call escalate_to_human with reason='policy_exception_needed' "
            "instead."
        )

    async def _enforce_return_window(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        order_id = input_data.get("tool_input", {}).get("order_id")
        # Read the order straight from the backend, never from anything the
        # model or customer said about it: "it was actually delivered 10
        # days ago" must not be able to move the window.
        order = data.find_order(order_id)
        if order is None:
            return {}  # let process_refund return its own validation error
        if is_within_return_window(order):
            return {}

        if order.get("delivered_date") is None:
            return _deny(
                f"process_refund blocked: order '{order_id}' hasn't been "
                "delivered yet, so it can't be refunded. Don't retry or "
                "escalate — tell the customer the order's current status."
            )
        deadline = return_window_deadline(order["delivered_date"])
        return _deny(
            f"process_refund blocked: order '{order_id}' was delivered on "
            f"{order['delivered_date']}, so its {RETURN_WINDOW_DAYS}-day "
            f"return window closed on {deadline} (today is {MOCK_TODAY}). "
            "This is outside the refund policy. Don't retry process_refund "
            "on the customer's say-so — call escalate_to_human with "
            "reason='policy_gap' instead."
        )
