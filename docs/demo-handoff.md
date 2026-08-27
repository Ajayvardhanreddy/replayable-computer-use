# Human Handoff — Demo

Two runnable demos show a human taking over the exact live browser session and handing
it back. They exercise the same control-transfer primitive from two directions:

- **Replay-side** — deterministic replay (`model_calls = 0`) meets an unexpected modal it
  cannot classify, pauses, a human resolves it, and replay reconciles and completes.
- **Discovery-side** — a live model discovers a workflow, reaches a state it cannot get
  past, proposes escalation itself, a human resolves it, and the model continues.

Add `--headed` to watch the Chromium window; the terminal output is identical either way.
Identifiers and timestamps vary per run.

## Setup

```bash
uv run playwright install chromium   # first time only
uv run legacy-core                   # terminal 1: serves http://localhost:8000
```

Demo A needs no model key. Demo B needs `ANTHROPIC_API_KEY` (a local `.env` is loaded
automatically).

---

## Demo A — Replay-side handoff (deterministic, model-free)

### Run it

```bash
uv run cua handoff-demo --headed          # terminal 2
```

A window opens: automation types the member number, clicks Search, reaches the profile —
and a **"System Notice" modal blocks it**. Replay stops and prints an intervention. At the
`operator>` prompt, type:

```
status
take
ack
resume
```

### Expected output

```
=== Intervention required ===
  id:         int_bd23879b
  capability: member.lookup_savings_balance v1
  step:       step_3_extract
  reason:     UNKNOWN_DIALOG
  control:    automation (epoch 0)
  route:      /
  landmarks:  ['System Notice', 'Member Profile']
commands: take (take control) | ack (acknowledge the notice) | resume (hand back to automation) | status | help | quit
operator> owner=automation epoch=0
operator> control -> HUMAN (epoch 1); automation is now blocked
operator> acknowledged on the live session (human action recorded)
operator> {"run_id":"run_02d0684e","model_calls":0,"status":"success","capability":"member.lookup_savings_balance","version":1,"outputs":{"savings_balance":"8421.31"}}
```

The Chromium window stays open so you can watch the same session change as each command
runs. Every operator action goes through the audited control path — `ack` clicks Acknowledge
on that exact session and records it — so the human's activity is captured, not just observed.

### What this proves

- Replay **stopped before `step_3_extract`** and raised `UNKNOWN_DIALOG`: it refuses to act
  on a blocking state the artifact does not model. Detection is structural (a visible
  `role=dialog aria-modal` element), not a match on the notice text.
- `take` flips the lease to `HUMAN (epoch 1)` and the trusted kernel **fences automation off**.
- The human resolves it on the **same live session**; `resume` reconciles (the modal is gone
  and the "Member Profile" checkpoint still holds) and continues.
- The run finishes `success`, `savings_balance: 8421.31`, **`model_calls: 0`** — the human
  intervention did not turn deterministic replay back into an agent.

### Evidence (`evidence/replay_handoff/`, sanitized)

```
# actions.jsonl
{"event":"control_transferred", "from_owner":"automation","to_owner":"human","epoch":1,"reason":"UNKNOWN_DIALOG"}
{"event":"human_action",        "epoch":1,"action":"click","target":"link:Acknowledge","route":"/","value":null}
{"event":"control_transferred", "from_owner":"human","to_owner":"automation","epoch":2,"reason":null}
# result.json  ->  outputs.savings_balance = "<financial>"   (masked in persisted evidence)
```

Ownership moves `automation → human → automation` with a monotonic epoch (0 → 1 → 2); the
balance is masked in the persisted file while the live result returns the real value.

---

## Demo B — Discovery-side handoff (live model)

### Run it

```bash
uv run cua discover --headed --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  -p member_number=12345 \
  --out evidence/discovery_handoff/member_lookup.v1.json --evidence evidence/discovery_handoff/trace.jsonl
```

The window shows the live model type the member number, click Search, and reach an
**"Identity Verification Required"** screen — a verification state for which it was not given
the required credential. It stops and prints an intervention. At the `operator>` prompt, type:

```
take
submit Employee Verification Code=4729
resume
```

The window stays open so you watch the same session unblock; the operator drives it through
the audited console commands, so the code entry is recorded (as a redacted value).

### Expected output

