# replayable-computer-use

LLM-discovered, typed computer-use capabilities that replay deterministically against legacy
banking UIs, with a software-owned trust boundary and safety guardrails.

The discovery → capability → replay vertical slice, the safety/redaction boundary, the
consequential-write (mutation) semantics, and same-session human-in-the-loop takeover are
implemented and tested. The design write-up - architecture, artifact schema, determinism and
error handling, heterogeneity and multi-tenant reuse, escalation and handoff, safety, and cuts -
is in [`REPORT.md`](REPORT.md). Deeper design notes are in
[`docs/handoff-design.md`](docs/handoff-design.md) and
[`docs/mutation-design.md`](docs/mutation-design.md); runnable demos with expected output are in
[`HOW_TO_DEMO.md`](HOW_TO_DEMO.md) and [`docs/demo-handoff.md`](docs/demo-handoff.md).

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
uv run playwright install chromium   # the browser the agent drives
```

## Run the tests

```bash
uv run pytest
```

## Run the LegacyCore demo app

LegacyCore is the synthetic credit-union employee workstation the agent operates.

```bash
uv run legacy-core
```

Then open http://localhost:8000. LegacyCore doubles as a deterministic eval environment: a
`scenario` query parameter injects a reproducible world state (e.g. `?scenario=slow`,
`?scenario=unexpected_dialog`, `?scenario=session_expired`, `?scenario=permission_denied`,
`?scenario=not_found`), and each maps to a typed replay outcome. The full scenario → outcome
matrix is in [`docs/eval-scenarios.md`](docs/eval-scenarios.md). Ordinary product behaviour stays
data-driven - a member number with no record (e.g. `99999`) yields a "Member record not found".

## Configuration

Copy the example environment file and fill in values as needed:

```bash
cp .env.example .env
```

Secrets are never committed. See `.env.example` for the variables the project reads.

## Demo path

Discovery runs a genuine LLM against the live app and needs a model key; replay never does.

```bash
uv run legacy-core                       # terminal 1: the target app

# genuine discovery -> writes evidence/capability/member_lookup.v1.json
export ANTHROPIC_API_KEY=...
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000

# deterministic replay with a different input, no model in the loop (model_calls = 0)
unset ANTHROPIC_API_KEY
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=99999
```

Without a model key you can still replay the committed artifact at
`evidence/capability/member_lookup.v1.json`.

## Human handoff

When a run cannot safely proceed on its own, it pauses and a human takes over the *same* live
session, resolves the blocker, and hands control back; the runtime reconciles observable state
before resuming. Two runnable demos (add `--headed` to watch the browser):

```bash
uv run legacy-core                        # terminal 1: the target app

# Replay meets an unexpected modal it cannot classify (deterministic, no model key):
uv run cua handoff-demo --headed
#   operator ❯ take  ->  click c1  ->  resume   (c1 = the blocker's Acknowledge; model_calls = 0)

# A live discovery model asks for a human on a flagged account (needs a model key):
uv run cua discover --headed --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345
#   operator ❯ take  ->  submit c1  ->  resume   (enter the code at the masked prompt)
```

See [`docs/demo-handoff.md`](docs/demo-handoff.md) for the full walkthrough with expected
output and evidence, and [`docs/handoff-design.md`](docs/handoff-design.md) for the design.
