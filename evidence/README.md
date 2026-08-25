# Evidence

A genuine end-to-end run of the computer-use vertical slice against the local
LegacyCore workstation: LLM discovery → compiled capability → deterministic replay.

## Files

- `discovery/trace.jsonl` — the **genuine LLM discovery run** (provider `anthropic`,
  model `claude-sonnet-4-6`). Structured events only: no secrets, no raw member value
  (recorded symbolically as `<param:member_number>`), no financial values, and no raw
  natural-language goal (recorded as `goal_present`).
- `capability/member_lookup.v1.json` — the typed **capability** compiled from that run
  (`ParameterRef` provenance, a semantic `table_cell` target for the balance, an authored
  `MEMBER_NOT_FOUND` business outcome; no literal invocation values).
- `replay_success/result.json` — deterministic replay for a **different** member (`54321`):
  `success`, `model_calls = 0`. The financial output is **masked in persisted evidence**
  (`savings_balance = <financial>`); the caller receives the raw typed value from the
  returned result on stdout.
- `replay_business_outcome/result.json` — replay for an **unknown** member (`99999`):
  `business_outcome` `MEMBER_NOT_FOUND` — a legitimate domain answer, not a crash — with
  `model_calls = 0`.
- `discovery_handoff/` — a **genuine discovery-side handoff** (`anthropic`, `claude-sonnet-4-6`):
  the live model looks up a flagged account, finds it needs an employee verification credential
  it was never given, and — from its normal action schema — proposes `request_human`. A human
  enters the code on the *same* live session and the model resumes to `GOAL_REACHED`.
  - `trace.jsonl` — the model's run: `intervention_raised` (`HUMAN_REQUESTED`, with the
    model-call index), the `automation → human → automation` transfers with a monotonic epoch,
    and the human action with the code redacted (`<redacted>`). No goal text, member id,
    balance, or code — the escalation itself was the real model's decision (`model_calls > 0`),
    while production replay of a compiled capability stays `model_calls = 0`.
  - `intervention.json` — the sanitized `InterventionRequest` (reason `HUMAN_REQUESTED`,
    structural landmark `Identity Verification Required`, no screenshot, no PII).

  This scenario intentionally exercises the general **same-session takeover** path (exclusive
  control, audited human action, reconciliation). A known verification-code requirement is a
  natural structured `INPUT_REQUIRED` intervention and in production would normally be handled
  as a typed request without transferring browser control; live takeover is reserved for states
  that cannot be represented safely as a typed request. See `docs/handoff-design.md`.
- `replay_handoff/` — a **same-session human handoff**: replay meets an unexpected modal it
  cannot classify, pauses, and a human resolves it on the *same* live session before
  automation resumes.
  - `intervention.json` — the sanitized `InterventionRequest`: reason `UNKNOWN_DIALOG`, the
    pending step, current ownership, a structural route label, and structural landmarks
    (`System Notice`, `Member Profile`) — no member id, no financial value, no screenshot.
  - `actions.jsonl` — the audited control transfers and the human action. Ownership moves
    `automation → human → automation` with a **monotonic control epoch** (0 → 1 → 2); the
    human action records a structural target fingerprint (`link:Acknowledge`) with any typed
    value redacted — never the raw value.
  - `result.json` — the final `success` after handback with `model_calls = 0`; the financial
    output is masked (`savings_balance = <financial>`) as in every persisted result.

## Reproduce

```bash
uv run legacy-core                     # terminal 1: the target app
export ANTHROPIC_API_KEY=...           # discovery only
rm -f evidence/discovery/trace.jsonl   # the evidence store appends; start fresh
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000

unset ANTHROPIC_API_KEY                # replay never needs a model
# stdout returns the raw result to the caller; --evidence-out writes masked evidence
uv run cua replay evidence/capability/member_lookup.v1.json \
  --param member_number=54321 --evidence-out evidence/replay_success/result.json
uv run cua replay evidence/capability/member_lookup.v1.json \
  --param member_number=99999 --evidence-out evidence/replay_business_outcome/result.json

# same-session human handoff (headed so a human can watch/act); the operator types
# take -> ack -> resume. Piping those keystrokes reproduces the recorded evidence:
printf 'take\nack\nresume\n' | uv run cua handoff-demo --headless \
  --evidence-out evidence/replay_handoff

# discovery-side handoff (needs ANTHROPIC_API_KEY): the live model asks for a human
# on a flagged account; the operator enters the employee code, and the model resumes.
export ANTHROPIC_API_KEY=...            # or place it in a local .env (git-ignored)
printf 'take\nsubmit Employee Verification Code=4729\nresume\n' | \
  uv run cua discover --headless --scenario verification_required \
  --goal "Look up this member and return their current savings balance" \
  -p member_number=12345 \
  --evidence evidence/discovery_handoff/trace.jsonl \
  --out /tmp/discovered_handoff.json
```
