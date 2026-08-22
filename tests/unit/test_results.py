import pytest
from pydantic import TypeAdapter, ValidationError

from computer_use.model import (
    BusinessOutcome,
    Escalated,
    Failure,
    PolicyDecision,
    PolicyEffect,
    RunResult,
    Success,
)

run_result_adapter: TypeAdapter[object] = TypeAdapter(RunResult)


def test_success_round_trips_with_outputs() -> None:
    success = Success(
        run_id="run_1",
        capability="member.lookup_savings_balance",
        version=1,
        outputs={"savings_balance": "8421.31"},
    )
    reloaded = run_result_adapter.validate_json(success.model_dump_json())
    assert isinstance(reloaded, Success)
    assert reloaded.model_calls == 0
    assert reloaded.outputs["savings_balance"] == "8421.31"


def test_business_outcome_is_distinct_from_failure() -> None:
    outcome = BusinessOutcome(run_id="run_2", capability="c", code="MEMBER_NOT_FOUND")
    reloaded = run_result_adapter.validate_python(outcome.model_dump())
    assert isinstance(reloaded, BusinessOutcome)
    assert reloaded.code == "MEMBER_NOT_FOUND"


def test_escalated_variant() -> None:
    esc = Escalated(
        run_id="r", code="MUTATION_AMBIGUOUS", step_id="submit", intervention_id="int_1"
    )
    reloaded = run_result_adapter.validate_python(esc.model_dump())
    assert isinstance(reloaded, Escalated)
    assert reloaded.intervention_id == "int_1"


def test_failure_variant() -> None:
    fail = Failure(
        run_id="r",
        code="CHECKPOINT_FAILED",
        step_id="submit",
        expected="Member Profile",
        retryable=False,
    )
    reloaded = run_result_adapter.validate_python(fail.model_dump())
    assert isinstance(reloaded, Failure)
    assert reloaded.expected == "Member Profile"


def test_policy_decision_requires_reason() -> None:
    deny = PolicyDecision(
        effect=PolicyEffect.DENY, reason="domain not allowlisted", rule="domain_allowlist"
    )
    assert deny.effect is PolicyEffect.DENY
    with pytest.raises(ValidationError):
        PolicyDecision(effect=PolicyEffect.ALLOW)


def test_model_calls_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        Success(run_id="run_1", capability="c", version=1, model_calls=-1)
