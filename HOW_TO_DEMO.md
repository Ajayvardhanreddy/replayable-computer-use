# Demo Guide

Every runnable demo, with the exact command and what to watch for. The target is
**LegacyCore**, the synthetic bank workstation (`uv run legacy-core`, on
`http://localhost:8000`).

- **Genuine discovery and the discovery-side handoff** (Demos 1 and 9) need
  `ANTHROPIC_API_KEY` - a real model drives the UI.
- **Deterministic replay and the replay-side handoff demos are keyless** - they run the
  committed capabilities with no model in the loop (`model_calls = 0`).
- Add **`--headed`** to watch the real Chromium window; without it, runs are invisible and
  driven entirely from the terminal.
- Run **`uv run cua reset-demo`** before re-running any *write* demo (it clears created
  sub-accounts; leave the server up).

Setup once: `uv sync && uv run playwright install chromium`.

---

## Full demo catalog

### Demo 1 - Genuine discovery

- **Demonstrates / why** - a real model completes the goal by driving the live UI; this is
  the "discover once" half that everything else reuses.
- **Requires** - LegacyCore; `ANTHROPIC_API_KEY`; `--headed` to watch.

```bash
export ANTHROPIC_API_KEY=...
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345 --target http://localhost:8000 --headed
```

- **What you'll see** - the model types the member number, clicks Search, reads the balance,
  and declares success; the terminal prints `{"artifact": "...", "model_calls": 4,
  "stop_reason": "GOAL_REACHED"}`.
- **Proves success** - `GOAL_REACHED`, a non-zero `model_calls`, and a written artifact.
- **Evidence** - writes `evidence/capability/member_lookup.v1.json` and a sanitized
  `evidence/discovery/trace.jsonl` (values recorded as `<param:member_number>`).

### Demo 2 - Keyless replay (the production path)

- **Demonstrates / why** - the compiled capability re-runs for a *different* member with no
  model; this is the path an agent would invoke in production.
- **Requires** - LegacyCore. No key.

```bash
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321
```

- **What you'll see** -
  `{"status":"success", ..., "outputs":{"savings_balance":"312.45"}, "model_calls":0}`.
- **Proves success** - `model_calls: 0`, a returned output, and an input different from the
  one discovery used.
- **Evidence** - `evidence/replay_success/` (`result.json` + `trace.jsonl`; the balance is
  masked to `<financial>` in evidence, returned raw only on stdout).

### Demo 3 - Business outcome

- **Demonstrates / why** - "no such member" is a legitimate result the caller needs, not a
  crash - a correctness distinction the result contract deliberately preserves.
- **Requires** - LegacyCore. No key.

```bash
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=99999
```

- **What you'll see** -
  `{"status":"business_outcome","code":"MEMBER_NOT_FOUND", ..., "model_calls":0}`.
- **Proves success** - a typed `business_outcome` distinct from `failure`.
- **Evidence** - `evidence/replay_business_outcome/`.

### Demo 4 - Deterministic runtime scenarios

- **Demonstrates / why** - injected runtime conditions map to typed results, not silent
  proceed-anyway; the same model-free runtime classifies each.
- **Requires** - LegacyCore. No key.

```bash
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321 --scenario session_expired
#   → failure CHECKPOINT_FAILED, observed heading "Session Expired"
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321 --scenario permission_denied
#   → failure CHECKPOINT_FAILED, observed heading "Access Denied"
uv run cua replay evidence/capability/member_lookup.v1.json --param member_number=54321 --scenario not_found
#   → business_outcome MEMBER_NOT_FOUND
```

- **Proves success** - a hard failure carries `expected` vs `observed` (sanitized); a
  business outcome stays a business outcome.
- **Reference** - the full 12-row matrix is in [`docs/eval-scenarios.md`](docs/eval-scenarios.md).

### Demo 5 - Consequential write, verified under a lost response

- **Demonstrates / why** - the write is dispatched **exactly once**; its response is lost,
  and an independent read-only re-derivation confirms the effect committed. Timeout ≠ failure,
  and an uncertain write is never blindly retried.
- **Requires** - LegacyCore; **`reset-demo` first**. No key.

```bash
uv run cua reset-demo
uv run cua replay evidence/capability/open_sub_account.v1.json --param member_number=54321 \
  --capability open_sub_account --scenario commit_then_timeout --commit-timeout-ms 300 --headed
```

- **What you'll see** -
  `{"status":"success", ..., "outputs":{"sub_account_status":"OPEN"}, "model_calls":0}`; in
  headed mode the write page hangs, then the runtime re-checks the member's accounts.
- **Proves success** - `success` reached via independent verification, not via the commit
  echo; the commit dispatched once (no double-write).
- **Evidence** - `evidence/replay_mutation/`.

### Demo 6 - Consequential write, unverifiable → escalate

- **Demonstrates / why** - when the effect cannot be established, the runtime **never
  guesses**: it stops and hands off a full case.
