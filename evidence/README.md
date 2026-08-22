# Evidence

A genuine end-to-end run of the computer-use vertical slice against the local
LegacyCore workstation: LLM discovery → compiled capability → deterministic replay.

## Files

- `discovery/trace.jsonl` — the **genuine LLM discovery run** (provider `anthropic`,
  model `claude-sonnet-4-6`). Structured events only: no secrets, no raw member value
  (recorded symbolically as `<param:member_number>`), no financial values.
- `capability/member_lookup.v1.json` — the typed **capability** compiled from that run
  (`ParameterRef` provenance, a semantic `table_cell` target for the balance, an authored
  `MEMBER_NOT_FOUND` business outcome; no literal invocation values).
- `replay_success/result.json` — deterministic replay for a **different** member (`54321`):
  `success`, `savings_balance = 312.45`, `model_calls = 0`.
- `replay_business_outcome/result.json` — replay for an **unknown** member (`99999`):
  `business_outcome` `MEMBER_NOT_FOUND` — a legitimate domain answer, not a crash — with
  `model_calls = 0`.

## Reproduce

```bash
uv run legacy-core                     # terminal 1: the target app
export ANTHROPIC_API_KEY=...           # discovery only
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000

unset ANTHROPIC_API_KEY                # replay never needs a model
uv run cua replay artifacts/member_lookup.v1.json --param member_number=54321
uv run cua replay artifacts/member_lookup.v1.json --param member_number=99999
```
