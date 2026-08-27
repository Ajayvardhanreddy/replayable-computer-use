"""The discovery loop: observe -> model proposes -> TrustedKernel executes -> trace.

The model only ever proposes; the kernel owns authority and execution. The raw
invocation values live in the kernel's ValueResolver and are never sent to the model.
Each turn the model is told which actions it has already completed, and a repeated
identical action trips a bounded "stuck" stop so a loop cannot burn the step budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from computer_use.execution import (
    ApprovalGrant,
    ApprovalRequest,
    ApprovalRequired,
    ControlLease,
    InterventionSignal,
    KernelRejection,
    RejectionCode,
    TrustedKernel,
)
from computer_use.handoff import OperatorController
from computer_use.model import PolicyEffect, ProposedActionType, TargetDescriptor
from computer_use.observability import (
    EvidenceStore,
    consequential_approval_event,
    discovery_finished_event,
    discovery_started_event,
    intervention_raised_event,
    step_executed_event,
    step_rejected_event,
)
from computer_use.safety import NavigationPolicy, route_label
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

# A handler drives the human takeover on the same live session and returns True when
# the human resolved the block and discovery should continue, False to stop. It receives the
# model's own concise escalation reason (the `request_human` reason, not chain-of-thought) so
# the operator surface can show why the agent asked for a human.
HumanRequestHandler = Callable[[OperatorController, str | None], Awaitable[bool]]

# A handler obtains a human decision for a consequential action. It returns a one-time
# ApprovalGrant to authorize this exact operation, or None to refuse (the model is then
# steered to escalate; it can never authorize itself).
ConsequentialApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalGrant | None]]

_STUCK_LIMIT = 5
_REPEAT_LIMIT = 3

# Human-readable feedback the untrusted model receives when the trusted kernel refuses
# an action, phrased so a genuinely blocked action steers toward escalation rather than
# a futile retry. Generic to the rejection class, never to any one screen.
_REJECTION_GUIDANCE: dict[RejectionCode, str] = {
    RejectionCode.RISK_CONFIRMATION_REQUIRED: (
        "that action was blocked because it makes a change requiring confirmation or "
        "authorization you have not been given. You cannot complete this goal on your own "
        "with the controls available; do not try other controls to get around it — propose "
        "request_human"
    ),
}


def _rejection_message(rejection: KernelRejection) -> str:
    guidance = _REJECTION_GUIDANCE.get(rejection.code)
    if guidance is not None:
        return guidance
    if rejection.detail:
        return f"{rejection.code.value}: {rejection.detail}"
    return rejection.code.value


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
        filled=candidate.filled,
    )


def _describe(action: ProposedActionType, target: TargetDescriptor) -> str:
    if target.table_cell is not None:
        location = f"cell[{target.table_cell.row_contains}/{target.table_cell.column_header}]"
    elif target.name:
        location = f"{target.role}:{target.name}"
    else:
        location = target.role or "element"
    return f"{action.value} {location}"


class _DiscoveryHandoff:
    """A handoff view of the live discovery session for the operator control path.

    Exposes exactly what ``OperatorController`` needs (the ``HandoffSession`` shape),
    reusing the same lease, surface, and evidence boundary as replay handoff.
    """

    def __init__(
        self, run_id: str, surface: Surface, lease: ControlLease, nav_policy: NavigationPolicy
    ) -> None:
        self.run_id = run_id
        self.surface = surface
        self.lease = lease
        self._nav_policy = nav_policy
        self.pending: InterventionSignal | None = None

    @property
    def nav_policy(self) -> NavigationPolicy:
        return self._nav_policy

    async def current_route(self) -> str:
        return await self.surface.current_route()

    def route_label(self, path: str) -> str:
        return route_label(path, self._nav_policy.allowed_routes)


async def discover(
    model: DiscoveryModel,
    surface: Surface,
    kernel: TrustedKernel,
    spec: GoalSpec,
    target_url: str,
    *,
    nav_policy: NavigationPolicy,
    max_steps: int = 12,
    evidence: EvidenceStore | None = None,
    lease: ControlLease | None = None,
    on_human_request: HumanRequestHandler | None = None,
    on_consequential_approval: ConsequentialApprovalHandler | None = None,
) -> DiscoveryOutcome:
    run_id = f"run_{uuid4().hex[:8]}"
    # Same-session human handoff for a genuinely stuck model: available only when a
    # lease and a handler are supplied. Otherwise a request-human proposal stops the
    # run with a typed reason, as before.
    handoff: _DiscoveryHandoff | None = None
    operator: OperatorController | None = None
    if lease is not None and on_human_request is not None:
        handoff = _DiscoveryHandoff(run_id, surface, lease, nav_policy)
        operator = OperatorController(handoff, evidence=evidence)
    goal_ctx = _goal_context(spec)
    entry = target_url.rstrip("/") + "/"
    # Navigation scope is mandatory and fail-closed for discovery too.
    if nav_policy.check(entry).effect is PolicyEffect.DENY:
        return DiscoveryOutcome(
            trace=DiscoveryTrace(steps=[]), model_calls=0, stop_reason="OUT_OF_SCOPE"
        )
    await surface.goto(entry)
    if evidence is not None:
        evidence.write(
            discovery_started_event(
                run_id, model.provider, model.model_id, spec.capability_id, spec.goal
            )
        )

    trace_steps: list[TraceStep] = []
    history: list[str] = []
    obtained_outputs: set[str] = set()
    model_calls = 0
    consecutive_errors = 0
    last_signature: str | None = None
    repeat_count = 0
    last_error: str | None = None
    stop_reason = "MAX_STEPS"

    for step_index in range(max_steps):
        await surface.wait_settled()
        if nav_policy.check(await surface.current_url()).effect is PolicyEffect.DENY:
            stop_reason = "OUT_OF_SCOPE"
            break
        obs = await surface.observe()
        by_id = {candidate.id: candidate for candidate in obs.candidates}
        model_obs = ModelObservation(
            # The concrete path may carry a sensitive parameter (a member id); the
            # model only needs the structural route, so egress the allowed pattern.
            route=route_label(obs.route, nav_policy.allowed_routes),
            candidates=[_minimize(candidate) for candidate in obs.candidates],
            actions_taken=list(history),
            obtained_outputs=sorted(obtained_outputs),
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
            # The model may PROPOSE success, but software ratifies it: the goal is only
            # reached once the required declared output has actually been obtained.
            if spec.success_output in obtained_outputs:
                stop_reason = "GOAL_REACHED"
                break
            last_error = f"cannot declare success: output '{spec.success_output}' not obtained"
            consecutive_errors += 1
            if evidence is not None:
                evidence.write(step_rejected_event(run_id, "SUCCESS_PRECONDITION_FAILED"))
            if consecutive_errors >= _STUCK_LIMIT:
                stop_reason = "STUCK"
                break
            continue
        if proposal.action is ProposedActionType.REQUEST_HUMAN:
            # The model judged it cannot safely proceed. With no handoff wired, stop
            # with a typed reason; otherwise pause and hand the SAME live session to a
            # human, then re-observe and let the model continue.
            if handoff is None or operator is None or on_human_request is None:
                stop_reason = "HUMAN_REQUESTED"
                break
            handoff.pending = InterventionSignal(
                reason="HUMAN_REQUESTED",
                run_id=run_id,
                capability=spec.capability_id,
                version=1,
                step_id=None,
                intervention_id=f"int_{uuid4().hex[:8]}",
                epoch=handoff.lease.epoch,
            )
            if evidence is not None:
                evidence.write(intervention_raised_event(run_id, "HUMAN_REQUESTED", model_calls))
            try:
                resolved = await on_human_request(operator, proposal.reason)
            finally:
                handoff.pending = None
            if not resolved:
                stop_reason = "HUMAN_REQUESTED"
                break
            # Resolved on the same session: discard the stale pre-handoff observation
            # and loop, forcing a fresh observation and a new model decision.
            last_error = None
            continue
        if proposal.action is ProposedActionType.OBSERVE:
            last_error = None
            continue
        if proposal.action is ProposedActionType.EXTRACT and proposal.output not in spec.outputs:
            # The model cannot invent an output; it must bind one the capability declares.
            last_error = f"unknown output {proposal.output!r}; declared: {sorted(spec.outputs)}"
            consecutive_errors += 1
            if evidence is not None:
                evidence.write(step_rejected_event(run_id, "UNKNOWN_OUTPUT"))
            if consecutive_errors >= _STUCK_LIMIT:
                stop_reason = "STUCK"
                break
            continue
        if proposal.action is ProposedActionType.EXTRACT and proposal.output in obtained_outputs:
            # Already obtained; re-extracting is not progress. Nudge toward declaring
            # success without recording a duplicate step (bounded by max_steps).
            last_error = (
                f"output {proposal.output!r} already obtained; declare success "
                "or extract a different declared output"
            )
            if evidence is not None:
                evidence.write(step_rejected_event(run_id, "OUTPUT_ALREADY_OBTAINED"))
            continue

        heading_before: str | None = None
        if proposal.action is ProposedActionType.CLICK:
            heading_before = await surface.primary_heading()
        epoch = lease.epoch if lease is not None else None
        try:
            execution = await kernel.execute(proposal, by_id, epoch=epoch)
        except ApprovalRequired as required:
            # A consequential action: authority to commit comes from a human, never
            # the model. Obtain a one-time grant, then re-invoke the kernel, which
            # re-resolves and re-fingerprints the operation before dispatching once.
            grant = (
                await on_consequential_approval(required.request)
                if on_consequential_approval is not None
                else None
            )
            decision = "approved" if grant is not None else "denied"
            if evidence is not None:
                evidence.write(
                    consequential_approval_event(run_id, decision, required.request, model_calls)
                )
            if grant is None:
                last_error = (
                    "that action makes a consequential change and was not authorized. You "
                    "cannot authorize it yourself; do not try other controls — propose "
                    "request_human"
                )
                consecutive_errors += 1
                if consecutive_errors >= _STUCK_LIMIT:
                    stop_reason = "STUCK"
                    break
                continue
            try:
                execution = await kernel.execute(proposal, by_id, epoch=epoch, approval=grant)
            except KernelRejection as rejection:
                # e.g. APPROVAL_STALE: the operation moved between request and grant.
                last_error = _rejection_message(rejection)
                consecutive_errors += 1
                if evidence is not None:
                    evidence.write(step_rejected_event(run_id, rejection.code.value))
                if consecutive_errors >= _STUCK_LIMIT:
                    stop_reason = "STUCK"
                    break
                continue
        except KernelRejection as rejection:
            last_error = _rejection_message(rejection)
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
            # A click may navigate; re-check scope before recording a landmark from
            # the resulting page into the trace/artifact.
            if nav_policy.check(await surface.current_url()).effect is PolicyEffect.DENY:
                stop_reason = "OUT_OF_SCOPE"
                break
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
                route=model_obs.route,
                observed_landmark=landmark,
                heading_before=heading_before,
                expected_effect=proposal.expected_effect,
            )
        )
        history.append(signature)
        if (
            execution.action is ProposedActionType.EXTRACT
            and execution.extracted is not None
            and proposal.output is not None
        ):
            obtained_outputs.add(proposal.output)
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