- **Requires** - LegacyCore; **`reset-demo` first**. No key.

```bash
uv run cua reset-demo
uv run cua replay evidence/capability/open_sub_account.v1.json --param member_number=54321 \
  --capability open_sub_account --scenario commit_unverifiable --headed
```

- **What you'll see** - `{"status":"escalated","code":"MUTATION_AMBIGUOUS", ...}` followed by
  a rendered **handoff case** (capability, step, why, what-I-did, what-to-do).
- **Proves success** - `MUTATION_AMBIGUOUS` rather than a false `success`/`failure`; the
  unattended runner routes a clear case and stops.
- **Evidence** - `evidence/replay_mutation_ambiguous/`.

### Demo 7 - Same-session replay handoff

- **Demonstrates / why** - replay meets a dialog it cannot classify, pauses, and a human
  resolves it on the **same live session** before automation reconciles and finishes.
- **Requires** - LegacyCore. No key. Interactive; `--headed` to watch.

```bash
uv run cua handoff-demo --headed
```

At the `operator ❯` prompt:

```
take        # take exclusive control of the same live session (AUTOMATION → HUMAN, epoch → 1)
click c1    # c1 = the blocker's "Acknowledge" (or: click Acknowledge)
resume      # hand back (HUMAN → AUTOMATION, epoch → 2); the runtime reconciles and finishes
```

- **What you'll see** - an `INTERVENTION REQUIRED` panel (reason `UNKNOWN_DIALOG`, an
  Expected-vs-Observed view, controls by id), then `SUCCESS` with `model_calls: 0` after
  handback; the browser stays the *same* window throughout.
- **Proves success** - `AUTOMATION → HUMAN → AUTOMATION` on one session, a recorded human
  action, and a resumed `success`.
- **Evidence** - `evidence/replay_handoff/` (`intervention.json`, `actions.jsonl`,
  `result.json`).

### Demo 8 - Same-session mutation handoff

- **Demonstrates / why** - a write commits, but the verification read is blocked; a human
  clears it and resumes, and automation re-runs **only the read-only verification** - the
  write is never re-dispatched.
- **Requires** - LegacyCore; **`reset-demo` first**. No key. Interactive.

```bash
uv run cua reset-demo
uv run cua handoff-demo evidence/capability/open_sub_account.v1.json --param member_number=54321 \
  --capability open_sub_account --scenario verification_dialog --headed
#   operator ❯ take → click c1 → resume
```

- **Proves success** - `MUTATION_AMBIGUOUS` intervention → takeover → `SUCCESS`
  (`sub_account_status: OPEN`) with the commit dispatched exactly once.
- **Evidence** - `evidence/replay_mutation_handoff/`.

### Demo 9 - Discovery-side handoff

- **Demonstrates / why** - a live discovery model, refused a consequential step, asks for a
  human itself; the human acts on the same session and the model resumes.
- **Requires** - LegacyCore; `ANTHROPIC_API_KEY`. Interactive; `--headed` to watch.

```bash
uv run cua discover --headed --scenario verification_required \
  --goal "Look up this member and return their savings balance" \
  --param member_number=12345
```

At the `operator ❯` prompt:

```
take        # take the same live session
submit c2   # c2 = the Employee Verification Code field; the value is entered at a masked
            # prompt, never typed inline, and audited as <redacted>
resume      # hand back; the model re-observes and finishes (GOAL_REACHED)
```

- **Proves success** - the model's own `request_human` (labelled "Agent request"), a masked
  human action, `AUTOMATION → HUMAN → AUTOMATION`, and `GOAL_REACHED` on resume.
- **Evidence** - `evidence/discovery_handoff/`. A deeper annotated walkthrough is in
  [`docs/demo-handoff.md`](docs/demo-handoff.md).

---

## Intervention *raised* ≠ handoff *completed*

Two different things, easy to conflate:

- **Intervention raised** - the system detects it cannot safely proceed and routes a
  context-carrying request; the *unattended* `cua replay` runner then **exits** with
  `escalated` + the case (Demo 6). This is the detect-and-route half.
- **Handoff completed** - a human takes over the **same live session**, acts, and hands
  control back, and the run resumes/finishes (Demos 7, 8, 9), driven by `cua handoff-demo`
  (or interactive `discover`).

The visible browser is only so you can watch; what makes it a handoff is the control transfer
over the same session (the `ControlLease` + epochs) plus the human operating it.

## Troubleshooting

- **`connection refused` / blank page** - LegacyCore isn't running; start `uv run legacy-core`
  and confirm `http://localhost:8000` loads.
- **A write demo returns `ACCOUNT_ALREADY_EXISTS`** - the sub-account already exists from a
  prior run; run `uv run cua reset-demo` and retry.
- **`ANTHROPIC_API_KEY is not set`** - only discovery (Demos 1, 9) needs a key; replay and
  handoff demos do not.
- **Chromium fails to launch** - run `uv run playwright install chromium` once.
