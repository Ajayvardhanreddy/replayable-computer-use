"""Naive but typed compiler: a successful discovery trace -> a Capability artifact.

Provenance is preserved by construction: a step's value is the ValueRef recorded
during discovery (e.g. ParameterRef(member_number)), never a raw invocation value
searched out of a transcript. Postconditions are synthesized only from a genuine
observed before/after delta, and authored business outcomes attach to the specific
transition declared to produce them. The output is a validated Capability contract
(the model layer enforces the artifact-alone invariants), not a click log.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from computer_use.model import (
    Action,
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ExtractAction,
    Heading,
    InputSpec,
    Outcome,
    OutputSpec,
    ProposedActionType,
    Step,
    TargetDescriptor,
    TypeAction,
)

from .trace import DiscoveryTrace, TraceStep


class CapabilityValidationError(Exception):
    """A trace + spec cannot be compiled into a valid capability."""


class OutcomeBinding(BaseModel):
    """Binds an authored alternative outcome to the transition that produces it.

    An outcome attaches by declared intent (this action on this target), never by
    coincidental proximity to a landmark. The compiler requires exactly one
    matching transition in the trace and fails closed on zero or several.
    """

    model_config = ConfigDict(extra="forbid")
    action: ProposedActionType
    target: TargetDescriptor
    outcome: Outcome


class GoalSpec(BaseModel):
    """The declared shape of a capability: what it needs and returns."""

    model_config = ConfigDict(extra="forbid")
    capability_id: str
    goal: str
    target: CapabilityTarget
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    success_output: str
    # Authored (not discovered) alternative outcomes, each bound to the transition
    # that can produce it (e.g. the Search click produces MEMBER_NOT_FOUND).
    business_outcomes: list[OutcomeBinding] = Field(default_factory=list)


def _artifact_action(step: TraceStep) -> Action:
    if step.action is ProposedActionType.CLICK:
        return ClickAction()
    if step.action is ProposedActionType.TYPE:
        if step.value is None:
            raise ValueError("type trace step is missing its value ref")
        return TypeAction(value=step.value)
    if step.action is ProposedActionType.EXTRACT:
        return ExtractAction()
    raise ValueError(f"non-executable action in trace: {step.action.value}")


def _target_identity(target: TargetDescriptor) -> tuple[object, ...]:
    """The identity fields of a target, ignoring ``frame`` (orthogonal context)."""
    cell = target.table_cell
    return (
        target.role,
        target.name,
        target.label,
        target.text,
        (cell.row_contains, cell.column_header) if cell else None,
    )


def _synthesized_postcondition(trace_step: TraceStep) -> Condition | None:
    """A heading checkpoint only for a genuine before/after delta.

    The landmark must have actually changed relative to the pre-action heading, so
    the compiler never asserts a condition that was already true before the action.
    When no transition was observed (e.g. a navigation slower than the discovery
    heading-change wait), no checkpoint is synthesized; an outcome-bound step then
    fails validation and compilation stops, rather than emitting a branch with no
    normal-path checkpoint.
    """
    landmark = trace_step.observed_landmark
    if landmark and landmark != trace_step.heading_before:
        return Condition(heading=Heading(role="heading", name=landmark))
    return None


def _resolve_outcome_bindings(
    trace: DiscoveryTrace, bindings: list[OutcomeBinding]
) -> dict[int, list[Outcome]]:
    by_index: dict[int, list[Outcome]] = {}
    for binding in bindings:
        matches = [
            index
            for index, trace_step in enumerate(trace.steps)
            if trace_step.action is binding.action
            and _target_identity(trace_step.target) == _target_identity(binding.target)
        ]
        if len(matches) != 1:
            raise CapabilityValidationError(
                f"outcome {binding.outcome.code!r} must bind to exactly one "
                f"{binding.action.value} transition, but matched {len(matches)}"
            )
        by_index.setdefault(matches[0], []).append(binding.outcome)
    return by_index


def compile_capability(trace: DiscoveryTrace, spec: GoalSpec, version: int = 1) -> Capability:
    outcomes_by_index = _resolve_outcome_bindings(trace, spec.business_outcomes)
    steps: list[Step] = []
    for index, trace_step in enumerate(trace.steps):
        steps.append(
            Step(
                id=f"step_{index + 1}_{trace_step.action.value}",
                action=_artifact_action(trace_step),
                target=trace_step.target,
                risk=trace_step.risk,
                postcondition=_synthesized_postcondition(trace_step),
                outcomes=outcomes_by_index.get(index, []),
                output=trace_step.output,
            )
        )
    # Capability construction enforces the artifact-alone static invariants
    # (provenance cross-reference, output production, matcher allowlist, ...).
    return Capability(
        id=spec.capability_id,
        version=version,
        target=spec.target,
        inputs=spec.inputs,
        outputs=spec.outputs,
        steps=steps,
        success_checkpoint=Condition(output_present=spec.success_output),
    )
