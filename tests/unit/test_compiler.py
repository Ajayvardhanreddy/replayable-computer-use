from computer_use.discovery import DiscoveryTrace, GoalSpec, TraceStep, compile_capability
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


def _trace() -> DiscoveryTrace:
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
                observed_landmark="Member Profile",
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


def _spec() -> GoalSpec:
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
        business_outcomes=[
            Outcome(
                code="MEMBER_NOT_FOUND",
                outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                detector=Condition(text_present="Member record not found"),
            )
        ],
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


def test_compiler_attaches_authored_outcome_to_navigation_step() -> None:
    capability = compile_capability(_trace(), _spec())
    nav_step = capability.steps[1]  # the Search click (the step with the observed landmark)
    assert nav_step.action.type == "click"
    assert [outcome.code for outcome in nav_step.outcomes] == ["MEMBER_NOT_FOUND"]
    # non-navigation steps carry no outcomes
    assert capability.steps[0].outcomes == []
    assert capability.steps[2].outcomes == []
