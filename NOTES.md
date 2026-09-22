# Learning notes

Findings from each stage's exercises that are worth remembering for the exam —
not implementation detail (that's what the code and PR descriptions are for),
just what was actually observed when testing.

## Stage 2 — tool selection (Task 2.1)

**Setup:** `get_customer` and `lookup_order` started with the brief's
deliberately thin descriptions ("Retrieves customer information" / "Retrieves
order details"). Ran the same ~11 messages before and after rewriting them.

**Before, with thin descriptions:** no message reliably produced the "wrong
tool" or "calls both when it didn't need to" failure the brief predicts.
Across every test — including adversarial ones like mislabeling an order ID
as an "account number", or giving an order_id alongside an unrelated question
— the model either asked for a missing identifier or called the two tools in
the correct order. Likely reason: the *parameter-level* schema descriptions
("Customer email or customer_id", "Must be a verified customer_id from
get_customer") and required fields already carry most of the disambiguating
signal, independent of the top-level tool description's quality. With a
strong current model and only two, structurally distinct tools, thin
top-level descriptions alone didn't reproduce the failure mode — that's an
honest result, not a dodge of the exercise, and it's worth remembering that
this exercise may show more dramatic before/after deltas with weaker models,
more tools, or tools whose *parameters* (not just names) genuinely overlap.

**After rewriting** both descriptions by hand with input format, 2-3
example queries, edge cases, and an explicit sentence distinguishing each
tool from the other (see `support_bot/tools.py`), then re-tested the same
~11 messages:

- **Real improvement:** the model now explicitly catches an order ID
  mislabeled as an "account number" or "customer" before asking to
  verify — e.g. *"ORD-1004 looks like an order ID rather than an account
  number"* — instead of just asking blindly for an identifier like the
  "before" version did.
- **Verification order held up correctly.** For messages that hand over
  both an order_id and a customer_id in one sentence (e.g. "What's going
  on with ORD-1004, I'm customer CUST-002"), the model still called
  `get_customer` before `lookup_order` every time, across two separate
  rounds of edits to the description text.
- One earlier draft rewrite (mine, since reverted) caused the model to
  skip `get_customer` and trust a customer-typed ID directly in one case
  — worth remembering that this specific check (does description wording
  guarantee verification order) is sensitive to exact phrasing, not just
  "does it mention verification at all." The description saying an ID
  must be "verified via get_customer" is prose, not a structural
  guarantee — a live example of why Stage 3 enforces this with hooks
  instead.
- **Overall:** across 9 re-run messages with the final descriptions, zero
  wrong-tool calls and zero unnecessary calls.

## Stage 2 — a second, unrelated bug found while testing

While re-testing the above, `get_customer` was repeatedly called (and, in
one run, merely *suggested in text*) with an email address that was never
in the customer's message at all — the developer's own account email. The
`claude` CLI subprocess the Agent SDK spawns appears to expose some
ambient account-level context to the model, and the model was treating it
as a plausible stand-in for "the customer's" identity.

- Ruled out project/user settings leaking in (`setting_sources=[]` didn't
  stop it) and `ClaudeAgentOptions.user` (unset, defaults to `None`).
  Likely inherent to the CLI's own authenticated-session context, outside
  what documented SDK options govern.
- **This is not something Stage 3's hooks will fix.** Hooks intercept
  tool calls; the second occurrence of this bug involved zero tool calls
  — the model just floated a candidate email in its reply text. Hooks and
  prompts operate at different layers; this needed a prompt-level fix
  specifically because no tool call was involved to gate.
- First fix (system prompt: "only use an identifier the customer
  provided, never guess") stopped the tool-call version, but the bug
  still resurfaced once in 9 follow-up test runs — as text, not a tool
  call. A prompt-only mitigation reduced the failure rate but wasn't
  deterministic, which is itself worth remembering: this is the same
  "prompts vs. hooks" lesson as above, just applied to identity handling
  instead of refund verification, and to a case hooks structurally can't
  reach (no tool call to intercept).
- Second, stronger fix in `support_bot/agent.py`'s `SYSTEM_PROMPT`:
  explicit instruction not to guess, infer, reuse, *or even suggest/ask
  to confirm* a candidate email or ID the customer didn't type. Stress
  tested with 5 repeated runs of the exact message that leaked before,
  plus a full 9-message re-run of the routing tests — clean every time
  after this change.
