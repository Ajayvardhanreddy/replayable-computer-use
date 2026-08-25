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
    Capability,
    EvidenceEvent,
    Failure,
    ParameterRef,
    RunResult,
    SafeLiteral,
    SecretRef,
    Sensitivity,
    Success,
    TargetDescriptor,
    ValueRef,
)

# Allowlist of attribute keys that may be persisted per event type. The sanitizer
# runs at the write boundary, so any key not listed here is dropped before bytes hit
# disk — a caller mistake cannot leak raw data. Free-text (goal, reasons, raw error
# messages) is intentionally not allowlisted anywhere.
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "discovery_started": frozenset({"provider", "model_id", "capability_id", "goal_present"}),
    "step_executed": frozenset({"action", "target", "risk", "value", "output"}),
    "step_rejected": frozenset({"code"}),
    "discovery_finished": frozenset({"model_calls", "stop_reason"}),
}


def _sanitize(event: EvidenceEvent) -> EvidenceEvent:
    allowed = _ALLOWED_ATTRIBUTES.get(event.event, frozenset())
    safe = {key: value for key, value in event.attributes.items() if key in allowed}
    return event.model_copy(update={"attributes": safe})


class EvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: EvidenceEvent) -> None:
        # Sanitize at the last point before serialization: only allowlisted,
        # already-symbolic attributes reach disk.
        safe = _sanitize(event)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(safe.model_dump_json() + "\n")


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
            # The raw natural-language goal may contain PII; record only its presence.
            "goal_present": bool(goal),
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


_SENSITIVITY_MASK: dict[Sensitivity, str] = {
    Sensitivity.FINANCIAL: "<financial>",
    Sensitivity.PII: "<pii>",
    Sensitivity.SECRET: "<secret>",
}


def persistable_result(result: RunResult, capability: Capability) -> dict[str, object]:
    """A RunResult masked for persistence: sensitive outputs become typed placeholders.

    The in-memory RunResult returned to the caller keeps the raw value; only the
    persisted evidence is masked by the output's declared sensitivity.
    """
    data: dict[str, object] = result.model_dump(mode="json")
    if isinstance(result, Success):
        specs = capability.outputs
        masked: dict[str, str] = {}
        for name, value in result.outputs.items():
            spec = specs.get(name)
            masked[name] = _SENSITIVITY_MASK.get(spec.sensitivity, value) if spec else value
        data["outputs"] = masked
    if isinstance(result, Failure):
        # Persist only the stable structural signal (code + step id). Free-text
        # expected/observed can carry a raw value; richer diagnostics come from the
        # separately sanitized FailureEvidence.
        data.pop("expected", None)
        data.pop("observed", None)
    return data
