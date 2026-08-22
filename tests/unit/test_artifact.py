import pytest
from pydantic import ValidationError

from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ExtractAction,
    Heading,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    Sensitivity,
    Step,
    TargetDescriptor,
    TypeAction,
)


def build_capability() -> Capability:
    return Capability(
        id="member.lookup_savings_balance",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_id": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        steps=[
            Step(
                id="open_member_search",
                action=ClickAction(),
                target=TargetDescriptor(role="link", name="Member Search"),
                postcondition=Condition(text_present="Member Lookup"),
            ),
            Step(
                id="enter_member_id",
                action=TypeAction(value=ParameterRef(name="member_id")),
                target=TargetDescriptor(role="textbox", label="Member Number"),
            ),
            Step(
                id="submit_lookup",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Search"),
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
                outcomes=[
                    Outcome(
                        code="MEMBER_NOT_FOUND",
                        outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                        detector=Condition(text_present="No member found"),
                    )
                ],
            ),
            Step(
                id="extract_balance",
                action=ExtractAction(),
                target=TargetDescriptor(role="cell", text="Current Balance"),
                output="savings_balance",
            ),
        ],
        success_checkpoint=Condition(output_present="savings_balance"),
    )


def test_capability_round_trips() -> None:
    capability = build_capability()
    reloaded = Capability.model_validate_json(capability.model_dump_json())
    assert reloaded == capability
    assert reloaded.schema_version == "1.0"


def test_unknown_field_is_rejected() -> None:
    data = build_capability().model_dump()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_unknown_schema_version_is_rejected() -> None:
    data = build_capability().model_dump()
    data["schema_version"] = "9.9"
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_target_descriptor_requires_identity() -> None:
    with pytest.raises(ValidationError):
        TargetDescriptor()
    with pytest.raises(ValidationError):
        TargetDescriptor(frame="workspace")  # frame alone is context, not identity


def test_click_requires_target() -> None:
    with pytest.raises(ValidationError):
        Step(id="s", action=ClickAction(), target=None)


def test_extract_requires_output() -> None:
    with pytest.raises(ValidationError):
        Step(id="s", action=ExtractAction(), target=TargetDescriptor(text="Balance"))


def test_extract_requires_target() -> None:
    with pytest.raises(ValidationError):
        Step(id="s", action=ExtractAction(), output="savings_balance")


def test_output_only_valid_for_extract() -> None:
    with pytest.raises(ValidationError):
        Step(
            id="s",
            action=ClickAction(),
            target=TargetDescriptor(role="button", name="Search"),
            output="savings_balance",
        )


def test_type_action_requires_value() -> None:
    with pytest.raises(ValidationError):
        TypeAction.model_validate({"type": "type"})


def test_capability_requires_steps() -> None:
    data = build_capability().model_dump()
    data["steps"] = []
    with pytest.raises(ValidationError):
        Capability.model_validate(data)
