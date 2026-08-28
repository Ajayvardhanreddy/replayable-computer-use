"""Neutral replay execution events emitted by the trusted runtime.

The runtime owns the truth of what it executed and emits these small structural facts
through an optional sink. Persistence (JSONL) and redaction belong to the observability
and CLI layers — this module carries no I/O and no raw values, so the execution layer
never couples to a storage or observability framework.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayEvent:
    """One structural fact about a replay run.

    Which run started, which step executed and whether its checkpoint held, a mutation's
    verified effect state, and the terminal result. Only structural identifiers and enums —
    never a raw value, driver text, or model reasoning.
    """

    kind: str  # replay_started | step_replayed | mutation_verified | replay_finished
    run_id: str | None = None
    step_id: str | None = None
    action_kind: str | None = None  # click | type | extract
    checkpoint_satisfied: bool | None = None
    effect_state: str | None = None  # COMMITTED | NOT_COMMITTED | AMBIGUOUS | ...
    result_kind: str | None = None  # success | business_outcome | escalated | failure
    model_calls: int | None = None


ReplayEventSink = Callable[[ReplayEvent], None]
