# Consequential Mutation Design

## 1. The problem

The dangerous case for automating a legacy banking UI is not layout drift - it is a
consequential write whose completion signal is lost:

```
click "Create Account"  ->  server commits  ->  response never arrives (timeout)
```

The client cannot assume the write failed. Retrying may double-submit; treating it as a
hard failure hides a change that actually happened. The runtime must instead establish
what really occurred.

## 2. Commit boundary and confirmation

Risk is software-derived, never taken from the artifact. Preparatory clicks (open the
form, continue to review) are read-only and run unattended; only the **commit** click is
`CONSEQUENTIAL_WRITE`, and it is dispatched only against an explicit human authorization:

- while a capability is being authored, a one-time approval bound to the proposed
  action's target fingerprint and observable state, re-validated immediately before
  dispatch (a stale approval, if the page moved, is refused);
- during deterministic replay, an operation-scoped approval keyed to
  `capability : version : step`.

The model can never authorize itself, and without approval nothing is dispatched.

## 3. Effect certainty - never blindly retry

The dispatch boundary is the click call itself. Everything before it - resolution,
policy, confirmation, ownership - precedes any side effect, so a failure there is
definitely `NOT_DISPATCHED` and safe to fail normally. The moment the consequential click
is invoked, the effect may have reached the application:

```
EffectState:  NOT_DISPATCHED -> DISPATCHING -> DISPATCHED
                                                    |
                          committed / not_committed / ambiguous  (established by verification)
```

A consequential dispatch is executed exactly once and is **never** wrapped in the generic
transient-retry: a failure at the dispatch call raises a non-retryable "uncertain" signal
from the trusted kernel, so no code path can re-issue it.

## 4. Independent verification - discovered, not assumed

The write's effect is confirmed by an **independent read the model discovered while
reaching the goal**, not by trusting the commit's own response. When the goal requires
confirmation, the model re-derives the member's state after the commit - returning through
the persistent navigation, re-querying the member by the same parameter, reaching the
accounts view - and observes the effect there. The compiler captures that read-only
sub-trace as an **embedded verification recipe** on the consequential step: one flat,
read-only sequence, never a nested workflow.

At replay the recipe is re-executed (model-free) through the same kernel, under a mode
that independently refuses any step whose software-derived risk is above `READ_ONLY`. So a
malformed or edited artifact can never turn verification into another write. The compiler
enforces the same read-only rule when it builds the recipe - defense in depth.

## 5. Attribution - a transition, not mere presence

A row being present *after* the write does not prove *this* write created it (it may have
pre-existed). Attribution requires a **transition**: a trusted matcher evaluates the
effect on its view *before* dispatch (the baseline), and a commit is credited only when the
effect was **absent before and present after** the independent read.

```
effect present, baseline was absent            -> COMMITTED     -> Success
effect absent, read source is authoritative     -> NOT_COMMITTED -> Failure(MUTATION_NOT_COMMITTED)
effect not establishable, or baseline unknown    -> AMBIGUOUS     -> Escalated(MUTATION_AMBIGUOUS)
```

Whether an *absent* effect is a definite non-commit - versus a source that may lag the
write - is a property of the environment, decided by trusted runtime configuration, never
serialized in the artifact. The conservative default is that absence is ambiguous.

## 6. Result mapping (the four-shape contract is preserved)

| Outcome | Result |
|---|---|
| success confirmation, or verification finds the effect (absent → present) | `Success` |
| explicit application rejection (e.g. already exists) | `BusinessOutcome` |
| pre-dispatch failure (resolution / policy / no approval) | `Failure` |
| dispatched, verification proves absent on an authoritative source | `Failure(MUTATION_NOT_COMMITTED)` |
| dispatched, effect not establishable | `Escalated(MUTATION_AMBIGUOUS)` |

## 7. Escalation and human handoff

An ambiguous mutation is never a dead end. The escalation carries a **full, sanitized
handoff case** - the capability, the step, why it stopped, that the write was dispatched
exactly once and neither retried nor assumed, and what remains to confirm - so a human can
act with full context and only structural state (no member id, no amount).

Two paths follow, both reusing the same control-lease handoff seam:

- **Recoverable, in-session:** if the block is something a human can clear on the live
  session (for example an unexpected dialog on the read path), the operator takes exclusive
  control of the *same* session, clears it, and resumes. Automation then re-runs **only the
  read-only verification** - never the write - and completes.
- **Out of band:** if the read cannot be established at all, the unattended runner emits the
  handoff case and stops. A human resolves it with access the intentionally boxed-in agent
  does not have (retry the read, or check the system of record directly), then closes it.

Either way the write is dispatched exactly once, and the system never reports a success it
could not observe.

## 8. Idempotency

Application-supported idempotency keys are the best defense against duplicate writes - when
available. A legacy UI reached only by driving the browser has no such key, and inventing an
API-only mechanism would not reflect the real environment. When idempotency is unavailable,
**dispatch-once plus independent verification** is the load-bearing mechanism: dispatch once,
then establish the effect through observation rather than assumption.

## 9. Scope

This is the local correctness primitive for one uncertain write. It is not a transaction
manager, saga, command bus, or ledger. Durable coordination and cross-service consistency
would build on the same `EffectState` and verification contract at production scale.