```
=== Intervention required (discovery) ===
  id:         int_a84e5d70
  capability: member.lookup_savings_balance
  reason:     HUMAN_REQUESTED
  control:    automation (epoch 0)
  landmarks:  ['Identity Verification Required']
  controls:   ['link:Member Inquiry', 'textbox:Employee Verification Code']

You now hold nothing yet. To resolve this on the SAME live session:
  1) 'take'  — grab exclusive control
  2) resolve it, either way:
       • directly in the browser window (type the code, press Enter), or
       • 'submit <field>=<value>' to do it through the audited console
         e.g.  submit Employee Verification Code=4729
  3) 'resume' — hand control back so the model continues
commands: take | submit <field>=<value> | type <field>=<value> | click <name> | resume | status | help | quit
operator> control -> HUMAN (epoch 1); automation is blocked
operator> submitted 'Employee Verification Code' (value recorded as redacted)
operator> control -> AUTOMATION; discovery will re-observe and continue
{"artifact": "evidence/discovery_handoff/member_lookup.v1.json", "model": "claude-sonnet-4-6", "model_calls": 6, "stop_reason": "GOAL_REACHED"}
```

### Evidence trace (`evidence/discovery_handoff/trace.jsonl`)

```
discovery_started   {provider: anthropic, model_id: claude-sonnet-4-6, goal_present: True}
step_executed       {action: type,   target: textbox:Member Number, value: <param:member_number>}
step_executed       {action: click,  target: button:Search}
step_rejected       {code: RISK_CONFIRMATION_REQUIRED}          # tried to click through; trusted policy blocked it
intervention_raised {reason: HUMAN_REQUESTED, model_call: 4}    # the model itself escalated
control_transferred {automation -> human, epoch: 1}
human_action        {epoch: 1, action: type, target: textbox:Employee Verification Code, value: <redacted>}
control_transferred {human -> automation, epoch: 2}
step_executed       {action: extract, target: cell[Share Savings/Current Balance], output: savings_balance}
discovery_finished  {model_calls: 6, stop_reason: GOAL_REACHED}
```

### What this proves

- This is a genuine live run (`claude-sonnet-4-6`, `model_calls: 6`).
- The model tried to click through the verification state; the **trusted kernel refused it**
  (`RISK_CONFIRMATION_REQUIRED`). The model then **proposed `request_human` on its own** at
  `model_call 4` — the escalation is the model's decision, from its normal action schema.
- The human takes exclusive control (`epoch 1`) and enters the code on the **same live
  session**; the value is audited as `<redacted>`, never the raw code.
- `resume` returns control to automation (`epoch 2`); discovery **re-observes** the now-
  unblocked page and the model continues to `GOAL_REACHED`. The resulting artifact is then
  handed to the same deterministic replay engine, which never calls a model.
- The evidence contains no member id, balance, or code — only structural fingerprints,
  redacted values, and a monotonic epoch.

### Sanity check — the escalation is not hard-coded

The identical discovery flow against the **normal** account completes with no intervention:

```bash
uv run cua discover --headless \
  --goal "Look up this member and return their savings balance" \
  -p member_number=12345 --out /tmp/n.json --evidence /tmp/n.jsonl
# -> "no intervention was required for this run"
# -> {"stop_reason": "GOAL_REACHED", "model_calls": 4}
```

The handoff is therefore not keyed to "always ask for a human"; the model requests help only
after it actually encounters the verification state.

### Why the verification scenario uses live takeover

A known verification-code requirement is a natural structured `INPUT_REQUIRED` intervention;
in production it would be answered as a typed request without transferring browser control.
It is used here to exercise the more demanding same-session takeover path, since that is the
correctness-sensitive mechanism worth proving directly. See `docs/handoff-design.md`.

---

## Reproduce the transcripts non-interactively

The operator inputs can be piped for a scripted, headless reproduction (useful in CI or for
regenerating evidence):

```bash
printf 'status\ntake\nack\nresume\n' | uv run cua handoff-demo --headless

printf 'take\nsubmit Employee Verification Code=4729\nresume\n' | \
  uv run cua discover --headless --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  -p member_number=12345 --out /tmp/d.json --evidence /tmp/d.jsonl
```

---

## What the demos prove

| Property | Replay-side | Discovery-side |
|---|---|---|
| Detect and pause before an unsafe step | `UNKNOWN_DIALOG` before `step_3_extract` | model `request_human` → `HUMAN_REQUESTED` |
| Sanitized intervention context (no record values) | ✅ | ✅ |
| Exclusive control (lease + epoch, automation fenced) | `take` → epoch 1 | `take` → epoch 1 |
| Human acts on the same live session | Acknowledge click | code entry |
| Audited human action, values redacted | `link:Acknowledge` | `value: <redacted>` |
| Reconcile before resume (never `cursor + 1`) | checkpoint re-verified | re-observe → continue |
| Completes after handback | `success`, `model_calls = 0` | `GOAL_REACHED` |
| Escalation decided by the live model | — | ✅ |
