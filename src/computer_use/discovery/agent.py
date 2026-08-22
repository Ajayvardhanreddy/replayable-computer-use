"""The discovery loop: observe -> model proposes -> TrustedKernel executes -> trace.

The model only ever proposes; the kernel owns authority and execution. The raw
invocation values live in the kernel's ValueResolver and are never sent to the model.
Each turn the model is told which actions it has already completed, and a repeated
identical action trips a bounded "stuck" stop so a loop cannot burn the step budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from computer_use.execution import KernelRejection, TrustedKernel
from computer_use.model import ProposedActionType, TargetDescriptor
from computer_use.observability import (
    EvidenceStore,
    discovery_finished_event,
    discovery_started_event,
    step_executed_event,
    step_rejected_event,
)
from computer_use.surface import Candidate, Surface

from .compiler import GoalSpec
from .model import (
    DiscoveryModel,
    GoalContext,
    InputInfo,
    ModelCandidate,
    ModelObservation,
    ModelOutputError,
)
from .trace import DiscoveryTrace, TraceStep

_STUCK_LIMIT = 3
_REPEAT_LIMIT = 3


@dataclass
class DiscoveryOutcome:
    trace: DiscoveryTrace
    model_calls: int
    stop_reason: str


def _goal_context(spec: GoalSpec) -> GoalContext:
    inputs = [
        InputInfo(
            name=name, type=spec_in.type.value, sensitivity=spec_in.sensitivity.value,
            required=spec_in.required,
        )
        for name, spec_in in spec.inputs.items()
    ]
    return GoalContext(goal=spec.goal, inputs=inputs, outputs=list(spec.outputs))


def _minimize(candidate: Candidate) -> ModelCandidate:
    # Omit `text`: the model chooses cells by row/column, not by their (financial) value.
    return ModelCandidate(
        id=candidate.id, role=candidate.role, name=candidate.name,
        frame=candidate.frame, row=candidate.row, column=candidate.column,
    )


def _describe(action: ProposedActionType, target: TargetDescriptor) -> str:
    if target.table_cell is not None:
        location = f"cell[{target.table_cell.row_contains}/{target.table_cell.column_header}]"
    elif target.name:
        location = f"{target.role}:{target.name}"
    else:
        location = target.role or "element"
    return f"{action.value} {location}"


async def discover(
    model: DiscoveryModel,
    surface: Surface,
    kernel: TrustedKernel,
    spec: GoalSpec,
    target_url: str,
    max_steps: int = 12,
    evidence: EvidenceStore | None = None,
) -> DiscoveryOutcome:
    run_id = f"run_{uuid4().hex[:8]}"
    goal_ctx = _goal_context(spec)
    await surface.goto(target_url.rstrip("/") + "/")
    if evidence is not None:
        evidence.write(
            discovery_started_event(
                run_id, model.provider, model.model_id, spec.capability_id, spec.goal
            )
        )

    trace_steps: list[TraceStep] = []
    history: list[str] = []
    model_calls = 0
    consecutive_errors = 0
    last_signature: str | None = None
    repeat_count = 0
    last_error: str | None = None
    stop_reason = "MAX_STEPS"

    for step_index in range(max_steps):
        await surface.wait_settled()
        obs = await surface.observe()
        by_id = {candidate.id: candidate for candidate in obs.candidates}
        model_obs = ModelObservation(
            route=obs.route,
            candidates=[_minimize(candidate) for candidate in obs.candidates],
            actions_taken=list(history),
            last_error=last_error,
            steps_remaining=max_steps - step_index,
        )
        model_calls += 1
        try:
            proposal = await model.decide(goal_ctx, model_obs)
        except ModelOutputError as error:
            last_error = f"previous reply was not valid JSON: {error}"
            consecutive_errors += 1
            if evidence is not None:
                evidence.write(step_rejected_event(run_id, "MODEL_OUTPUT_INVALID"))
            if consecutive_errors >= _STUCK_LIMIT:
                stop_reason = "STUCK"
                break
            continue

        if proposal.action is ProposedActionType.DECLARE_SUCCESS:
            stop_reason = "GOAL_REACHED"
            break
        if proposal.action is ProposedActionType.REQUEST_HUMAN:
            stop_reason = "HUMAN_REQUESTED"
            break
        if proposal.action is ProposedActionType.OBSERVE:
            last_error = None
            continue

        heading_before: str | None = None
        if proposal.action is ProposedActionType.CLICK:
            heading_before = await surface.primary_heading()
        try:
            execution = await kernel.execute(proposal, by_id)
        except KernelRejection as rejection:
            last_error = f"{rejection.code.value}: {rejection.detail}"
            consecutive_errors += 1
            if evidence is not None:
                evidence.write(step_rejected_event(run_id, rejection.code.value))
            if consecutive_errors >= _STUCK_LIMIT:
                stop_reason = "STUCK"
                break
            continue

        consecutive_errors = 0
        last_error = None
        signature = _describe(execution.action, execution.target)
        landmark: str | None = None
        if execution.action is ProposedActionType.CLICK:
            await surface.wait_settled()
            # Record the heading only after it actually changes, so a slow navigation
            # doesn't capture the stale (pre-click) heading as the checkpoint.
            landmark = await surface.wait_for_heading_change(heading_before)
        trace_steps.append(
            TraceStep(
                action=execution.action,
                target=execution.target,
                risk=execution.risk,
                value=execution.value,
                output=proposal.output,
                observed_landmark=landmark,
            )
        )
        history.append(signature)
        if evidence is not None:
            evidence.write(
                step_executed_event(run_id, len(trace_steps), execution, proposal.output)
            )

        # Bounded no-progress guard: the same action repeating is a dead loop.
        if signature == last_signature:
            repeat_count += 1
        else:
            repeat_count = 1
            last_signature = signature
        if repeat_count >= _REPEAT_LIMIT:
            stop_reason = "STUCK"
            break

    if evidence is not None:
        evidence.write(discovery_finished_event(run_id, model_calls, stop_reason))
    return DiscoveryOutcome(
        trace=DiscoveryTrace(steps=trace_steps), model_calls=model_calls, stop_reason=stop_reason
    )
