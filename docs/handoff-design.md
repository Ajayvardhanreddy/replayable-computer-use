# Human Intervention Design

> **Scope:** the design and semantics of same-session control transfer - detection,
> intervention context, the `ControlLease`/epoch ownership model, and reconcile-before-resume.
> For the runnable commands see [`../HOW_TO_DEMO.md`](../HOW_TO_DEMO.md); for how this fits the
> whole system see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## 1. The idea

Automation cannot always finish on its own. When it stops, the question is simple: does
the human need to *answer something*, or *do something*?

Most of the time the human just supplies one piece of information or makes one decision -
a code, an approval, a choice - handled asynchronously, without anyone touching the
browser. Occasionally the state is one the automation cannot represent at all, and a human
must operate the live session directly. That second case is the hard one, and it is what
this repository implements end to end.

```
                        automation is blocked
                                 |
                     what does the human need?
                                 |
                +----------------+----------------+
                |                                 |
          a specific answer               direct UI control
                |                                 |
                v                                 v
        STRUCTURED INTERVENTION              LIVE TAKEOVER
        input / approve / choose                  |
                |                        lease: automation -> human
         no browser handoff                       |
                |                       human operates the same session
                v                                 |
             resume                               v
                                            reconcile state
                                                  |
                                                  v
                                                resume
```

> Human intervention is a control-plane capability, not a remote-desktop feature:
> structured decisions handle the common case; live takeover uses fenced ownership of the
> same session and state reconciliation before automation resumes.

## 2. Two ways to involve a human

Conceptually, an `Intervention` is one typed, sanitized record, and the space of reasons
falls into four kinds that differ only in whether control of the browser changes hands:

| Kind | Example | Browser control transfers? |
|---|---|---|
| `INPUT_REQUIRED` | "enter the employee verification code" | No |
| `APPROVAL_REQUIRED` | "approve this $5,000 release" | No |
| `CHOICE_REQUIRED` | "which of these two matching member records?" | No |
| `LIVE_TAKEOVER_REQUIRED` | novel modal / unmodeled branch / visual challenge | Yes |

The first three pause, ask a typed question, take a typed answer, and resume. Only
`LIVE_TAKEOVER_REQUIRED` hands over the live session. Structured intervention is the default;
live takeover is the escape hatch, and it should shrink as failure modes become understood.

This repository implements the `LIVE_TAKEOVER_REQUIRED` kind (surfaced by two reason codes,
`UNKNOWN_DIALOG` and `HUMAN_REQUESTED`); the structured kinds above are the production model,
described here but not built (see Section 8).

## 3. What this repository implements

This repository implements the hard path - `LIVE_TAKEOVER_REQUIRED` - end to end on one
live session:

```
automation observes an unsupported state
        |
raise a sanitized InterventionRequest, pause
        |
hand control over:  automation -> human      (a human now owns the session)
        |
human resolves it on the same page
        |
hand control back:  human -> automation
        |
reconcile: does the live page match a known checkpoint?
        |
resume only if it does; otherwise stop
```

Three invariants make this safe:

- **One owner at a time.** A control lease (`automation | human`) is checked by the trusted
  kernel before every action, so the two can never drive the session at once.
- **Stale work cannot sneak back in.** Every handoff increments a counter (an *epoch*); an
  automation action created under an older epoch is rejected, so old queued work cannot
  regain control after a human has taken over.
- **Nothing sensitive is kept.** Evidence is allowlisted - action type, the control's
  structural identity, a route pattern, the ownership epoch, and redacted value metadata -
  never raw page text, record values, credentials, or secrets. A control name that could
  itself carry record data is redacted by tenant policy.

Two entry points reach this path: an unhandled blocking dialog during deterministic replay
(`UNKNOWN_DIALOG`), and a live discovery model that judges it lacks a required credential
and asks for a human (`HUMAN_REQUESTED`). The credential case is really an `INPUT_REQUIRED`
situation; it is used here to exercise the harder takeover path, since that is the
correctness-sensitive mechanism worth proving directly.

## 4. Scaling to production

The same primitive is externalized across many workers and institutions:

