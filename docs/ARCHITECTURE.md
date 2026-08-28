# Architecture

This is the deep system-design reference. [`REPORT.md`](../REPORT.md) is the concise
required write-up; this document explains how the pieces actually fit and points at the
code, tests, and evidence that back each claim.

## System at a glance

An LLM is the right tool for *discovering* an unfamiliar workflow and the wrong authority
for *repeating* it. This system splits those roles: a model drives discovery behind a
trusted execution boundary, a successful run is compiled into a typed reusable capability,
and production replay executes that capability with no model in the decision loop.

```
 natural-language goal + typed inputs
        │
        ▼
 ┌───────────────────── discovery (probabilistic) ─────────────────────┐
 │  model ──ProposedAction──▶ TrustedKernel ──▶ live Surface           │
 │    ▲                          │                    │                 │
 │    └──── minimized observation ◀── verified observation ◀───────────┘│
 │                               │                                      │
 │                          successful trace                            │
 └───────────────────────────────┼──────────────────────────────────────┘
                                  ▼
                             compiler  ──▶  typed Capability (execution IR)
                                                    │
 ┌──────────────────── replay (deterministic, model-free) ─────────────┐
 │  Capability + inputs ──▶ ReplaySession ──▶ same TrustedKernel ──▶    │
 │  Surface ──▶ observed postcondition ──▶ RunResult                    │
 └──────────────────────────────────────────────────────────────────────┘
```

Three regimes, deliberately separated:

- **Probabilistic discovery** - a real model proposes one typed action at a time.
- **Deterministic authority** - `TrustedKernel` decides what may execute, in both
  discovery and replay. Only the *proposer* changes between the two.
- **Deterministic replay** - no model participates; `model_calls = 0`.

Stack: Python 3.12, Pydantic v2 (typed contracts), Playwright (the one concrete surface),
Typer CLI (`cua`), FastAPI/Jinja for the synthetic target. Single-process orchestration with
async browser and model I/O.

## Authority and trust boundary

The load-bearing decision is a boundary between a probabilistic *proposer* and a
deterministic *executor*. A model proposal never reaches the browser directly; it crosses
one trusted path (`src/computer_use/execution/kernel.py`):

```
 model proposal / compiled step
   → schema validation        (typed ProposedAction / Step)
   → policy & scope           (safety/policy.py, safety/navigation.py)
   → control ownership         (execution/lease.py - automation must own the session)
   → target resolution         (surface - exactly one match, else fail closed)
   → risk & authorization      (safety/risk.py, execution/approval.py)
   → Surface dispatch          (the single side-effect boundary)
   → observable verification   (postcondition on the live page)
   → safe evidence             (observability - allowlisted, sanitized)
```

The principle: **model output does not directly cause a browser side effect, and page
content is untrusted data.** A control's on-page text is used to *locate* a control, never
to expand scope or authorize a write. Risk is derived by software from the resolved control
(`safety/risk.py`), never read from a field the model or artifact supplies.

## Core invariants

Each is enforced by the final implementation; the clause after it is the failure it
prevents.

1. **The model proposes; trusted software executes.** Prevents a hallucinated or injected
   action from causing an effect that policy would forbid.
2. **Replay invokes no model for decisions** (`model_calls = 0`, runs with the key absent).
   Prevents non-determinism and unreviewable production behavior.
3. **Observed state determines progress.** A returned driver call is not success; a
   postcondition must hold. Prevents "the click returned, therefore it worked."
4. **Target ambiguity fails closed** (`LOCATOR_AMBIGUOUS`). Prevents acting on the wrong
   control when more than one matches.
5. **Consequential writes are never blindly retried after uncertain dispatch.** Prevents a
   double-write when a commit's response is lost.
6. **Evidence is minimized and sanitized before persistence.** Prevents regulated data from
   reaching disk.
7. **Human and automation never simultaneously own action authority** (`ControlLease` +
   epoch). Prevents split-brain control of one live session.

## Discovery and compilation

Discovery (`src/computer_use/discovery/agent.py`) runs a bounded `observe → decide → act`
loop against the live surface:

- **Bounded structured observation** - the page is harvested into candidate controls, then
  *minimized* before egress: the model receives roles, accessible names, and table
  row/column labels, but table-cell text is omitted, so rendered financial cell values are
  not sent through the candidate inventory (`discovery/model.py`, `discovery/agent.py::_minimize`).
- **Ephemeral candidate IDs** - the model may only target a `c1…cN` id from the current
  observation, so it cannot invent a selector.
