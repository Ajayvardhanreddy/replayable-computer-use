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
and a **"System Notice" modal blocks it**. Replay stops and prints a deterministic
**Expected-vs-Observed** intervention (no model prose). At the `operator ❯` prompt, type:

```
take           # take control; it lists the blocker's controls (c1 Acknowledge) and shows the session id
click c1       # click the blocker's Acknowledge (short alias: `ack`)
resume         # reconcile and hand back; the run completes
```

### Expected output (rich panels; shown here as plain text)

```
╭──────────────── INTERVENTION REQUIRED ────────────────╮
  Capability   member.lookup_savings_balance v1
  Step         step_3_extract
  Reason       UNKNOWN_DIALOG
  Session      sess_83018d74
  Control      AUTOMATION      Epoch  0
  Last action  click Search
  Expected     Member Profile · savings_balance
  Observed     Member Profile · blocked by dialog "System Notice"
  Dialog text  "Please acknowledge the account notice before continuing."
  controls in blocker:   c1   link   Acknowledge
  take · inspect · controls · status · help · quit
╰────────────────────────────────────────────────────────╯
[AUTOMATION · epoch 0] operator ❯ take
  ● Control transferred   AUTOMATION → HUMAN   session sess_83018d74 preserved   epoch → 1
  ╭ HUMAN CONTROL ╮ You now control the same live session. Automation is fenced.
  blocked by dialog "System Notice" — its controls:   c1   link   Acknowledge
[HUMAN · epoch 1] operator ❯ click c1
  ✓ recorded: click "Acknowledge"   (epoch 1)
  blocker cleared — now at "Member Profile". Type 'resume' to hand back.
[HUMAN · epoch 1] operator ❯ resume
  ◌ Reconciling current application state…
  ✓ Control returned HUMAN → AUTOMATION (session sess_83018d74 preserved, epoch 2)
  ✓ model_calls: 0
  ╭ SUCCESS ╮  savings_balance  8421.31
```

The Chromium window stays open so you can watch the same session change as each command
runs. Every operator action goes through the audited control path — `click c1` clicks the
dialog's Acknowledge on that exact session and records it — so the human's activity is
captured, not just observed. The intervention presents **deterministic facts** (what the
artifact expected vs what the live surface shows), never a model-written explanation.

### What this proves

- Replay **stopped before `step_3_extract`** and raised `UNKNOWN_DIALOG`: it refuses to act
  on a blocking state the artifact does not model. Detection is structural (a visible
  `role=dialog aria-modal` element), not a match on the notice text.
- `take` flips the lease to `HUMAN (epoch 1)` and the trusted kernel **fences automation off**.
- The **same session id** (`sess_…`) is shown at the intervention and marked *preserved* across
  both transfers (epoch 0 → 1 → 2) — the same-live-session requirement made visible, using our
  own stable identifier, never a driver internal.
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
the required credential. The model **asks for a human itself**, and the panel shows the
model's own reason plus the trusted structural observation. At the `operator ❯` prompt, type:

```
take           # take control; it lists the page controls (c2 is the code field) and shows the session id
submit c2      # enter the code at a masked prompt (a sensitive field is never typed inline)
resume         # hand back; the model re-observes the unblocked page and continues
```

### Expected output (rich panels; shown here as plain text)

```
╭─────────── INTERVENTION REQUIRED  ·  discovery ───────────╮
  Capability   member.lookup_savings_balance v1
  Step         —
  Reason       HUMAN_REQUESTED
  Session      sess_cb7b3ccb
  Control      AUTOMATION      Epoch  0
  Agent request:  <the model's own concise reason for escalating>
  Observed     Identity Verification Required
  take · inspect · controls · status · help · quit
╰────────────────────────────────────────────────────────────╯
[AUTOMATION · epoch 0] operator ❯ take
  ● Control transferred   AUTOMATION → HUMAN   session sess_cb7b3ccb preserved   epoch → 1
  current actionable controls:   c1 link Member Inquiry    c2 textbox Employee Verification Code
[HUMAN · epoch 1] operator ❯ submit c2
  Employee Verification Code: ••••          (no-echo prompt — the value never appears)
  ✓ recorded: submit "Employee Verification Code"   (epoch 1 · value redacted)
[HUMAN · epoch 1] operator ❯ resume
  ● Control transferred   HUMAN → AUTOMATION   session sess_cb7b3ccb preserved   epoch → 2
  discovery will re-observe and continue
{"artifact": "evidence/discovery_handoff/member_lookup.v1.json", "model": "claude-sonnet-4-6", "model_calls": 6, "stop_reason": "GOAL_REACHED"}
```

Because the code field is a **sensitive** field, its value is read from a no-echo prompt — it
appears in neither the terminal transcript nor the evidence (audited as `<redacted>`). The panel
shows the model's *own* `request_human` reason (clearly labelled as an agent request), never a
generated paragraph; replay, by contrast, shows deterministic Expected-vs-Observed facts.

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
printf 'take\nclick c1\nresume\n' | uv run cua handoff-demo --headless

printf 'take\nsubmit c2=4729\nresume\n' | \
  uv run cua discover --headless --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  -p member_number=12345 --out /tmp/d.json --evidence /tmp/d.jsonl
```

The inline form (`submit c2=4729`, a synthetic value) is for automated, non-interactive
regeneration only — a piped stream has no terminal for a masked prompt to read from. It is
never the human workflow: interactively the same sensitive field is refused inline and read
from a masked prompt, so a real credential is never placed in shell history. Either way the
value is audited as `<redacted>`.

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
