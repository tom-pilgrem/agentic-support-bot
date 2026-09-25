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
import os
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from dotenv import load_dotenv

from support_bot.hooks import RefundEnforcement
from support_bot.tools import (
    MOCK_TODAY,
    REFUND_AMOUNT_LIMIT,
    RETURN_WINDOW_DAYS,
    SUPPORT_BOT_TOOLS,
)

BASE_INSTRUCTIONS = (
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

# Stage 6: the policy the agent resolves against. Before this, no policy was
# stated anywhere the model could see — see NOTES.md for what that caused.
RETURN_POLICY = f"""
Today's date is {MOCK_TODAY}.

Refund policy — this is the complete policy; nothing else is covered:
- A delivered order can be refunded in full within {RETURN_WINDOW_DAYS} days
  of its delivered_date, for any reason (including just not liking it).
  lookup_order returns within_return_window — go by that field, don't
  work the dates out yourself. The system blocks out-of-window refunds
  regardless of what the customer says about the delivery date.
- An item that arrived damaged or defective can be refunded in full within
  {RETURN_WINDOW_DAYS} days of delivery. The customer describing the damage,
  or saying they have photos, counts as enough evidence — don't ask them to send anything.
- An order that is still in_transit can't be refunded yet. Give the
  customer its status instead.
- Refunds over ${REFUND_AMOUNT_LIMIT} can't be approved automatically; the
  system will block them and they must be escalated.
"""

ESCALATION_CRITERIA = """
When to escalate to a human (escalate_to_human), and when not to:

Escalate when:
1. The customer asks for a human. Escalate straight away with
   reason='customer_requested' — don't verify them, look anything up, or
   try to solve the problem first. Pass along whatever identifiers and
   details they've already given in the summary.
2. The request falls outside the policy above — something the policy
   doesn't address (e.g. a return outside the return window, or a kind of
   request the policy doesn't mention). Use reason='policy_gap'. Never
   invent an exception or stretch the policy to cover it.
3. A tool blocks or fails in a way that leaves you unable to help
   (reason='policy_exception_needed' for a blocked over-limit refund,
   'unable_to_progress' otherwise).

Do NOT escalate when the request is covered by the policy and you have
what you need to resolve it. Resolve it yourself. How the customer feels
is not a reason to escalate: if they're frustrated or angry but the case is
straightforward, acknowledge how they feel briefly and then fix it.

Examples:

Customer: "Can I just talk to a real person? It's about my order ORD-1004."
Right: call escalate_to_human immediately (reason='customer_requested',
summary mentioning ORD-1004), then tell them it's been handed off.
Wrong: asking for their email and customer_id, or looking up the order
first.

Customer (verified; within_return_window true, $45): "The blender I got
stopped working after two uses, I'd like my money back."
Right: this is within the return window and under the limit — process the
refund and confirm it. Wrong: escalating it.

Customer (verified; lookup_order shows within_return_window false): "I know it's been a
while, but can I still return this jacket?"
Right: explain it's outside the return window and that you're
passing it to a colleague who can review it, then escalate with
reason='policy_gap'. Wrong: refunding it anyway, or refusing outright
without escalating.
"""

# Stage 7: one message can bundle several unrelated requests. The model
# already decomposed these correctly at baseline (see NOTES.md); this
# section is mostly there to keep it that way. Making sure the customer
# actually *sees* every answer is not left to the prompt: run_turn shows
# them all of the turn's text, not just the last block. The last rule here
# just tells the model that, so it doesn't repeat itself.
MULTI_CONCERN_HANDLING = """
When one message contains more than one request:
- Before calling any tools, identify every separate request in the
  message, including any mentioned in passing at the end.
- Verify the customer once and reuse that result for every request. Don't
  call get_customer again unless a call failed.
- Handle each request on its own terms. One request being blocked,
  escalated or outside the policy doesn't stop you resolving the others.
  Likewise, escalating one request doesn't cover the others: every request
  that needs a human must be in an escalate_to_human summary.
- Everything you write during a turn, including text between tool calls,
  is shown to the customer in order as one reply. Make sure every request
  and its outcome is covered somewhere in it, and don't repeat a summary
  you've already written.
"""

SYSTEM_PROMPT = BASE_INSTRUCTIONS + RETURN_POLICY + ESCALATION_CRITERIA + MULTI_CONCERN_HANDLING

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

MODEL = "claude-sonnet-5"

# Why an API key is required rather than just recommended: when the
# bundled Claude Code CLI is signed in via a claude.ai account instead, it
# injects that account's email into every session ("The user's email
# address is ..."). The support agent then treats the *developer's* email
# as the customer's and offers it up unprompted, which no system-prompt
# rule reliably stops (see NOTES.md). With API-key auth there's no
# account, so nothing gets injected.
MISSING_API_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. This bot must run on an API key, not a "
    "claude.ai login: a claude.ai login makes the CLI inject your account "
    "email into the conversation, which the agent then treats as the "
    "customer's. Export ANTHROPIC_API_KEY and try again."
)


