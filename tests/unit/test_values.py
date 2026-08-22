import pytest
from pydantic import TypeAdapter, ValidationError

from computer_use.model import (
    Condition,
    DerivedValue,
    Heading,
    OutcomeClass,
    ParameterRef,
    RiskClass,
    SafeLiteral,
    SecretRef,
    ValueRef,
)

value_ref_adapter: TypeAdapter[object] = TypeAdapter(ValueRef)


def test_value_ref_discriminates_by_source() -> None:
    param = value_ref_adapter.validate_python({"source": "parameter", "name": "member_id"})
    literal = value_ref_adapter.validate_python({"source": "safe_literal", "value": "Savings"})
    secret = value_ref_adapter.validate_python({"source": "secret", "name": "legacy_pw"})
    assert isinstance(param, ParameterRef)
    assert isinstance(literal, SafeLiteral)
    assert isinstance(secret, SecretRef)


def test_derived_value_nests_value_refs() -> None:
    derived = DerivedValue(function="format_date", args=[ParameterRef(name="dob")])
    reloaded = value_ref_adapter.validate_json(value_ref_adapter.dump_json(derived))
    assert derived == reloaded


def test_bare_scalar_is_not_a_value_ref() -> None:
    with pytest.raises(ValidationError):
        value_ref_adapter.validate_python("12345")


def test_condition_requires_a_matcher() -> None:
    with pytest.raises(ValidationError):
        Condition()
    assert Condition(text_present="Member Profile").text_present == "Member Profile"


def test_condition_nests_any_of() -> None:
    cond = Condition(
        any_of=[
            Condition(text_present="Member Profile"),
            Condition(heading=Heading(role="heading", name="Member Profile")),
        ]
    )
    reloaded = Condition.model_validate_json(cond.model_dump_json())
    assert reloaded == cond


def test_outcome_class_has_no_escalation_member() -> None:
    assert {member.value for member in OutcomeClass} == {
        "business_outcome",
        "recoverable",
        "hard_failure",
    }


def test_risk_class_members() -> None:
    assert {member.value for member in RiskClass} == {
        "read_only",
        "reversible_write",
        "consequential_write",
        "irreversible",
    }