- **Typed `ProposedAction`** - one action per turn (`model/proposals.py`). Meta-signals
  (`declare_success`, `request_human`) are handled by the runtime, not dispatched to the
  surface; the executable vocabulary is click / type / extract.
- **Kernel-executed** - every proposal crosses the same `TrustedKernel` used by replay.
- **Success is ratified, not asserted** - a declared success is accepted only if the
  declared output was actually extracted.

A successful run yields a `DiscoveryTrace`, which the compiler
(`src/computer_use/discovery/compiler.py`) normalizes into a `Capability`.

> **trace ≠ artifact.** The artifact is execution IR - semantic controls and symbolic
> inputs - not a recording of the model transcript. That is what makes it replayable for a
> different input and reviewable by a human or a calling agent.

## Capability artifact

The schema (`src/computer_use/model/artifact.py`, `model/values.py`) is designed as reusable
knowledge extracted from a run.

```
Capability
├─ schema_version, id, version, target (vendor/application_family)
├─ inputs:  { name → InputSpec(type, sensitivity) }
├─ outputs: { name → OutputSpec(type, sensitivity, currency?) }
├─ steps: [ Step
│            ├─ action           (compiled subset: click | type | extract)
│            ├─ target           (TargetDescriptor: role+name | label | text | table_cell)
│            ├─ value            (ValueRef, for type actions)
│            ├─ risk             (RiskClass, software-derived at runtime)
│            ├─ postcondition    (Condition)
│            ├─ outcomes         ([Outcome] - first-class business outcomes)
│            └─ verification?    (MutationVerification, on the single write step)
│          ]
└─ success_checkpoint (Condition)
```

Design choices:

- **Parameterization** - a member id compiles to `ParameterRef(member_number)`, never the
  concrete value, so one discovery replays for another member.
- **Semantic target identity** - `TargetDescriptor` names a role + accessible name, a label,
  visible text, or a relational `table_cell` (`row_contains` / `column_header`), with
  ordered fallbacks. The environment has no test IDs or stable selectors, so a recorded CSS
  path would be the brittle choice.
- **Created provenance** - a written value is a `ValueRef` =
  `ParameterRef | SafeLiteral | SecretRef | DerivedValue`. Provenance is *created* at compile
  time rather than inferred later from a coincidental scalar; a secret is a `SecretRef` only
  the trusted runtime resolves.
- **Business outcomes are first-class** - "no such member" is an `Outcome`, not an error
  string.
- **Action vocabulary** - the schema defines `click | type | select | extract`; the current
  compiler emits, and the kernel executes, `click | type | extract` (a step with an
  unexecutable action is refused, `NOT_EXECUTABLE`).
- **Closed mutation topology** - validation rejects more than one verification step, or any
  step after it, so unsupported write shapes fail at load
  (`Capability._single_final_verification`).

`extra="forbid"` throughout: an artifact referencing an undeclared output or an ill-formed
condition fails to load rather than misbehaving at runtime.

## Deterministic replay and runtime outcomes

*Deterministic* means: **given the observed external state, predefined software chooses the
next branch; no model decides anything.** Determinism comes from stable semantic resolution
and observed verification, not replayed coordinates. Replay
(`src/computer_use/execution/session.py`) reports `model_calls = 0` and runs with the
discovery key absent.

```
 step executed → observe
   ├─ postcondition holds        → continue
   ├─ business-outcome detector  → BusinessOutcome   (e.g. MEMBER_NOT_FOUND)
   ├─ classified transient       → bounded retry, then continue or fail
   ├─ unknown blocking dialog    → Escalated (UNKNOWN_DIALOG)
   └─ checkpoint fails           → Failure (CHECKPOINT_FAILED, expected vs observed)
```

Results are a discriminated union (`src/computer_use/model/results.py`):

```
RunResult = Success | BusinessOutcome | Escalated | Failure
```

`Success` / `BusinessOutcome` / recoverable-then-continue / `Failure` map onto the error
taxonomy of expected outcomes, recoverable conditions, and hard failures; `Escalated` is a
distinct fourth outcome - automation deliberately
transferring authority to a human. Every supported success path evaluates the capability's
declared `success_checkpoint` before returning `Success`; the mutation path is no exception.
Target resolution requires exactly one match, else `LOCATOR_AMBIGUOUS`; zero triggers ordered
fallbacks, then `TARGET_MISSING`.

## Consequential-write correctness

Capability B (`member.open_sub_account`) is a deep example of the invariants, not a second
architecture. The dangerous case is a commit whose response is lost: a returned exception
does **not** mean the write failed.

