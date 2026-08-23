import pytest
from pydantic import ValidationError

from computer_use.discovery import (
    CapabilityValidationError,
    DiscoveryTrace,
    GoalSpec,
    OutcomeBinding,
    TraceStep,
    compile_capability,
)
from computer_use.model import (
    Capability,
    CapabilityTarget,
    Condition,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedActionType,
    RiskClass,
    Sensitivity,
    TableCellTarget,
    TargetDescriptor,
)


def _trace(
    *, landmark: str | None = "Member Profile", heading_before: str | None = None
) -> DiscoveryTrace:
    return DiscoveryTrace(
        steps=[
            TraceStep(
                action=ProposedActionType.TYPE,
                target=TargetDescriptor(role="textbox", name="Member Number", frame="lc-workspace"),
                risk=RiskClass.READ_ONLY,
                value=ParameterRef(name="member_number"),
            ),
            TraceStep(
                action=ProposedActionType.CLICK,
                target=TargetDescriptor(role="button", name="Search", frame="lc-workspace"),
                risk=RiskClass.READ_ONLY,
                observed_landmark=landmark,
                heading_before=heading_before,
            ),
            TraceStep(
                action=ProposedActionType.EXTRACT,
                target=TargetDescriptor(
                    frame="lc-workspace",
                    table_cell=TableCellTarget(
                        row_contains="Share Savings", column_header="Current Balance"
                    ),
                ),
                risk=RiskClass.READ_ONLY,
                output="savings_balance",
            ),
        ]
    )


def _member_not_found() -> OutcomeBinding:
    return OutcomeBinding(
        action=ProposedActionType.CLICK,
        target=TargetDescriptor(role="button", name="Search"),
        outcome=Outcome(
            code="MEMBER_NOT_FOUND",
            outcome_class=OutcomeClass.BUSINESS_OUTCOME,
            detector=Condition(text_present="Member record not found"),
        ),
    )


def _spec(*, bindings: list[OutcomeBinding] | None = None) -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal="Look up this member and return their savings balance",
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        success_output="savings_balance",
        business_outcomes=[_member_not_found()] if bindings is None else bindings,
    )


def test_compiler_produces_a_valid_capability() -> None:
    capability = compile_capability(_trace(), _spec())
    assert Capability.model_validate_json(capability.model_dump_json()) == capability
    assert capability.success_checkpoint.output_present == "savings_balance"
    assert len(capability.steps) == 3


def test_compiler_preserves_parameter_provenance_not_literals() -> None:
    dumped = compile_capability(_trace(), _spec()).model_dump_json()
    assert '"source":"parameter"' in dumped  # ParameterRef is preserved
    assert "member_number" in dumped
    # a concrete invocation value must never be compiled into the reusable artifact
    assert "12345" not in dumped
    assert "54321" not in dumped


def test_compiler_binds_outcome_to_its_declared_transition() -> None:
    capability = compile_capability(_trace(), _spec())
    nav_step = capability.steps[1]  # the Search click
    assert nav_step.action.type == "click"
    assert [outcome.code for outcome in nav_step.outcomes] == ["MEMBER_NOT_FOUND"]
    # every other step carries no outcomes
    assert capability.steps[0].outcomes == []
    assert capability.steps[2].outcomes == []


def test_checkpoint_requires_a_real_before_after_delta() -> None:
    # When the heading did not actually change, no heading checkpoint is synthesized.
    trace = _trace(landmark="Member Inquiry", heading_before="Member Inquiry")
    capability = compile_capability(trace, _spec(bindings=[]))
    assert capability.steps[1].postcondition is None


def test_bound_outcome_without_observed_transition_fails_closed() -> None:
    # No heading delta on the Search click -> no synthesized postcondition; a bound
    # outcome on that step then cannot compile, rather than a checkpoint-less branch.
    trace = _trace(landmark="Member Inquiry", heading_before="Member Inquiry")
    with pytest.raises(ValidationError, match="normal-path postcondition"):
        compile_capability(trace, _spec())


def test_outcome_binding_must_match_exactly_one_transition() -> None:
    stray = OutcomeBinding(
        action=ProposedActionType.CLICK,
        target=TargetDescriptor(role="button", name="Nonexistent"),
        outcome=Outcome(
            code="X",
            outcome_class=OutcomeClass.BUSINESS_OUTCOME,
            detector=Condition(text_present="nope"),
        ),
    )
    with pytest.raises(CapabilityValidationError):
        compile_capability(_trace(), _spec(bindings=[stray]))
