"""JSONL evidence store and safe event builders.

Evidence never persists API keys, environment variables, model reasoning, or raw
sensitive values. Action values are recorded symbolically (e.g. <param:member_number>);
extracted financial values are not persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from computer_use.execution import KernelExecution
from computer_use.model import (
    EvidenceEvent,
    ParameterRef,
    SafeLiteral,
    SecretRef,
    TargetDescriptor,
    ValueRef,
)


class EvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: EvidenceEvent) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _target_summary(target: TargetDescriptor) -> str:
    if target.table_cell is not None:
        return f"cell[{target.table_cell.row_contains}/{target.table_cell.column_header}]"
    if target.role and target.name:
        return f"{target.role}:{target.name}"
    if target.text:
        return f"{target.role or 'element'}:text"
    return target.role or "element"


def _value_summary(value: ValueRef | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, ParameterRef):
        return f"<param:{value.name}>"
    if isinstance(value, SafeLiteral):
        return "<const>"
    if isinstance(value, SecretRef):
        return "<secret>"
    return "<derived>"


def discovery_started_event(
    run_id: str, provider: str, model_id: str, capability_id: str, goal: str
) -> EvidenceEvent:
    return EvidenceEvent(
        event="discovery_started",
        run_id=run_id,
        ts=_now(),
        attributes={
            "provider": provider,
            "model_id": model_id,
            "capability_id": capability_id,
            "goal": goal,
        },
    )


def step_executed_event(
    run_id: str, index: int, execution: KernelExecution, output: str | None
) -> EvidenceEvent:
    return EvidenceEvent(
        event="step_executed",
        run_id=run_id,
        ts=_now(),
        step_id=f"step_{index}",
        attributes={
            "action": execution.action.value,
            "target": _target_summary(execution.target),
            "risk": execution.risk.value,
            "value": _value_summary(execution.value),
            "output": output,
        },
    )


def step_rejected_event(run_id: str, code: str) -> EvidenceEvent:
    return EvidenceEvent(
        event="step_rejected", run_id=run_id, ts=_now(), attributes={"code": code}
    )


def discovery_finished_event(run_id: str, model_calls: int, stop_reason: str) -> EvidenceEvent:
    return EvidenceEvent(
        event="discovery_finished",
        run_id=run_id,
        ts=_now(),
        attributes={"model_calls": model_calls, "stop_reason": stop_reason},
    )
