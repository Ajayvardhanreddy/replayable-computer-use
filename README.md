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

## Configuration

Copy the example environment file and fill in values as needed:

```bash
cp .env.example .env
```

Secrets are never committed. See `.env.example` for the variables the project reads.

## Demo path

_To be added once the discovery and replay CLI lands._
