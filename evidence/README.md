# Evidence

Genuine end-to-end runs against the local LegacyCore workstation: **LLM discovery →
compiled capability → deterministic replay (`model_calls = 0`)**, with safe/redacted
evidence, consequential-write safety, and same-session human handoff. Every discovery
trace is a real Anthropic run (`claude-sonnet-4-6`). Exact commands to reproduce this whole
set are in [`../HOW_TO_DEMO.md`](../HOW_TO_DEMO.md).

Each `replay_*/` directory also contains a `trace.jsonl` - the runtime's own structural
execution trace (which steps ran and whether their checkpoints held, a mutation's verified
effect state, and the terminal result with `model_calls = 0`). It carries only structural
identifiers and enums, never a raw value.

**Safety, verified across the whole set:** no raw member id, financial value, or verification
code is persisted in any run trace, artifact, or result here; financial outputs are masked
(`savings_balance = <financial>`);
routes are recorded as structural patterns, not concrete PII paths; screenshots are
structural-only; and no runtime scenario identifier reaches any model-facing trace.

## Read capability - `member.lookup_savings_balance`

- **`discovery/trace.jsonl`** - the genuine discovery run (`anthropic`, `claude-sonnet-4-6`,
  `model_calls = 4`, `GOAL_REACHED`). Sanitized structural events: the goal is recorded as
  `goal_present`, values as `<param:member_number>`, routes as allowed patterns.
- **`capability/member_lookup.v1.json`** - the typed capability compiled from that run
  (3 steps: type member → search → extract the Share Savings current balance).
- **`replay_success/result.json`** - deterministic replay for a **different** member (`54321`):
  `success`, `model_calls = 0`, output masked (`savings_balance = <financial>`; the raw value is
  returned to the caller on stdout, never persisted).
- **`replay_business_outcome/result.json`** - replay for an **unknown** member (`99999`):
  `business_outcome MEMBER_NOT_FOUND` - a legitimate domain answer, not a crash, `model_calls = 0`.

## Write capability - `member.open_sub_account` (consequential mutation)

- **`discovery_open_sub_account/trace.jsonl`** - the genuine write discovery (`model_calls = 9`,
  `GOAL_REACHED`) including a `consequential_approval` event: the trusted kernel classified
  "Create Account" as `CONSEQUENTIAL_WRITE` and a human authorized that one action.
- **`discovery_open_sub_account/verification_provenance.json`** - maps the compiled verification
  back to the discovery steps that produced it (steps 5-8:
  `Member Inquiry → type <param:member_number> → Search → extract the sub-account status`),
  so the independent verification is provably *discovered*, not authored.
- **`capability/open_sub_account.v1.json`** - the compiled write capability (4 top-level steps)
  with the **embedded read-only verification recipe** on the commit step.
- **`replay_mutation/result.json`** - replay under `commit_then_timeout`: the write is dispatched
  **exactly once**, the response is lost, and an independent read confirms the effect →
  `success`, `model_calls = 0`, no double-write.
- **`replay_mutation_ambiguous/result.json`** - replay under `commit_unverifiable`: the effect
  cannot be established, so the runtime **never guesses** → `escalated MUTATION_AMBIGUOUS` plus a
  sanitized **handoff case** (`intervention`) carrying the capability, step, reason, and structural
  state. This is the **detect-and-route** half - the unattended runner raises the request and stops.

## Human handoff - same-session takeover

> **Intervention *raised* ≠ handoff *completed*.** `replay_mutation_ambiguous` above is a raised
> request that the unattended runner routes and stops on. The three folders below are *completed*
> handoffs: automation pauses, a human takes over the **same live session** (same Page/Context),
> acts, and hands control back, and the run resumes/finishes. A `ControlLease` with monotonic
> epochs is the ownership seam; automation is fenced while the human holds control.

- **`discovery_handoff/`** - discovery-side handoff (`model_calls = 6`): the model looks up a
  flagged account, is refused a consequential step (`RISK_CONFIRMATION_REQUIRED`), and proposes
  `request_human` on its own (`model_call 4`). A human enters the code on the **same session** and
  the model resumes to `GOAL_REACHED`.
  - `trace.jsonl` - `intervention_raised (HUMAN_REQUESTED)`, `control_transferred`
    (`automation → human`, epoch 1), a **recorded `human_action`** (`type` into the verification
    field, value `<redacted>`), `control_transferred` (`human → automation`, epoch 2).
  - `intervention.json` - the sanitized routed request (reason `HUMAN_REQUESTED`, structural
    landmark `Identity Verification Required`, no screenshot, no member id).
  - `member_lookup.v1.json` - the capability this handoff run produced.
- **`replay_handoff/`** - replay-side handoff: replay meets an unexpected modal it cannot classify
  (`UNKNOWN_DIALOG`), pauses, and a human resolves it on the **same live session** before automation
  reconciles and completes.
  - `intervention.json` - the sanitized request (reason `UNKNOWN_DIALOG`, structural landmarks,
    no screenshot).
  - `actions.jsonl` - the audited ownership transfers (`automation → human → automation`, epochs 1 → 2).
  - `result.json` - `success` after handback, `model_calls = 0`, output masked.
- **`replay_mutation_handoff/`** - the mutation takeover (`verification_dialog`): the commit
  succeeds but the independent read is blocked by a dialog → `MUTATION_AMBIGUOUS`; a human takes the
  **same session**, clears the blocker, and resumes; automation re-runs **only the read-only
  verification** (never the write) and completes.
  - `actions.jsonl` - the ownership transfers.
  - `result.json` - `success` after recovery, `model_calls = 0` (the commit is never re-dispatched).

## Reproduce

All of the above is regenerated by following [`../HOW_TO_DEMO.md`](../HOW_TO_DEMO.md) top to
bottom (discovery needs a model key; replay needs none). Discovery is a live model run, so exact
step counts vary slightly between runs; every claim above is what a fresh run produces in shape.
