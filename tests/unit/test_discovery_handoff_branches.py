"""Discovery-side handoff control branches, without a browser or a model.

A stub model proposes request_human immediately; a minimal surface stands in for the
page. These pin the two non-happy branches: with no handoff wired the run stops with a
typed reason, and a handler that declines also stops — the happy resume path is proven
on a real browser in the integration suite.
"""

from computer_use.discovery import GoalSpec, discover
from computer_use.execution import ControlLease, TrustedKernel, ValueResolver
from computer_use.handoff import OperatorController
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    OutputSpec,
    ParamType,
    ProposedAction,
    ProposedActionType,
    Sensitivity,
)
from computer_use.safety import NavigationPolicy, Policy, RiskClassifier
from computer_use.surface import Observation, StructuralSnapshot

_NAV = NavigationPolicy(
    allowed_origins=frozenset({"http://legacy"}),
    allowed_routes=frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"}),
)


class _MiniSurface:
    """Only what the discovery loop touches before a request_human proposal."""

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def wait_settled(self) -> None: ...
    async def current_url(self) -> str:
        return "http://legacy/"

    async def current_route(self) -> str:
        return "/"

    async def observe(self) -> Observation:
        return Observation(route="/", candidates=[])

    async def primary_heading(self) -> str | None:
        return None

    async def capture(self) -> StructuralSnapshot:
        return StructuralSnapshot(route="/", frames=["main"], landmarks=[])


class _HumanRequestModel:
    provider = "stub"
    model_id = "stub-1"

    async def decide(self, goal: object, observation: object) -> ProposedAction:
        return ProposedAction(action=ProposedActionType.REQUEST_HUMAN, reason="cannot proceed")


def _spec() -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal="g",
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL)
        },
        success_output="savings_balance",
        business_outcomes=[],
    )


def _kernel(surface: object, lease: ControlLease | None = None) -> TrustedKernel:
    return TrustedKernel(
        surface,  # type: ignore[arg-type]
        Policy(allowed_actions=frozenset({ProposedActionType.CLICK})),
        RiskClassifier(),
        ValueResolver({}),
        lease=lease,
    )


async def test_request_human_without_handoff_stops_with_typed_reason() -> None:
    surface = _MiniSurface()
    outcome = await discover(
        _HumanRequestModel(), surface, _kernel(surface), _spec(), "http://legacy",
        nav_policy=_NAV,
    )
    assert outcome.stop_reason == "HUMAN_REQUESTED"
    assert outcome.model_calls >= 1
    assert outcome.trace.steps == []


async def test_request_human_with_declining_handler_stops() -> None:
    surface = _MiniSurface()
    lease = ControlLease()
    seen: dict[str, bool] = {"called": False}

    async def decline(operator: OperatorController, reason: str | None = None) -> bool:
        seen["called"] = True
        return False

    outcome = await discover(
        _HumanRequestModel(), surface, _kernel(surface, lease), _spec(), "http://legacy",
        nav_policy=_NAV, lease=lease, on_human_request=decline,
    )
    assert seen["called"] is True
    assert outcome.stop_reason == "HUMAN_REQUESTED"
