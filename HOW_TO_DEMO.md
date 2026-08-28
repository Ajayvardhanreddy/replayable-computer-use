# How to run the demos

One sentence: **a model figures out a task on a real bank UI, we save it as a reusable
capability, and replay it later with no model - safely, and with a human able to step in.**

Three demos below. Run them in order. That's the whole thing.

---

## Setup (do this once)

**Terminal 1 - the target app (a synthetic bank workstation):**
```bash
uv run legacy-core
```

**Terminal 2 - the commands below.**

- Discovery needs a model key: put `ANTHROPIC_API_KEY=...` in a local `.env` (git-ignored).
- Replay needs **no key** (that's the point).
- **Before each WRITE demo (3b, 3c, 3d), run `uv run cua reset-demo`** - it clears LegacyCore's
  in-memory state (leave the server running; no Ctrl-C needed). Skip it and a leftover account
  turns the next write demo into `ACCOUNT_ALREADY_EXISTS`.
- Add `--headed` to any command to watch it in a real browser window. Leave it off to run invisibly.
- Each `discover` writes the capability into `evidence/capability/`; the `replay` steps read it
  from there. So run a demo's `discover` step before its `replay` steps.

---

## Demo 1 - Look up a member's balance (the core loop)

**1a. Discover - watch the model drive the UI:**
```bash
uv run cua discover \
  --goal "Look up this member and return their savings balance" \
  -p member_number=12345 \
  --out evidence/capability/member_lookup.v1.json \
  --evidence evidence/discovery/trace.jsonl --headed
```

**1b. Replay for a DIFFERENT member - no model:**
```bash
uv run cua replay evidence/capability/member_lookup.v1.json -p member_number=54321 --headed
```
→ returns the balance, `model_calls: 0`.

**1c. Unknown member - a clean answer, not a crash:**
```bash
uv run cua replay evidence/capability/member_lookup.v1.json -p member_number=99999
```
→ `business_outcome: MEMBER_NOT_FOUND`.

**Shows:** the model discovers a task once → we save it → replay it deterministically with new
inputs → known outcomes are handled, not crashed.

---

## Demo 2 - A locked account needs a human

```bash
uv run cua discover \
  --goal "Look up this member and return their current savings balance" \
  -p member_number=12345 --scenario verification_required \
  --out evidence/discovery_handoff/member_lookup.v1.json \
  --evidence evidence/discovery_handoff/trace.jsonl --headed
```
The model hits a locked account and **asks for a human**. At the `operator ❯` prompt:
```
take                                    # take the same live session; shows the blocker's controls
submit c2                               # the code field; the value is entered at a masked prompt, never typed inline
resume                                  # hand control back; the model finishes
```
You're running **headed**, so the live browser window is your visual context - you look at the real
page and decide. The terminal presents the state structurally and scopes controls to the blocker
(with ids); nothing is pre-scripted. `inspect` re-reads the live state.

**Shows:** when the model can't proceed, a human takes over the *same live session*, acts, and
hands back - no restart, nothing lost.

> ### On the human handoff - two different things, don't conflate them
> - **Intervention *raised*** = the system detects it can't safely proceed and routes a
>   context-carrying request; the *unattended* `cua replay` runner then **exits** with
>   `escalated` + the case. This is only the **detect-and-route** half. → **Demo 3c**.
> - **Handoff *completed*** = a human takes over the **same live session**, acts, and hands
>   control back, and the run resumes/finishes - the full loop
>   `detect → route → pause → take same session → act → hand back → reconcile → resume`.
>   Driven by `cua handoff-demo`. → **Demo 2** (locked account) and **Demo 3d** (mutation).
>
> **Canonical handoff demo = headed + interactive**: the reviewer *watches* the very same
> Chromium session pass `automation → human → automation`, and the live browser window **is** the
> human's visual surface - they look at the real dialog and decide what to do. The identical
> control mechanism also runs **headless** - the operator drives it purely through the terminal,
> which shows a deterministic **Expected vs Observed** panel and the **blocker's** controls by id
> (the deterministic proof for CI):
> ```bash
> uv run cua handoff-demo --headless   # take -> click c1 -> resume  (c1 = the blocker's Acknowledge)
> # same Page/BrowserContext, same ControlLease/epochs, same audited human action - no window
> ```
> The browser being *visible* is only so you can watch; it is not what makes it a handoff. What
> makes it a handoff is the **control transfer over the same session** (the lease) + the human
> **operating** it through the operator surface - both true headed or headless.

---

## Demo 3 - Open an account safely (the hard banking case)

**3a. Discover - the model opens an account AND confirms it (you approve the one write):**
```bash
uv run cua discover --capability open_sub_account \
  --goal "Open a Share Savings sub-account for this member and report the new sub-account's status from their account list." \
  -p member_number=12345 \
  --out evidence/capability/open_sub_account.v1.json \
  --evidence evidence/discovery_open_sub_account/trace.jsonl --headed
# when it asks:  approve this action? [y/N]  →  y
```

**3b. Replay, different member - the write's reply is lost, but it verifies anyway:**
*(run `uv run cua reset-demo` first)*
```bash
uv run cua replay evidence/capability/open_sub_account.v1.json -p member_number=54321 \
  --capability open_sub_account --scenario commit_then_timeout --commit-timeout-ms 300 --headed
```
→ dispatches the write **once**, the page hangs, it re-checks the accounts independently →
`success`, no double-write.

**3c. Replay - it can't verify, so it raises a full handoff case and stops:**
*(run `uv run cua reset-demo` first)*
```bash
uv run cua replay evidence/capability/open_sub_account.v1.json -p member_number=54321 \
  --capability open_sub_account --scenario commit_unverifiable --headed
```
→ `escalated: MUTATION_AMBIGUOUS`, then it prints a **handoff case** for a human:
```
=== Handoff case - a human needs to look into this ===
  case id:       int_...
  capability:    member.open_sub_account v1
  stopped at:    step step_4_click
  why:           MUTATION_AMBIGUOUS
  what happened: verification: effect could not be established
  what I did:    dispatched the write exactly once; did NOT retry it and did NOT assume success/failure.
  please:        confirm in the system of record whether the change took effect, then close this case.
```
`cua replay` is the *unattended* runner: it does the write once, can't confirm it *here*, and hands
off a **clear case with full context** - it never guesses. A human then resolves it **out of band**:
retry the read, or check the **core banking system directly** - using access the boxed-in agent
doesn't have. (The *in-session* takeover, where a human fixes it live, is the next demo.)

**3d. Handoff - blocked but fixable: a human takes over and it continues:**
*(run `uv run cua reset-demo` first)*
```bash
uv run cua handoff-demo evidence/capability/open_sub_account.v1.json -p member_number=54321 \
  --capability open_sub_account --scenario verification_dialog \
  --evidence-out evidence/replay_mutation_handoff --headed
```
The verification read is blocked by a notice. At the `operator ❯` prompt:
```
take       # take the same live session
```
You're running **headed**, so the live browser is your visual context. The panel shows
**Expected vs Observed** (deterministic, no prose) and the **blocker's** controls by id - then
*you* decide (nothing is pre-scripted). `inspect` re-reads the live state; act by label or id:
```
click c1              # c1 = the blocker's Acknowledge; or `click Acknowledge`
```
then:
```
resume     # hand back; it re-checks (read-only) and finishes
```
→ `success`. The write is **never** re-clicked.

**Shows:** a consequential write is dispatched exactly once; if the result is uncertain it
verifies by an independent read; if it still can't tell, it stops or a human resolves it - never
a blind retry, never a false success.

---

## Demo 4 - Deterministic scenarios (the app as an eval environment)

LegacyCore injects reproducible failure states with a `--scenario` switch; the model-free runtime
(`model_calls: 0`) classifies each into a **typed** result. No model key needed.

```bash
# Runtime errors → typed failures that name the observed state:
uv run cua replay evidence/capability/member_lookup.v1.json -p member_number=54321 --scenario session_expired
#   → failure CHECKPOINT_FAILED, observed heading "Session Expired"

uv run cua replay evidence/capability/member_lookup.v1.json -p member_number=54321 --scenario permission_denied
#   → failure CHECKPOINT_FAILED, observed heading "Access Denied"

# A legitimate domain answer, not a crash (even with a valid id):
uv run cua replay evidence/capability/member_lookup.v1.json -p member_number=54321 --scenario not_found
#   → business_outcome MEMBER_NOT_FOUND
```

Other scenarios used by the demos above: `slow`, `unexpected_dialog` (Demo 2),
`verification_required` (discovery handoff), and the write family `commit_then_timeout`,
`commit_ambiguous`, `commit_dropped`, `commit_unverifiable`, `verification_dialog` (Demo 3).

**Shows:** the same generic runtime handles heterogeneous runtime errors - a business outcome, a
typed failure with safe evidence, an escalation, or a same-session handoff - never a crash. The
full **scenario → outcome matrix** (12 rows, each with its test/evidence) is in
[`docs/eval-scenarios.md`](docs/eval-scenarios.md).

---

## What each demo proves (one line each)

| Demo | Proves |
|---|---|
| 1 | discovery → saved capability → deterministic replay + business outcomes |
| 2 | same-session human takeover |
| 3 | safe consequential writes: dispatch-once, independent verification, escalate-or-recover |
| 4 | deterministic eval scenarios: heterogeneous runtime errors → typed outcomes + safe evidence |

That covers the whole submission: a goal, a genuine model run on a real UI, a saved capability,
deterministic replay with inputs/outputs/errors, and a human able to take the live session.
