# Customer Support Resolution Agent

This project is a learning exercise for the Claude Certified Architect – Foundations exam.
Read PROJECT_BRIEF.md before writing any code — it defines a staged build order and the
reasoning behind each stage. Build strictly in stage order; don't skip ahead to hooks or
escalation logic before the bare agentic loop is working and tested.

## Stack
- Language: Python
- Claude Agent SDK for the agent loop, tool registration, and hooks
- Mock backend: mock_data/customers.json and mock_data/orders.json — no real database

## Conventions
- Tools live in a single module, one function per tool, matching the schemas in
  PROJECT_BRIEF.md.
- Every tool returns errors as structured objects: {errorCategory, isRetryable, message},
  never bare strings or exceptions surfaced directly to the model.
- Hooks live separately from tool implementations so enforcement logic is easy to point at
  and explain (this is the whole point of the exercise — keep it legible, not clever).
- Prefer explicit, readable control flow over abstraction. This is a study project — code
  clarity matters more than reusability.

## Current stage
Stage 4 complete. tools.py now returns two more structured error
categories on top of validation/permission: transient (randomly-simulated
backend timeout on get_customer/lookup_order, 15% chance, isRetryable
true) and business (refund over the $200 limit, isRetryable false — a
defense-in-depth backstop, since the Stage 3 hook already blocks that
case before this code runs in normal operation). REFUND_AMOUNT_LIMIT now
lives in tools.py as the single source of truth; hooks.py imports it.
SYSTEM_PROMPT tells the model to retry once on isRetryable true, never
otherwise. Confirmed end-to-end per category. Full writeup in NOTES.md.

PROJECT_BRIEF.md's staging was revised: a new Stage 5 (multi-turn
conversation loop, Task 1.7 — swap the one-shot query() call for the
SDK's stateful ClaudeSDKClient) was inserted after Stage 4. Escalation
calibration is now Stage 6, multi-concern decomposition is now Stage 7.
Stage 5 (multi-turn conversation loop) not yet started.

## Do not
- Do not implement loop termination by checking for assistant text content or capping
  iterations as the primary stop condition. Use stop_reason only (see PROJECT_BRIEF.md Stage 1).
- Do not enforce the refund-verification rule or the refund-amount policy in the system prompt
  alone. These must be enforced by hooks (Stage 3) — that's the thing being learned here.

## Git Workflow

This is a learning project. I (the human) want to review and understand every
change before it lands in `main`, so always work on branches and open PRs —
never commit directly to `main`.

### Branch naming
- New features/frameworks you scaffold: `claude/<short-feature-name>`
  (e.g. `claude/auth-system`, `claude/api-routes`)
- Bug fixes: `claude/fix-<short-description>`
- Refactors: `claude/refactor-<short-description>`
- If continuing work on a branch I've already started editing, ask before
  creating a new one — check with `git branch` first.

### Workflow
1. Before starting new work, check out `main` and pull latest, then create a
   new branch from it using the naming convention above.
2. Make commits in small, logical chunks rather than one giant commit — this
   makes the PR easier for me to review and learn from.
3. Write descriptive commit messages explaining *why*, not just *what*
   (e.g. "Add JWT middleware to validate auth tokens before route handlers"
   rather than "add auth").
4. When a feature/fix is ready, do NOT merge it yourself. Push the branch and
   let me know it's ready for a PR — I'll create/review the PR in GitHub, or
   ask you to do so via `gh pr create` with a clear description.
5. When opening a PR, include in the description:
   - What changed and why
   - Any decisions/trade-offs made and alternatives considered
   - Anything I should pay particular attention to when reviewing (tricky
     logic, new patterns, things worth learning from)

### Commit messages
Use conventional commit style prefixes where possible:
- `feat: ...` new feature
- `fix: ...` bug fix
- `refactor: ...` code change that neither fixes a bug nor adds a feature
- `docs: ...` documentation only
- `test: ...` adding or correcting tests

### Code review etiquette
- If I ask you to review my own edits/commits, be honest and thorough —
  point out bugs, unclear naming, or patterns that could be improved, don't
  just approve everything.
- If I ask "why did you do it this way," explain your reasoning rather than
  just restating what the code does.

### How this interacts with the staged build plan
Each stage in PROJECT_BRIEF.md is a good natural unit for a branch — e.g.
`claude/agentic-loop` for Stage 1, `claude/tool-selection-fix` for Stage 2,
`claude/refund-hooks` for Stage 3, and so on. Since you'll be making manual
edits in VS Code between Claude Code sessions while learning, always run
`git status` and `git branch` before starting a new stage so work in progress
isn't accidentally branched over or left uncommitted.
