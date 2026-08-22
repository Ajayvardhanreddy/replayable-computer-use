from computer_use.discovery import DiscoveryTrace, GoalSpec, TraceStep, compile_capability
from computer_use.execution import replay
from computer_use.model import (
    BusinessOutcome,
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
    Success,
    TableCellTarget,
    TargetDescriptor,
)

_SAFE_CLICKS = frozenset({"Search"})


def _capability() -> Capability:
    trace = DiscoveryTrace(
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
    spec = GoalSpec(
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
    return compile_capability(trace, spec)


async def test_replay_returns_second_member_balance(legacy_core_url: str) -> None:
    result = await replay(
        _capability(), {"member_number": "54321"}, legacy_core_url, safe_clicks=_SAFE_CLICKS
    )
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "312.45"
    assert result.model_calls == 0


async def test_replay_first_member_balance(legacy_core_url: str) -> None:
    result = await replay(
        _capability(), {"member_number": "12345"}, legacy_core_url, safe_clicks=_SAFE_CLICKS
    )
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "8421.31"
    assert result.model_calls == 0


async def test_replay_unknown_member_is_business_outcome(legacy_core_url: str) -> None:
    # "No such member" is a legitimate domain answer, not a hard failure.
    result = await replay(
        _capability(), {"member_number": "99999"}, legacy_core_url, safe_clicks=_SAFE_CLICKS
    )
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.model_calls == 0
