# Deterministic eval scenarios

LegacyCore doubles as an eval environment: a `?scenario=` switch (query wins → cookie →
`normal`, fail-closed on anything unknown) injects a specific, reproducible world state, and the
model-free runtime (`model_calls = 0`) classifies each into a typed `RunResult` with safe
evidence. The runtime never keys on app-specific copy - it classifies generically (a bound
business outcome, an unmet postcondition, a blocking modal, or a mutation-completion state), so
the same runtime would behave the same way against a different application.

Ordinary product behaviour stays data-driven (an unknown member number naturally has no record);
the scenario switch only gives the eval runner one consistent interface across world states.

| Scenario | Injected world state | Typed outcome | Proof |
|---|---|---|---|
| `normal` | member profile renders | `Success` - savings balance extracted | `test_replay.py` |
| `not_found` | lookup returns no record (any id) | `BusinessOutcome MEMBER_NOT_FOUND` | `test_eval_scenarios.py` |
| `slow` | bounded response delay | `Success` - bounded transient polling recovers | `test_replay.py` (timing), `test_replay_reliability.py` |
| `session_expired` | expired-session page instead of profile | `Failure CHECKPOINT_FAILED` - observed `Session Expired` | `test_eval_scenarios.py` |
| `permission_denied` | access-denied page instead of profile | `Failure CHECKPOINT_FAILED` - observed `Access Denied` | `test_eval_scenarios.py` |
| `unexpected_dialog` | an unmodeled blocking modal | `Escalated UNKNOWN_DIALOG` → same-session human takeover | `test_handoff*.py`, `evidence/replay_handoff/` |
| `verification_required` | flagged account gated behind an employee credential | discovery `request_human` → same-session human takeover | `test_discovery_handoff.py`, `evidence/discovery_handoff/` |
| `commit_then_timeout` | write commits, response withheld past the bounded timeout | dispatch **once** → independent read-back → `Success` | `test_mutation.py` |
| `commit_ambiguous` | write commits, immediate ambiguous page | read-back confirms → `Success` (no double-write) | `test_mutation.py` |
| `commit_dropped` | ambiguous page, no commit | authoritative absence → `Failure MUTATION_NOT_COMMITTED` | `test_mutation.py` |
| `commit_unverifiable` | write commits, read-back unrenderable | `Escalated MUTATION_AMBIGUOUS` → routed handoff case | `test_mutation.py`, `evidence/replay_mutation_ambiguous/` |
| `verification_dialog` | post-commit read-back blocked by a recoverable dialog | `Escalated MUTATION_AMBIGUOUS` → same-session takeover → re-verify (write never re-dispatched) | `test_mutation.py`, `evidence/replay_mutation_handoff/` |

## Notes on the mapping

- **`session_expired` and `permission_denied`** both surface as `CHECKPOINT_FAILED` because the
  model-free runtime detects "the expected postcondition was not observed" - a generic, typed
  failure - and the *specific* state is preserved in the result's `observed` detail and the
  sanitized failure evidence (`Session Expired` / `Access Denied`). No app-coupled classification
  and no new failure code are introduced; the distinction lives in the evidence, not in bespoke
  runtime logic.
- **`verification_required` is not `permission_denied`.** The former is *resolvable* by supplying
  a credential (it drives the discovery-side human takeover); the latter is an authorization
  refusal for this session. They are kept semantically separate.
- **`not_found` stays a business outcome, not a failure** - an absent record is a legitimate
  domain answer, not a system error. The scenario switch merely forces it deterministically.
