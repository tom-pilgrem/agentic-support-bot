"""Stage 1: the bare agentic loop.

Sends one customer message through the Claude Agent SDK with the four
support tools registered, and returns once the SDK reports the turn is
over. `stop_reason` on the terminal ResultMessage is the only thing that
decides "are we done" — never the presence of assistant text, and never a
manually-counted number of iterations. (`max_turns` below is a safety
ceiling passed to the SDK, not something this code counts itself.)

The Agent SDK runs its own internal loop against the CLI subprocess and
calls registered tools automatically; this module doesn't dispatch tool
calls itself — it sends the message, observes the resulting message
stream, and reports what happened.
"""

import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from support_bot.hooks import RefundEnforcement
from support_bot.tools import SUPPORT_BOT_TOOLS

SYSTEM_PROMPT = (
    "You are a customer support agent. Use the available tools to look up "
    "customers and orders, process refunds, and escalate to a human when "
    "needed. Only use a customer identifier (email or customer_id) that the "
    "customer has typed themselves, in this conversation. Never guess, "
    "infer, or reuse any other email or ID you may have access to for any "
    "other purpose — this includes not mentioning, suggesting, or asking "
    "the customer to confirm a candidate email or ID that they didn't "
    "type. You have no information about who this customer is until they "
    "tell you. If you don't have an identifier from them, simply ask them "
    "to provide one; do not propose a value of your own. If process_refund "
    "is blocked, that block is final for this conversation — don't retry it "
    "or argue the customer's case yourself; call escalate_to_human instead "
    "and explain to the customer that it's been handed off."
)

ALLOWED_TOOLS = [
    "mcp__support_bot__get_customer",
    "mcp__support_bot__lookup_order",
    "mcp__support_bot__process_refund",
    "mcp__support_bot__escalate_to_human",
]


class AgentResult:
    def __init__(self, stop_reason: str | None, text: str | None, tool_calls: list[str]):
        self.stop_reason = stop_reason
        self.text = text
        self.tool_calls = tool_calls

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "end_turn" and self.text is not None


async def run_agent(message: str) -> AgentResult:
    # A fresh RefundEnforcement per call: "verified earlier in the same
    # session" means this session, so the verified-customer set must not
    # survive past this one run_agent() call.
    enforcement = RefundEnforcement()

    options = ClaudeAgentOptions(
        tools=[],  # no built-in tools (Bash, Read, ...) — only our four
        mcp_servers={"support_bot": SUPPORT_BOT_TOOLS},
        allowed_tools=ALLOWED_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        setting_sources=[],  # don't load this machine's own CLAUDE.md/settings
        hooks=enforcement.as_hook_config(),
        max_turns=10,
    )

    tool_calls: list[str] = []
    result: ResultMessage | None = None

    async for event in query(prompt=message, options=options):
        if isinstance(event, AssistantMessage):
            for block in event.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(block.name)
                    print(f"  [tool_use] {block.name}({block.input})")
                elif isinstance(block, TextBlock) and block.text:
                    print(f"  [assistant text] {block.text}")

        if isinstance(event, ResultMessage):
            # The ONLY thing this loop uses to decide the turn is over:
            # stop_reason on the terminal ResultMessage. Not the text
            # printed above — that's just for visibility while the loop
            # runs. ResultMessage is always the last item query() yields,
            # so we just record it and let the generator finish on its own
            # rather than breaking out early (an early return/break here
            # trips up the SDK's async generator cleanup).
            result = event

    if result is None:
        # query()'s stream always ends with a ResultMessage; getting here
        # means the CLI process ended without producing one.
        raise RuntimeError("Agent SDK stream ended without a ResultMessage")

    print(f"  [stop_reason] {result.stop_reason} (is_error={result.is_error})")
    if result.stop_reason == "end_turn" and not result.is_error:
        return AgentResult(result.stop_reason, result.result, tool_calls)
    return AgentResult(result.stop_reason, None, tool_calls)


def main() -> None:
    message = " ".join(sys.argv[1:]) or (
        "What's the status of order ORD-1002 for customer CUST-001?"
    )
    print(f"customer message: {message!r}\n")
    result = asyncio.run(run_agent(message))

    print()
    if result.succeeded:
        print(result.text)
    else:
        print(f"[did not complete normally] stop_reason={result.stop_reason}")


if __name__ == "__main__":
    main()