```
agent blocked
   -> Intervention Service      (durable, typed request)
   -> Router                    (by institution / app / skill / risk / SLA)
   -> operator queue            (one operator claims it; no double-claim)
        |-- structured answer  -> resume       (no browser handoff)
        '-- live takeover:
              -> Session Control   (durable lease + epoch, atomic handoff)
              -> Browser Gateway   (checks owner + epoch on every command)
              -> Live View         (short-lived, scoped viewer for one operator)
              -> operator drives the same session -> hand back -> reconcile
```

The one real change from the local version: today the epoch check runs inside a single
process; in production the **browser gateway** enforces it on every command, so even a
stale worker on another machine cannot act on the session. The lease itself becomes a
durable record with atomic ownership transfer (compare-and-set in the backing store) and
heartbeats; if a human's hold expires, the session becomes explicitly unowned and must be
reclaimed - never silently handed back to automation.

Structured interventions need none of this lease machinery - a typed request and a typed
answer through the same queue - which is why they are the production default: cheaper,
safer with regulated data, and horizontally scalable.

## 5. Regulated data and audit

An authorized employee seeing member data to do their job is normal; the risk is *keeping
and spreading* it. So the two concerns are separated:

```
LIVE VIEW  (what the operator sees)      AUDIT  (what is kept)
  ephemeral                                durable
  authorized operator only                 who acted, action, target
  short-lived scoped access                which epoch, route pattern
  not recorded by default                  redacted values only
```

- Access is brokered behind SSO/MFA and tenant RBAC - never a raw browser debug URL.
- **Audit actions, not pixels:** the live view is not recorded by default; the durable
  record is structural.
- Sensitive values are used transiently and never persisted.
- Uncertainty fails closed - of owner, effect, observed state, entitlement, or evidence.

## 6. Failure modes (all fail closed)

| Failure | Required behavior |
|---|---|
| Two operators claim one intervention | atomic claim admits exactly one |
| A stale automation worker resumes | its superseded epoch is rejected at the gateway |
| The operator disconnects mid-takeover | session stays paused / unowned; no silent resume |
| The browser or session dies | typed `SESSION_LOST`; resume is not pretended |
| The human leaves the page elsewhere | reconciliation finds no matching checkpoint, escalate |
| The current page matches no state, or several | do not resume; escalate |
| The intervention SLA is exceeded | escalate the queue; a consequential action never auto-approves |
| The operator lacks entitlement | cannot claim or view the session |
| The human enters a sensitive value | used transiently, redacted from audit |

## 7. Learning from interventions

An intervention is evidence, not implicit training data. An approved capability is never
changed because a human resolved a single run. Repeated interventions are grouped by the
structural state that triggered them and by how they were resolved, and a *candidate*
behavior is proposed - then typed, validated, approved, and promoted into a new capability
version. What is learned is the shape of the resolution, never a raw recorded action:

- a harmless recurring dialog becomes a **recoverable** step (dismiss it), so replay handles
  it with no human;
- a per-run credential becomes a structured `INPUT_REQUIRED` request - the runtime asks for a
  fresh value each time; the value itself is never stored, and no live takeover is needed;
- an approval or judgment stays a human decision by policy;
- a genuinely novel state keeps escalating until it is explicitly modeled.

Discovery and replay differ in one way. During discovery the capability is still a draft, so
an approved human resolution can become candidate knowledge for compilation - a production
extension, not current behavior: this repository does not compile operator actions into the
artifact, and after handback discovery simply re-observes and continues. During replay the
capability is approved, so a human action never mutates it - it becomes telemetry and a
candidate patch for an approved new version.

```
human resolves an intervention
          |
     safe audit trail
          |
   recurring pattern?
      /           \
    no             yes
    |               |
  keep         classify the resolution
escalating     (recover / input / approve / unknown)
                    |
             candidate capability version
                    |
              validate + approve
                    |
                  deploy
```

The goal is not zero human interventions - structured approvals and credentials remain human
by design. The goal is that **live takeovers of the exact session trend toward zero** as
recurring conditions become modeled behavior, without weakening review or safety.

## 8. Scope

This repository implements the correctness-critical local primitive: exclusive session
ownership, audited same-session takeover, and safe reconciliation. The routing, authorization,
live-view, and intervention-learning components above describe how that primitive is
externalized at production scale. They are left as design because they do not change the core
correctness argument demonstrated here.
