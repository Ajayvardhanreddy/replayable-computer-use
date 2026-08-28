# replayable-computer-use

**A model discovers how to operate an API-less banking UI; that behavior is compiled into a
typed, reusable capability that replays deterministically with no model in the loop -
safely, and with a human able to take over the same live session.**

The target is **LegacyCore**, a synthetic credit-union employee workstation built to be
hostile in the way real back-office apps are: an iframe workstation, dense non-semantic
tables, link-style actions, and no test IDs. It stands in for the long tail of legacy
banking software that offers no API, only a UI.

## What this system does

- **Genuine LLM discovery** - a real model runs an `observe → decide → act` loop against the
  live app until the goal is met.
- **Compiled typed capability** - a successful run becomes a versioned, parameterized
  artifact of semantic controls and symbolic inputs, not a model transcript.
- **Model-free deterministic replay** - the capability re-runs with different inputs and no
  model decisions (`model_calls = 0`; no API key needed).
- **Explicit outcomes** - every run returns `Success | BusinessOutcome | Escalated | Failure`;
  "no such member" is a business outcome, not a crash.
- **Trust boundary + safety** - the model proposes, but scope, risk, target resolution, and
  success are decided by trusted software. Secrets are withheld, model-facing observations
  minimize known sensitive values, and persisted evidence is allowlisted and sanitized.
- **Same-session human handoff** - when a run can't safely proceed, a human takes over the
  *same* live browser session, acts, and hands control back.

## System at a glance

```
 goal + inputs ─▶ discovery model ─proposes▶ TrustedKernel ─▶ live Surface
                        ▲                          │                │
                        └──── minimized view ◀──────  verified observation
                                                   │
                                            successful trace
                                                   ▼
                                       compiler ─▶ typed Capability
                                                   │
   Capability + inputs ─▶ ReplaySession ─▶ TrustedKernel ─▶ Surface ─▶ RunResult
   (deterministic, model_calls = 0)
```

The full design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`REPORT.md`](REPORT.md).

## Setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium   # the browser the agent drives
```

Genuine discovery and discovery-side handoff need a model key (`ANTHROPIC_API_KEY`);
**deterministic replay and the replay-side handoff demos need no key.** Copy `.env.example`
to `.env` if you want to set one; secrets are never committed.

## Quick start - prove the production path in ~2 minutes (no model key)

A capability produced by a genuine discovery run is already committed at
`evidence/capability/member_lookup.v1.json` (with the corresponding live-run trace under
`evidence/discovery/`), so you can exercise the deterministic production path immediately,
without a model.

```bash
# terminal 1
uv run legacy-core                       # the target app on http://localhost:8000

# terminal 2 - replay the committed capability for a DIFFERENT member, no model in the loop
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321
# → {"status":"success", ..., "outputs":{"savings_balance":"312.45"}, "model_calls":0}

# an unknown member is a first-class business outcome, not a crash
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=99999
# → {"status":"business_outcome","code":"MEMBER_NOT_FOUND", ..., "model_calls":0}
```

`model_calls: 0` and a different input than discovery used are the point: the capability
generalized, and no model decided anything.

## Full path - discovery → capability → replay (needs a model key)

```bash
uv run legacy-core                       # terminal 1

export ANTHROPIC_API_KEY=...             # terminal 2
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000 --headed
# a real model drives the UI, then writes evidence/capability/member_lookup.v1.json
# (+ a sanitized run log at evidence/discovery/trace.jsonl)

unset ANTHROPIC_API_KEY
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321
# same capability, new input, model_calls: 0 → the discovery generalized
```

## Demo catalog

Add `--headed` to any command to watch the real browser. Full step-by-step with expected
output is in [`HOW_TO_DEMO.md`](HOW_TO_DEMO.md).

| Demo | Demonstrates | Model key? | Command |
|---|---|:---:|---|
| Discovery | genuine model `observe→decide→act` | yes | `cua discover --goal "…" --param member_number=12345 --headed` |
| Keyless replay | deterministic, `model_calls = 0` | no | `cua replay evidence/capability/member_lookup.v1.json --param member_number=54321` |
| Business outcome | `MEMBER_NOT_FOUND`, not a crash | no | `cua replay evidence/capability/member_lookup.v1.json --param member_number=99999` |
| Runtime scenarios | typed failures for injected states | no | `cua replay …member_lookup.v1.json --param member_number=54321 --scenario session_expired` |
| Consequential write | dispatch-once + verified commit under lost response | no | `cua reset-demo` then `cua replay evidence/capability/open_sub_account.v1.json --param member_number=54321 --capability open_sub_account --scenario commit_then_timeout --commit-timeout-ms 300` |
| Replay handoff | same-session takeover on an unclassifiable dialog | no | `cua handoff-demo --headed`  → `take` → `click c1` → `resume` |
| Mutation handoff | takeover, then read-only re-verify (write not re-dispatched) | no | `cua reset-demo` then `cua handoff-demo evidence/capability/open_sub_account.v1.json --param member_number=54321 --capability open_sub_account --scenario verification_dialog --headed` → `take` → `click c1` → `resume` |
| Discovery handoff | a live model asks for a human, resumes after | yes | `cua discover --headed --scenario verification_required --goal "…" --param member_number=12345` → `take` → `submit c2` → `resume` |

`c1` is the blocker's **Acknowledge**; `c2` is the **Employee Verification Code** field
(entered at a masked prompt, never typed inline). Run `cua reset-demo` before re-running any
write demo.

## LegacyCore

`LegacyCore` (`demo_app/legacy_core/`) is a synthetic credit-union workstation - deliberately
legacy-hostile (server-rendered iframe shell, table-based account grids, link actions, no test
IDs) so the semantic-targeting and robustness strategy is pressured, not flattered. It uses
only synthetic records and no real customer data.

It doubles as a deterministic eval environment: a `?scenario=` switch injects reproducible
world states (`slow`, `unexpected_dialog`, `session_expired`, `permission_denied`,
`not_found`, and the commit-uncertainty family), each mapping to a typed replay outcome. The
full matrix is in [`docs/eval-scenarios.md`](docs/eval-scenarios.md). Ordinary product
behavior stays data-driven - a member number with no record (e.g. `99999`) yields
`MEMBER_NOT_FOUND`.

## Tests, lint, types

```bash
uv run pytest                              # unit + browser integration (replay is keyless)
uv run ruff check src/ tests/ demo_app/
uv run mypy
```

Live-model discovery tests are opt-in (`--run-live`) so the ordinary suite needs no key.

## Evidence

`/evidence/` is a curated proof package - the committed discovery runs, compiled artifacts,
replay logs (`result.json` + a structural `trace.jsonl`), and completed same-session
handoffs. What each directory proves is mapped in
[`evidence/README.md`](evidence/README.md).

## Documentation

| File | Purpose |
|---|---|
| [`REPORT.md`](REPORT.md) | Concise required design write-up (the seven brief headings) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Deep system architecture, invariants, and code/proof map |
| [`HOW_TO_DEMO.md`](HOW_TO_DEMO.md) | Complete demo guide with expected output |
| [`docs/mutation-design.md`](docs/mutation-design.md) | Consequential-write correctness deep dive |
| [`docs/handoff-design.md`](docs/handoff-design.md) | HITL control-transfer design |
| [`docs/eval-scenarios.md`](docs/eval-scenarios.md) | Deterministic scenario → outcome matrix |
| [`evidence/README.md`](evidence/README.md) | Proof-package map |
