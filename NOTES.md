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

**After rewriting** both descriptions with input format, 2-3 example
queries, edge cases, and an explicit sentence distinguishing each tool from
the other (see `support_bot/tools.py`):

- One real improvement: given "Here's my account number: ORD-1004, can you
  check on it?", the model now explicitly says *"It looks like ORD-1004 is
  an order ID rather than an account number"* before asking to verify —
  before the rewrite it just asked for an identifier without noticing the
  mismatch.
- One unexpected, more important result: given "What's going on with
  ORD-1004, I'm customer CUST-002", the *before* version called
  `get_customer` then `lookup_order` (verifying first); the *after* version
  skipped `get_customer` entirely and called `lookup_order` directly with
  the customer-provided ID. The new description explicitly states
  `lookup_order` "requires a customer_id already verified via
  get_customer" — but that's prose, not a structural constraint, and the
  model treated an ID the customer just typed as good enough. Tightening
  the description in one place (clarity about the tool's contract) didn't
  guarantee the model actually honors that contract.

**Takeaway carried into Stage 3:** description quality changed *some*
behavior for the better, but did not — and structurally cannot — guarantee
the verification-before-refund/lookup ordering. That's exactly the "why
hooks, not prompts" lesson Stage 3 is about, and it showed up organically
here rather than needing to be manufactured.
