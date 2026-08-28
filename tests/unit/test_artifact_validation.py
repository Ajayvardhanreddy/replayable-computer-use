"""Static validation is enforced on Capability construction and on load.

These invariants must hold for any artifact, however it was produced — so a
hand-edited or externally supplied capability cannot bypass them at load time.
"""

import pytest
from pydantic import ValidationError

from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    DerivedValue,
    ExtractAction,
    Heading,
    InputSpec,
    MutationVerification,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    RiskClass,
    Step,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)


def _steps() -> list[Step]:
    return [
        Step(
            id="s1",
            action=TypeAction(value=ParameterRef(name="member_number")),
            target=TargetDescriptor(role="textbox", name="Member Number"),
            risk=RiskClass.READ_ONLY,
        ),
        Step(
            id="s2",
            action=ClickAction(),
            target=TargetDescriptor(role="button", name="Search"),
            risk=RiskClass.READ_ONLY,
            postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
        ),
        Step(
            id="s3",
            action=ExtractAction(),
            target=TargetDescriptor(
                table_cell=TableCellTarget(
                    row_contains="Share Savings", column_header="Current Balance"
                )
            ),
            risk=RiskClass.READ_ONLY,
            output="savings_balance",
        ),
    ]


def _capability(steps: list[Step] | None = None, success: str = "savings_balance") -> Capability:
    return Capability(
        id="member.lookup_savings_balance",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING)},
        outputs={"savings_balance": OutputSpec(type=ParamType.DECIMAL)},
        steps=steps if steps is not None else _steps(),
        success_checkpoint=Condition(output_present=success),
    )


def test_valid_capability_round_trips_through_load() -> None:
    capability = _capability()
    assert Capability.model_validate_json(capability.model_dump_json()) == capability


def test_undeclared_parameter_ref_is_rejected_on_load() -> None:
    good = _capability().model_dump_json()
    tampered = good.replace("member_number", "does_not_exist", 1)  # break the ParameterRef only
    with pytest.raises(ValidationError, match="not a declared input"):
        Capability.model_validate_json(tampered)


def test_derived_value_args_are_validated_recursively() -> None:
    steps = _steps()
    steps[0] = Step(
        id="s1",
        action=TypeAction(
            value=DerivedValue(function="upper", args=[ParameterRef(name="ghost")])
        ),
        target=TargetDescriptor(role="textbox", name="Member Number"),
        risk=RiskClass.READ_ONLY,
    )
    with pytest.raises(ValidationError, match="not a declared input"):
        _capability(steps=steps)


def test_success_output_must_be_produced_by_an_extract() -> None:
    steps = _steps()[:2]  # drop the extract step
    steps[1] = Step(  # keep a produced-nothing flow but a valid navigation step
        id="s2",
        action=ClickAction(),
        target=TargetDescriptor(role="button", name="Search"),
        risk=RiskClass.READ_ONLY,
        postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
    )
    with pytest.raises(ValidationError, match="never produced"):
        _capability(steps=steps)


def test_table_cell_target_is_extract_only() -> None:
    with pytest.raises(ValidationError, match="table_cell target is only valid for extract"):
        Step(
            id="bad",
            action=ClickAction(),
            target=TargetDescriptor(
                table_cell=TableCellTarget(
                    row_contains="Share Savings", column_header="Current Balance"
                )
            ),
            risk=RiskClass.READ_ONLY,
        )


def test_fallbacks_must_be_depth_one() -> None:
    with pytest.raises(ValidationError, match="must not be nested"):
        TargetDescriptor(
            role="button",
            name="A",
            fallbacks=[
                TargetDescriptor(
                    role="button",
                    name="B",
                    fallbacks=[TargetDescriptor(role="button", name="C")],
                )
            ],
        )


def test_fallback_obeys_single_identity() -> None:
    with pytest.raises(ValidationError, match="conflicting identity forms"):
        TargetDescriptor(
            role="button",
            name="A",
            fallbacks=[TargetDescriptor(role="button", name="B", text="B")],
        )


def test_table_cell_fallback_on_a_click_is_rejected() -> None:
    with pytest.raises(ValidationError, match="table_cell target is only valid for extract"):
        Step(
            id="bad",
            action=ClickAction(),
            target=TargetDescriptor(
                role="button",
                name="Search",
                fallbacks=[
                    TargetDescriptor(
                        table_cell=TableCellTarget(row_contains="A", column_header="B")
                    )
                ],
            ),
            risk=RiskClass.READ_ONLY,
        )


