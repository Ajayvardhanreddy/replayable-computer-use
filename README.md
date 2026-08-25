# replayable-computer-use

LLM-discovered, typed computer-use capabilities that replay deterministically against legacy
banking UIs, with a software-owned trust boundary and safety guardrails.

The discovery → capability → replay vertical slice, the safety/redaction boundary, and
same-session human-in-the-loop takeover are implemented and tested. The escalation and
control-transfer design is written up in [`docs/handoff-design.md`](docs/handoff-design.md);
runnable handoff demos with expected output are in
[`docs/demo-handoff.md`](docs/demo-handoff.md).

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
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

Then open http://localhost:8000. Deterministic runtime scenarios can be requested with a
`scenario` query parameter, e.g. `http://localhost:8000/?scenario=slow` or
`http://localhost:8000/?scenario=unexpected_dialog`. A member number with no record (e.g.
`99999`) yields a "Member record not found" result.

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

# genuine discovery -> writes artifacts/member_lookup.v1.json
export ANTHROPIC_API_KEY=...
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000

# deterministic replay with a different input, no model in the loop (model_calls = 0)
unset ANTHROPIC_API_KEY
uv run cua replay artifacts/member_lookup.v1.json --param member_number=54321
uv run cua replay artifacts/member_lookup.v1.json --param member_number=99999
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
#   operator> take  ->  ack  ->  resume        (ends model_calls = 0, balance returned)

# A live discovery model asks for a human on a flagged account (needs a model key):
uv run cua discover --headed --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345
#   operator> take  ->  submit Employee Verification Code=4729  ->  resume
```

See [`docs/demo-handoff.md`](docs/demo-handoff.md) for the full walkthrough with expected
output and evidence, and [`docs/handoff-design.md`](docs/handoff-design.md) for the design.