def build_options(enforcement: RefundEnforcement) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        tools=[],  # no built-in tools (Bash, Read, ...) — only our four
        mcp_servers={"support_bot": SUPPORT_BOT_TOOLS},
        allowed_tools=ALLOWED_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        setting_sources=[],  # don't load this machine's own CLAUDE.md/settings
        hooks=enforcement.as_hook_config(),
        max_turns=10,  # per customer message, not per conversation
    )


def format_cache_usage(usage: dict | None) -> str:
    """One-line summary of how an API call's input tokens were billed.

    Caching is applied by the bundled CLI automatically — nothing in
    build_options() turns it on. This is just to watch it happen:
      read     = input tokens served from cache (~0.1x cost)
      write    = input tokens written to cache this call (~1.25x cost)
      uncached = input tokens billed at the normal rate
    Expect a big `write` on the first call of a conversation, then mostly
    `read` on every call after it (the tools + system prompt + history
    prefix is resent unchanged each time).
    """
    if not usage:
        return "no usage reported"
    read = usage.get("cache_read_input_tokens", 0)
    write = usage.get("cache_creation_input_tokens", 0)
    uncached = usage.get("input_tokens", 0)
    return f"read={read} write={write} uncached={uncached}"


async def run_turn(client: ClaudeSDKClient, message: str) -> AgentResult:
    """Send one customer message into the already-connected conversation
    and wait for the agent to finish responding to it."""
    await client.query(message)

    tool_calls: list[str] = []
    # Every text block the agent writes this turn. The customer is shown
    # all of them, not just ResultMessage.result — that's only the *last*
    # block, and in Stage 7 testing the agent wrote a full multi-part
    # answer, made one more tool call, then ended with a one-line closer.
    # Showing only the closer silently dropped two of three answers.
    texts: list[str] = []
    result: ResultMessage | None = None
    # One API response can arrive as several AssistantMessages (one per
    # content block) that share a message_id and the same usage. Track the
    # ids already logged so each API call's cache usage prints once.
    logged_message_ids: set[str] = set()

    # receive_response() yields this turn's messages and stops after the
    # ResultMessage. Unlike query()'s stream, it does NOT end the session —
    # the client stays connected and ready for the next customer message.
    async for event in client.receive_response():
        if isinstance(event, SystemMessage) and event.subtype == "init":
            # Confirms which credential the CLI actually used — should be
            # ANTHROPIC_API_KEY, never a claude.ai login (see MISSING_API_KEY_MESSAGE).
            print(f"  [auth] apiKeySource={event.data.get('apiKeySource')}")

        if isinstance(event, AssistantMessage):
            for block in event.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(block.name)
                    print(f"  [tool_use] {block.name}({block.input})")
                elif isinstance(block, TextBlock) and block.text:
                    texts.append(block.text)
                    print(f"  [assistant text] {block.text}")
            if event.message_id not in logged_message_ids:
                logged_message_ids.add(event.message_id)
                print(f"  [cache] api call: {format_cache_usage(event.usage)}")

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

    print(f"  [cache] turn total: {format_cache_usage(result.usage)}")
    print(f"  [stop_reason] {result.stop_reason} (is_error={result.is_error})")
    # Whether the turn succeeded is still decided by stop_reason alone; the
    # collected text only decides what the customer sees.
    if result.stop_reason == "end_turn" and not result.is_error:
        reply = "\n\n".join(texts) or None
        return AgentResult(result.stop_reason, reply, tool_calls)
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
    # Pick up ANTHROPIC_API_KEY from a gitignored .env file, if there is
    # one. A variable already set in the shell takes priority over .env.
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(MISSING_API_KEY_MESSAGE)

    first_message = " ".join(sys.argv[1:]) or None
    print("Customer support chat — type 'quit' to end the conversation.")
    asyncio.run(run_conversation(first_message))


if __name__ == "__main__":
    main()