def test_output_present_nested_in_any_of_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not accepted here"):
        Capability(
            id="member.lookup_savings_balance",
            version=1,
            target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
            inputs={"member_number": InputSpec(type=ParamType.STRING)},
            outputs={"savings_balance": OutputSpec(type=ParamType.DECIMAL)},
            steps=_steps(),
            success_checkpoint=Condition(any_of=[Condition(output_present="savings_balance")]),
        )


def test_authored_outcome_must_be_business_outcome() -> None:
    # recoverable is runtime behavior and hard failure is a Failure; neither is an
    # authored per-step outcome, so the artifact rejects them.
    with pytest.raises(ValidationError, match="must be a business_outcome"):
        Outcome(
            code="X",
            outcome_class=OutcomeClass.RECOVERABLE,
            detector=Condition(text_present="x"),
        )


def test_role_qualified_text_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not be qualified by a role"):
        TargetDescriptor(role="cell", text="Current Balance")


def test_target_identity_form_conflicts_are_rejected() -> None:
    with pytest.raises(ValidationError, match="table_cell target must not mix"):
        TargetDescriptor(
            role="button", table_cell=TableCellTarget(row_contains="A", column_header="B")
        )
    with pytest.raises(ValidationError, match="label target must not mix"):
        TargetDescriptor(role="textbox", label="Member Number")
    with pytest.raises(ValidationError, match="a name target requires a role"):
        TargetDescriptor(name="Search")
    with pytest.raises(ValidationError, match="conflicting identity forms"):
        TargetDescriptor(role="button", name="Search", text="Search")


def test_step_with_outcomes_requires_a_postcondition() -> None:
    with pytest.raises(ValidationError, match="normal-path postcondition"):
        Step(
            id="bad",
            action=ClickAction(),
            target=TargetDescriptor(role="button", name="Search"),
            risk=RiskClass.READ_ONLY,
            postcondition=None,
            outcomes=[
                Outcome(
                    code="MEMBER_NOT_FOUND",
                    outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                    detector=Condition(text_present="Member record not found"),
                )
            ],
        )


def test_output_present_is_not_a_step_matcher() -> None:
    # output_present is verified against extracted outputs, not as a live step
    # condition, so it is not accepted inside a step postcondition.
    steps = _steps()
    steps[1] = Step(
        id="s2",
        action=ClickAction(),
        target=TargetDescriptor(role="button", name="Search"),
        risk=RiskClass.READ_ONLY,
        postcondition=Condition(output_present="savings_balance"),
    )
    with pytest.raises(ValidationError, match="not accepted here"):
        _capability(steps=steps)


def _write_step(step_id: str) -> Step:
    """A consequential-write step carrying a minimal read-only verification recipe."""
    return Step(
        id=step_id,
        action=ClickAction(),
        target=TargetDescriptor(role="button", name="Commit"),
        risk=RiskClass.CONSEQUENTIAL_WRITE,
        postcondition=Condition(text_present="done"),
        verification=MutationVerification(
            navigate=[
                Step(
                    id=f"{step_id}_v",
                    action=ClickAction(),
                    target=TargetDescriptor(role="link", name="Records"),
                    risk=RiskClass.READ_ONLY,
                )
            ],
            page=Condition(heading=Heading(role="heading", name="Records")),
            effect_present=Condition(text_present="Thing"),
        ),
    )


def _write_capability(steps: list[Step]) -> Capability:
    return Capability(
        id="x.write",
        version=1,
        target=CapabilityTarget(vendor="v", application_family="f"),
        inputs={},
        outputs={},
        steps=steps,
        success_checkpoint=Condition(text_present="done"),
    )


def test_multiple_verification_steps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at most one mutation-verification"):
        _write_capability([_write_step("w1"), _write_step("w2")])


def test_step_after_verification_is_rejected() -> None:
    trailing = Step(
        id="after",
        action=ClickAction(),
        target=TargetDescriptor(role="button", name="Next"),
        risk=RiskClass.READ_ONLY,
    )
    with pytest.raises(ValidationError, match="must be the final step"):
        _write_capability([_write_step("w1"), trailing])
