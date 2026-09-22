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
tool from the other (see `support_bot/tools.py`) — re-test the same
messages above and add findings here. Things worth specifically checking
against the "before" result above:

- Does the model now catch an order ID mislabeled as an "account number"
  before asking to verify, instead of just asking blindly?
- For a message that hands over both an order_id and a customer_id in one
  sentence (e.g. "What's going on with ORD-1004, I'm customer CUST-002"),
  does it still call `get_customer` first, or does it trust the
  customer-provided ID and go straight to `lookup_order`? Worth noting
  either way — a description can *say* an ID must be "verified via
  get_customer" without that being a guarantee the model follows it, which
  is exactly the gap Stage 3's hooks exist to close.