```
 pre-write baseline (effect absent?)         ── attribution
   → operation-scoped authorization           ── operator sanctions this exact write
   → dispatch exactly once                     ── never wrapped in transient retry
   → response certain OR uncertain
        → independent read-only verification   ── re-derive the effect by the same parameter
             → effect present, baseline absent → COMMITTED  → Success
             → absent on authoritative source  → NOT_COMMITTED (MUTATION_NOT_COMMITTED)
             → cannot be established            → AMBIGUOUS  → Escalated (never a guess)
```

A commit is attributed only as a *baseline-absent → present* transition. Whether an absent
effect is an authoritative non-commit is a trusted-runtime policy
(`safety/authority.py`), never a flag in the artifact.

Authorization differs between discovery and replay, on purpose:

- **Discovery** - the kernel raises a typed `ApprovalRequired`; a human returns a one-time,
  single-use `ApprovalGrant` bound to the operation's fingerprint (`execution/approval.py`).
- **Replay** - there is no interactive prompt. The operator sanctions the write by running
  the command against the approved artifact, expressed as an operation-scoped
  `ConfirmationPolicy` (`safety/confirmation.py`). Verification steps are re-classified
  read-only at runtime, so a write can never be smuggled into a "verification."

Full detail in [`docs/mutation-design.md`](mutation-design.md).

## Human-in-the-loop control transfer

When a run cannot safely proceed, it escalates rather than guesses, and the human works the
*same* live session.

```
 AUTOMATION owns the session (ControlLease, epoch N)
   → blocked/stuck state (UNKNOWN_DIALOG | MUTATION_AMBIGUOUS | model request_human)
   → structured InterventionRequest (capability, step, reason, expected-vs-observed)
   → automation pauses and is fenced
   → AUTOMATION → HUMAN (epoch N+1)
   → human operates the SAME Surface/session; each action audited (values redacted)
   → HUMAN → AUTOMATION (epoch N+2)
   → runtime reconciles current state against the capability's checkpoints
   → resume / complete
```

`ControlLease` (`src/computer_use/execution/lease.py`) is the single authority token: the
kernel refuses to act unless it owns the lease at the current epoch, so work prepared under a
superseded epoch is rejected after a takeover. A *fresh* session would be incorrect - the
authentication, navigation, and in-flight workflow context live in the session the automation
was already driving. One control-transfer primitive serves both authorities: replay-runtime
(`UNKNOWN_DIALOG` / `MUTATION_AMBIGUOUS`) and the discovery model (`request_human`).

Full detail in [`docs/handoff-design.md`](handoff-design.md).

## Safety and evidence boundary

Guardrails run in trusted software, on the resolved control, before any effect:

- **Navigation scope** - a configured origin + route allowlist (`safety/navigation.py`),
  enforced frame-aware across every framed document, so an in-scope top page cannot mask an
  out-of-scope workspace iframe. The caller-supplied target cannot define its own scope.
- **Closed action vocabulary** - the model may execute only click / type / extract
  (`safety/policy.py`); everything else is refused.
- **Software risk + authorization** - consequential writes require authorization; irreversible
  actions are never auto-approved.
- **Data by construction** - invocation parameters are symbolic before model egress, secrets
  are withheld from the model, and observation candidates omit their cell text, so rendered
  financial cell values are not sent through the candidate inventory.

Evidence crosses an allowlisting boundary (`observability/evidence.py`): persistence accepts
only an allowlisted event shape, and the event builders supply symbolic or redacted values
for sensitive fields. This narrows the persistence surface and is exercised by the
evidence-safety tests, rather than being a universal taint checker.

```
 ephemeral runtime state (raw values, page text)
        │
        ▼  evidence safety boundary  (allowlist + redact BEFORE persistence)
        ▼
 persistable evidence  (route patterns, <financial>, <redacted>, structural snapshots)
```

The memorable pattern:

```
 uncertain target   → don't guess   (LOCATOR_AMBIGUOUS)
 uncertain mutation → don't retry   (MUTATION_AMBIGUOUS)
 uncertain evidence safety → persist less  (structural-only, never raw)
```

Where a rich visual signal cannot be shown safe, a structural snapshot is persisted instead
(`observability/evidence_policy.py`). This is a bounded trusted runtime, not an absolute
guarantee: the allowlist is origin/route/name-based, and there is no OCR-based PII detection
and no desktop sandboxing.

## Surface abstraction and evolution

**Implemented:** one concrete surface, `PlaywrightSurface`
(`src/computer_use/surface/playwright_surface.py`), driving `LegacyCore` - a hostile,
legacy-style iframe/table workstation with no test IDs - through semantic
`TargetDescriptor`s behind the `Surface` protocol (`surface/base.py`).

