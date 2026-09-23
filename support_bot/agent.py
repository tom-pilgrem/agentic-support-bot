"""Stage 5: a multi-turn conversation loop on top of the Stage 1 agentic loop.

Stage 1-4 used the SDK's one-shot `query()` function: every invocation
was a brand new session with no memory, so the customer could never
answer a clarifying question. This module instead uses `ClaudeSDKClient`,
the SDK's stateful, bidirectional client: it connects once, and every
customer message after that is sent into the *same* live session, so the
full message history (earlier questions, tool results, the agent's own
replies) carries across turns.

Each individual turn is still the Stage 1 agentic loop: send a message,
let the SDK run tools until it's done, and decide "this turn is over"
from `stop_reason` on the terminal ResultMessage — never from the
presence of assistant text, and never from a manually-counted number of
iterations. (`max_turns` below is a safety ceiling passed to the SDK, not
something this code counts itself.)

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
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from support_bot.hooks import RefundEnforcement
from support_bot.tools import SUPPORT_BOT_TOOLS

SYSTEM_PROMPT = (
    "You are a customer support agent. Use the available tools to look up "
    "customers and orders, process refunds, and escalate to a human when "
    "needed. Verifying a customer's identity requires BOTH their email "
    "address AND their customer_id — never call get_customer with only "
    "one of the two. Only use an email or customer_id that the customer "
    "has typed themselves, in this conversation. Never guess, infer, or "
    "reuse any other email or ID you may have access to for any other "
    "purpose — this includes not mentioning, suggesting, or asking the "
    "customer to confirm a candidate email or ID that they didn't type. "
    "You have no information about who this customer is until they tell "
    "you both. If you're missing either one, simply ask them to provide "
    "it; do not propose a value of your own. If process_refund "
    "is blocked, that block is final for this conversation — don't retry it "
    "or argue the customer's case yourself; call escalate_to_human instead "
    "and explain to the customer that it's been handed off. Tool errors are "
    "structured with an errorCategory and an isRetryable flag. If a call "
    "fails with isRetryable true (a transient error), you may retry that "
    "exact same call once before giving up. If isRetryable is false "
    "(validation, business, or permission errors), do not retry it — "
    "explain the situation to the customer in plain language instead, and "
    "escalate if that leaves you unable to help them."
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


EXIT_COMMANDS = {"quit", "exit"}


def build_options(enforcement: RefundEnforcement) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        tools=[],  # no built-in tools (Bash, Read, ...) — only our four
        mcp_servers={"support_bot": SUPPORT_BOT_TOOLS},
        allowed_tools=ALLOWED_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        setting_sources=[],  # don't load this machine's own CLAUDE.md/settings
        hooks=enforcement.as_hook_config(),
        max_turns=10,  # per customer message, not per conversation
    )


async def run_turn(client: ClaudeSDKClient, message: str) -> AgentResult:
    """Send one customer message into the already-connected conversation
    and wait for the agent to finish responding to it."""
    await client.query(message)

    tool_calls: list[str] = []
    result: ResultMessage | None = None

    # receive_response() yields this turn's messages and stops after the
    # ResultMessage. Unlike query()'s stream, it does NOT end the session —
    # the client stays connected and ready for the next customer message.
    async for event in client.receive_response():
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
            # runs. We record it and let receive_response() finish on its
            # own rather than breaking out early.
            result = event

    if result is None:
        # receive_response() only ends after a ResultMessage; getting here
        # means the CLI process ended without producing one.
        raise RuntimeError("Agent SDK stream ended without a ResultMessage")

    print(f"  [stop_reason] {result.stop_reason} (is_error={result.is_error})")
    if result.stop_reason == "end_turn" and not result.is_error:
        return AgentResult(result.stop_reason, result.result, tool_calls)
    return AgentResult(result.stop_reason, None, tool_calls)


async def read_customer_message() -> str | None:
    """Read the next customer message from stdin. Returns None when the
    customer is done (typed quit/exit, or stdin closed)."""
    try:
        # input() blocks, so run it in a thread to keep the event loop (and
        # the SDK client's background reader) free while we wait.
        line = await asyncio.to_thread(input, "\ncustomer> ")
    except EOFError:
        return None
    if not sys.stdin.isatty():
        print(line)  # piped input isn't echoed by the terminal; show it in the log
    line = line.strip()
    if line.lower() in EXIT_COMMANDS:
        return None
    return line


async def run_conversation(first_message: str | None) -> None:
    # One RefundEnforcement for the whole conversation, not one per
    # message: "verified earlier in the same session" now means earlier in
    # this conversation, so a customer verified on turn 1 must still count
    # as verified when they ask for a refund on turn 3.
    enforcement = RefundEnforcement()

    async with ClaudeSDKClient(options=build_options(enforcement)) as client:
        if first_message is not None:
            message = first_message
            print(f"\ncustomer> {message}")
        else:
            message = await read_customer_message()

        while message is not None:
            if message:  # skip blank lines
                result = await run_turn(client, message)
                print()
                if result.succeeded:
                    print(f"agent> {result.text}")
                else:
                    print(f"[did not complete normally] stop_reason={result.stop_reason}")
            message = await read_customer_message()

    print("\n[conversation ended]")


def main() -> None:
    # An optional first message can be passed on the command line; after
    # that the conversation continues interactively on stdin.
    first_message = " ".join(sys.argv[1:]) or None
    print("Customer support chat — type 'quit' to end the conversation.")
    asyncio.run(run_conversation(first_message))


if __name__ == "__main__":
    main()
