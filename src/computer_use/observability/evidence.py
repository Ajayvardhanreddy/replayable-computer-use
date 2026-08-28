"""JSONL evidence store and safe event builders.

Evidence never persists API keys, environment variables, model reasoning, or raw
sensitive values. Action values are recorded symbolically (e.g. <param:member_number>);
extracted financial values are not persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from computer_use.execution import ApprovalRequest, KernelExecution, ReplayEvent
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
    "control_transferred": frozenset(
        {"from_owner", "to_owner", "epoch", "reason", "operator_id"}
    ),
    "human_action": frozenset({"epoch", "action", "target", "route", "value", "operator_id"}),
    "intervention_raised": frozenset({"reason", "model_call"}),
    "mutation_verified": frozenset({"step_id", "effect_state"}),
    "consequential_approval": frozenset(
        {"decision", "action", "target", "risk", "epoch", "model_call"}
    ),
    "replay_started": frozenset(),
    "step_replayed": frozenset({"step_id", "action", "checkpoint_satisfied"}),
    "replay_finished": frozenset({"result_kind", "model_calls"}),
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


def consequential_approval_event(
    run_id: str, decision: str, request: ApprovalRequest, model_call: int
) -> EvidenceEvent:
    """Audit a human authorization decision for a consequential action.

    Structural only: the action, a target fingerprint, the risk class, the control
    epoch, and the model-call index. The landmark (which may carry a member id) is
    used only for the live staleness check and is never persisted.
    """
    fp = request.fingerprint
    if fp.row_contains is not None:
        target = f"cell[{fp.row_contains}/{fp.column_header}]"
    elif fp.target_name:
        target = f"{fp.target_role or 'element'}:{fp.target_name}"
    else:
        target = fp.target_role or "element"
    return EvidenceEvent(
        event="consequential_approval",
        run_id=run_id,
        ts=_now(),
        attributes={
            "decision": decision,
            "action": fp.action,
            "target": target,
            "risk": request.risk.value,
            "epoch": fp.epoch,
            "model_call": model_call,
        },
    )


def discovery_finished_event(run_id: str, model_calls: int, stop_reason: str) -> EvidenceEvent:
    return EvidenceEvent(
        event="discovery_finished",
        run_id=run_id,
        ts=_now(),
        attributes={"model_calls": model_calls, "stop_reason": stop_reason},
    )


def control_transferred_event(
    run_id: str,
    from_owner: str,
    to_owner: str,
    epoch: int,
    operator_id: str,
    reason: str | None = None,
) -> EvidenceEvent:
    """Records an ownership transfer of the live session (automation <-> human)."""
    return EvidenceEvent(
        event="control_transferred",
        run_id=run_id,
        ts=_now(),
        attributes={
            "from_owner": from_owner,
            "to_owner": to_owner,
            "epoch": epoch,
            "operator_id": operator_id,
            "reason": reason,
        },
    )


def intervention_raised_event(run_id: str, reason: str, model_call: int) -> EvidenceEvent:
    """Records that a run paused for a human, with the stable reason and the model-call
    index at which it happened. No model reasoning text is persisted."""
    return EvidenceEvent(
        event="intervention_raised",
        run_id=run_id,
        ts=_now(),
        attributes={"reason": reason, "model_call": model_call},
    )


def replay_evidence_event(event: ReplayEvent) -> EvidenceEvent:
    """Map a runtime replay event to a persisted evidence event: what started, which steps
    executed and whether their checkpoints held, a mutation's verified effect state, and the
    terminal result. Structural attributes only — the write-boundary allowlist drops anything
    else. The runtime owns the facts; this layer owns persistence, so execution never couples
    to JSONL."""
    attributes: dict[str, str | int | bool | None] = {}
    if event.step_id is not None:
        attributes["step_id"] = event.step_id
    if event.action_kind is not None:
        attributes["action"] = event.action_kind
    if event.checkpoint_satisfied is not None:
        attributes["checkpoint_satisfied"] = event.checkpoint_satisfied
    if event.effect_state is not None:
        attributes["effect_state"] = event.effect_state
    if event.result_kind is not None:
        attributes["result_kind"] = event.result_kind
    if event.model_calls is not None:
        attributes["model_calls"] = event.model_calls
    return EvidenceEvent(
        event=event.kind,
        run_id=event.run_id or "",
        ts=_now(),
        step_id=event.step_id,
        attributes=attributes,
    )


def human_action_event(
    run_id: str,
    epoch: int,
    operator_id: str,
    action: str,
    target: TargetDescriptor,
    route: str,
    value_present: bool,
) -> EvidenceEvent:
    """Records a human operator action with safe metadata only.

    The target is a structural fingerprint (role/name or cell relation) and the
    route is an allowed-route pattern. Any value the human entered is recorded as a
    presence token, never the raw value — human input crosses the same allowlist
    boundary as everything else.
    """
    return EvidenceEvent(
        event="human_action",
        run_id=run_id,
        ts=_now(),
        attributes={
            "epoch": epoch,
            "operator_id": operator_id,
            "action": action,
            "target": _target_summary(target),
            "route": route,
            "value": "<redacted>" if value_present else None,
        },
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
