from computer_use.discovery import GoalContext, GoalSpec, ModelObservation, discover
from computer_use.execution import TrustedKernel, ValueResolver
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedAction,
    ProposedActionType,
    Sensitivity,
    TargetDescriptor,
)
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import Candidate, Observation, StructuralSnapshot

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)


class _LoopSurface:
    """A stand-in surface whose observation always offers the Member Number field."""

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def observe(self) -> Observation:
        return Observation(
            route="/workspace/inquiry",
            candidates=[
                Candidate(id="c1", role="textbox", name="Member Number", frame="lc-workspace")
            ],
        )

    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def click(self, target: TargetDescriptor) -> None: ...
    async def type_text(self, target: TargetDescriptor, text: str) -> None: ...
    async def extract(self, target: TargetDescriptor) -> str:
        return ""

    async def capture(self) -> StructuralSnapshot:
        raise NotImplementedError

    async def wait_for_frame_url(self, fragment: str, timeout_ms: int = 5000) -> None: ...
    async def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool:
        return True

    async def has_text(self, text: str) -> bool:
        return False

    async def wait_settled(self) -> None: ...
    async def primary_heading(self) -> str | None:
        return "Member Inquiry"

    async def wait_for_heading_change(
        self, previous: str | None, timeout_ms: int = 5000
    ) -> str | None:
        return "Member Profile"

    async def close(self) -> None: ...


class _RepeatingModel:
    provider = "fake"
    model_id = "loop"

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        return ProposedAction(
            action=ProposedActionType.TYPE,
            candidate_id="c1",
            value=ParameterRef(name="member_number"),
        )


def _spec() -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal="Look up this member and return their savings balance",
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={"savings_balance": OutputSpec(type=ParamType.DECIMAL)},
        success_output="savings_balance",
    )


async def test_repeated_action_trips_stuck_before_max_steps() -> None:
    surface = _LoopSurface()
    kernel = TrustedKernel(
        surface,
        Policy(allowed_actions=_ALLOWED),
        RiskClassifier(safe_click_names=frozenset({"Search"})),
        ValueResolver({"member_number": "12345"}),
    )
    outcome = await discover(
        _RepeatingModel(), surface, kernel, _spec(), "http://localhost", max_steps=12
    )
    assert outcome.stop_reason == "STUCK"
    assert outcome.model_calls == 3  # bounded, not the full 12-step budget