**Designed (not built):**

- A future surface preserves the artifact's semantic intent while replacing perception,
  resolution, and dispatch. The current `Surface` interface is itself web-oriented (routes,
  frames, URLs); a production desktop adapter, driving the accessibility tree or OS
  automation, would likely factor a smaller portable core out of these optional web-specific
  capabilities. A web-only concept an adapter cannot honor **fails explicitly** rather than
  degrading silently.
- **Multi-tenant reuse** is a binding problem, not a copying problem: one approved base
  vendor capability plus small, validated per-tenant/version binding overlays that specialize
  only narrow target identity or layout. Bindings cannot rewrite workflow semantics; a real
  flow change is re-discovery producing a new immutable capability version. Production replay
  never silently mutates an approved capability or binding.

Not claimed: desktop support, a tenant-routing control plane, or arbitrary visual PII
detection. None exists.

## Key trade-offs

| Decision | Chosen | Alternative | Reason | Accepted limitation |
|---|---|---|---|---|
| Surface | Playwright DOM/ARIA driver | Screenshot + coordinates only | Semantic targets survive legacy markup; coordinates are brittle | Web-shaped; a desktop adapter is future work |
| Targeting | Semantic role/name/table-cell | CSS/XPath-first | No test IDs in the environment; accessible identity is more stable | Needs a meaningful accessible name to exist |
| Resolution | Ordered strategies, fail-closed on ambiguity | Opaque best-score match | Reviewable and deterministic; never silently picks a near-match | A genuinely ambiguous page escalates instead of proceeding |
| Recovery | Bounded deterministic (transient retry, known interstitial) | Open-ended model fallback on failure | Keeps replay model-free and reviewable | Unmodeled states escalate rather than self-heal |
| Evidence | Allowlisted structural, redact-before-persist | Rich raw screenshots/DOM | Regulated-data minimization by construction | Less pixel-level detail on failure |
| Scope | One strong concrete surface | Many shallow adapters | Depth on the load-bearing seams | Only one surface proven end to end |
| Architecture | Single process, synchronous | Queues/services up front | Simpler, correct, reviewable | Not horizontally scaled (by design) |

## Code and proof map

| Concern | Primary code | Proof |
|---|---|---|
| Discovery loop | `discovery/agent.py`, `discovery/anthropic_model.py` | `tests/unit/test_agent_loop.py`; `tests/integration/test_live_discovery.py` (`--run-live`); `evidence/discovery/trace.jsonl` |
| Artifact + compiler | `model/artifact.py`, `model/values.py`, `discovery/compiler.py` | `tests/unit/test_artifact.py`, `test_artifact_validation.py`, `test_compiler.py`, `test_verification_compiler.py`; `evidence/capability/*.json` |
| Trusted execution | `execution/kernel.py`, `safety/policy.py`, `safety/risk.py`, `execution/approval.py` | `tests/unit/test_kernel.py`, `test_kernel_safety.py`, `test_kernel_approval.py` |
| Deterministic replay | `execution/session.py`, `execution/replay.py`, `execution/trace.py` | `tests/unit/test_replay_reliability.py`; `tests/integration/test_replay.py`, `test_eval_scenarios.py`; `evidence/replay_*` |
| Target resolution | `surface/playwright_surface.py`, `surface/base.py` | `tests/integration/test_locator_safety.py`, `test_surface.py`, `test_kernel_surface.py` |
| Mutation verification | `execution/session.py`, `execution/approval.py`, `safety/authority.py` | `tests/unit/test_mutation_verification.py`; `tests/integration/test_mutation.py`; `evidence/replay_mutation*`, `evidence/discovery_open_sub_account/verification_provenance.json` |
| Handoff / control lease | `handoff/operator.py`, `execution/lease.py`, `handoff/intervention.py` | `tests/unit/test_handoff.py`, `test_operator_resolution.py`; `tests/integration/test_handoff.py`, `test_discovery_handoff.py`; `evidence/*handoff*/` |
| Navigation scope | `safety/navigation.py` | `tests/unit/test_navigation_policy.py`, `test_kernel_safety.py` |
| Evidence safety | `observability/evidence.py`, `observability/evidence_policy.py` | `tests/unit/test_evidence_safety.py`, `test_egress.py`, `test_model_egress_no_scenario_leak.py`; `evidence/` |

The scenario → outcome matrix is in [`docs/eval-scenarios.md`](eval-scenarios.md); the proof
package is mapped in [`evidence/README.md`](../evidence/README.md).
