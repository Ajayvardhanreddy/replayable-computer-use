# replayable-computer-use

LLM-discovered, typed computer-use capabilities with deterministic replay, safety guardrails,
and same-session human handoff for legacy banking UIs.

> **Status: bootstrap / under construction.** The end-to-end discovery → capability → replay
> flow is not implemented yet. This README documents only what currently exists.

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

_To be added once the discovery and replay CLI lands._
