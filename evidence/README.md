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
```
