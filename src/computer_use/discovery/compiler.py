"""Naive but typed compiler: a successful discovery trace -> a Capability artifact.

Provenance is preserved by construction: a step's value is the ValueRef recorded
during discovery (e.g. ParameterRef(member_number)), never a raw invocation value
searched out of a transcript. Postconditions are synthesized only from a genuine
observed before/after delta, and authored business outcomes attach to the specific
transition declared to produce them. The output is a validated Capability contract
(the model layer enforces the artifact-alone invariants), not a click log.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    MutationVerification,
    Outcome,
    OutputSpec,
    ProposedActionType,
    RiskClass,
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


@dataclass(frozen=True)
class VerificationProvenance:
    """Which discovery steps became a write's embedded verification, so it is visible
    that the model discovered the re-derivation rather than the compiler inventing it.

    Indices are 1-based discovery step positions, matching the ``step_executed`` events
    in the discovery trace evidence.
    """

    write_step_index: int
    navigate_step_indices: list[int]
    extract_step_index: int | None


def verification_provenance(
    trace: DiscoveryTrace, spec: GoalSpec
) -> VerificationProvenance | None:
    """The trace-step provenance of the compiled verification, or None for a read-only
    capability. Mirrors ``_build_verification`` so the mapping is exact, not inferred."""
    write_index = _consequential_index(trace)
    if write_index is None:
        return None
    post = trace.steps[write_index + 1 :]
    terminals = [
        i
        for i, ts in enumerate(post)
        if ts.action is ProposedActionType.EXTRACT and ts.output == spec.success_output
    ]
    if len(terminals) != 1:
        return None
    terminal = terminals[0]
    return VerificationProvenance(
        write_step_index=write_index + 1,
        navigate_step_indices=[write_index + 2 + offset for offset in range(terminal)],
        extract_step_index=write_index + 2 + terminal,
    )


def _artifact_step(trace_step: TraceStep, step_id: str) -> Step:
    return Step(
        id=step_id,
        action=_artifact_action(trace_step),
        target=trace_step.target,
        risk=trace_step.risk,
        postcondition=_synthesized_postcondition(trace_step),
        output=trace_step.output,
    )


def _consequential_index(trace: DiscoveryTrace) -> int | None:
    """The single consequential write, or None for a read-only capability.

    Fail closed on more than one: this capability class supports exactly one mutation,
    never a multi-write workflow. Generic — keyed on software-derived risk, not labels.
    """
    indices = [i for i, s in enumerate(trace.steps) if s.risk is not RiskClass.READ_ONLY]
    if not indices:
        return None
    if len(indices) > 1:
        raise CapabilityValidationError(
            f"exactly one consequential write is supported; the trace has {len(indices)}"
        )
    return indices[0]


def _effect_condition(target: TargetDescriptor | None) -> Condition:
    """The effect matcher derived from the model's own verification-extract target.

    The discovered target *is* the effect identity; the compiler captures it, never a
    hard-coded label. A relational cell is proven by the presence of its row identity.
    """
    if target is None:
        raise CapabilityValidationError("verification extract has no target to derive an effect")
    if target.table_cell is not None:
        return Condition(text_present=target.table_cell.row_contains)
    if target.name:
        return Condition(text_present=target.name)
    if target.text:
        return Condition(text_present=target.text)
    raise CapabilityValidationError("verification extract target has no stable effect identity")


def _page_condition(navigate: list[TraceStep], terminal: TraceStep) -> Condition:
    """Identify the effect view so a baseline can be evaluated on it (never on the
    commit form). Prefer a discovered heading landmark; fall back to the view's route."""
    for trace_step in reversed(navigate):
        if trace_step.observed_landmark:
            return Condition(heading=Heading(role="heading", name=trace_step.observed_landmark))
    if terminal.route:
        return Condition(route_pattern=terminal.route)
    raise CapabilityValidationError("verification view could not be identified from the trace")


def _build_verification(
    trace: DiscoveryTrace, spec: GoalSpec, write_index: int
) -> MutationVerification:
    """Compile the post-write read-only sub-trace into an embedded verification recipe.

    Generic and fail-closed: the segment after the single consequential write must be
    wholly read-only and culminate in exactly one extract of the declared success
    output — the independent confirmation the model discovered. Zero or several such
    extracts is ambiguous and stops compilation.
    """
    post = trace.steps[write_index + 1 :]
    if not post:
        raise CapabilityValidationError(
            "a consequential write must be followed by an independent verification"
        )
    for trace_step in post:
        if trace_step.risk is not RiskClass.READ_ONLY:
            raise CapabilityValidationError("every verification step must be read-only")
    terminals = [
        i
        for i, trace_step in enumerate(post)
        if trace_step.action is ProposedActionType.EXTRACT
        and trace_step.output == spec.success_output
    ]
    if len(terminals) != 1:
        raise CapabilityValidationError(
            f"verification must culminate in exactly one extract of "
            f"{spec.success_output!r}; the trace has {len(terminals)}"
        )
    terminal_index = terminals[0]
    navigate_traces = post[:terminal_index]
    terminal_trace = post[terminal_index]
    return MutationVerification(
        navigate=[
            _artifact_step(ts, f"verify_nav_{i + 1}_{ts.action.value}")
            for i, ts in enumerate(navigate_traces)
        ],
        page=_page_condition(navigate_traces, terminal_trace),
        effect_present=_effect_condition(terminal_trace.target),
        extract=_artifact_step(terminal_trace, "verify_extract"),
    )


def _baseline_reachable(pre_write: list[TraceStep], page: Condition) -> bool:
    """The write's effect view must be reached on the normal path before the write, so a
    trusted pre-dispatch baseline (effect-absent) can be evaluated there. Without it, a
    present effect after the write is unattributable to this write."""
    if page.heading is not None:
        return any(step.observed_landmark == page.heading.name for step in pre_write)
    if page.route_pattern is not None:
        return any(step.route == page.route_pattern for step in pre_write)
    return False


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
    # A consequential write's independent verification is the read-only sub-trace that
    # follows it. It is compiled into an embedded recipe on the write step, and the
    # post-write steps are lifted out of the top-level sequence (one canonical copy,
    # no duplication). A read-only capability has no write and compiles flat as before.
    write_index = _consequential_index(trace)
    verification = (
        _build_verification(trace, spec, write_index) if write_index is not None else None
    )
    # A consequential write's effect is only attributable if a baseline (effect-absent) can be
    # established before it — which requires the effect view to be reached on the normal path
    # before the write. Enforce that at compile time rather than relying on the trace
    # incidentally visiting it; otherwise a good commit would replay as MUTATION_AMBIGUOUS.
    if (
        verification is not None
        and write_index is not None
        and not _baseline_reachable(trace.steps[:write_index], verification.page)
    ):
        raise CapabilityValidationError(
            "the write's effect view is never reached before the write, so a pre-dispatch "
            "baseline cannot be established and a later present effect would be unattributable"
        )
    top_level = write_index + 1 if write_index is not None else len(trace.steps)
    steps: list[Step] = []
    for index in range(top_level):
        trace_step = trace.steps[index]
        steps.append(
            Step(
                id=f"step_{index + 1}_{trace_step.action.value}",
                action=_artifact_action(trace_step),
                target=trace_step.target,
                risk=trace_step.risk,
                postcondition=_synthesized_postcondition(trace_step),
                outcomes=outcomes_by_index.get(index, []),
                output=trace_step.output,
                verification=verification if index == write_index else None,
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
