"""Naive but typed compiler: a successful discovery trace -> a Capability artifact.

Provenance is preserved by construction: a step's value is the ValueRef recorded
during discovery (e.g. ParameterRef(member_number)), never a raw invocation value
searched out of a transcript. The output is a validated Capability contract, not a
click log.
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
    TypeAction,
)

from .trace import DiscoveryTrace, TraceStep


class GoalSpec(BaseModel):
    """The declared shape of a capability: what it needs and returns."""

    model_config = ConfigDict(extra="forbid")
    capability_id: str
    goal: str
    target: CapabilityTarget
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    success_output: str
    # Authored (not discovered) alternative outcomes for this capability, e.g. MEMBER_NOT_FOUND.
    business_outcomes: list[Outcome] = Field(default_factory=list)


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


def compile_capability(trace: DiscoveryTrace, spec: GoalSpec, version: int = 1) -> Capability:
    steps: list[Step] = []
    for index, trace_step in enumerate(trace.steps, start=1):
        postcondition = None
        outcomes: list[Outcome] = []
        if trace_step.observed_landmark:
            postcondition = Condition(
                heading=Heading(role="heading", name=trace_step.observed_landmark)
            )
            # Authored business outcomes are the alternative to a navigation checkpoint,
            # so they attach to the step that navigates.
            outcomes = list(spec.business_outcomes)
        steps.append(
            Step(
                id=f"step_{index}_{trace_step.action.value}",
                action=_artifact_action(trace_step),
                target=trace_step.target,
                risk=trace_step.risk,
                postcondition=postcondition,
                outcomes=outcomes,
                output=trace_step.output,
            )
        )
    return Capability(
        id=spec.capability_id,
        version=version,
        target=spec.target,
        inputs=spec.inputs,
        outputs=spec.outputs,
        steps=steps,
        success_checkpoint=Condition(output_present=spec.success_output),
    )
