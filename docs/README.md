# Documentation

Start with the root [`README.md`](../README.md) (what it is + quick start) and
[`REPORT.md`](../REPORT.md) (the concise design write-up). This folder holds the deeper
references.

| Document | What it covers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The canonical system design: trust boundary, invariants, discovery→compile→replay, and a code/proof map |
| [`../HOW_TO_DEMO.md`](../HOW_TO_DEMO.md) | Every runnable demo with exact commands and expected output |
| [`mutation-design.md`](mutation-design.md) | Consequential-write correctness: dispatch-once, discovered verification, committed/not-committed/ambiguous |
| [`handoff-design.md`](handoff-design.md) | Human-in-the-loop control-transfer semantics: lease, epoch fencing, reconcile-before-resume |
| [`demo-handoff.md`](demo-handoff.md) | An annotated, transcript-level walkthrough of the handoff demos |
| [`eval-scenarios.md`](eval-scenarios.md) | The deterministic scenario → outcome matrix |
| [`../evidence/README.md`](../evidence/README.md) | How the committed evidence maps to claims |
