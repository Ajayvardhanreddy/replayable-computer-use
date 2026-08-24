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


def _kernel(surface: _LoopSurface, inputs: dict[str, str] | None = None) -> TrustedKernel:
    return TrustedKernel(
        surface,
        Policy(allowed_actions=_ALLOWED),
        RiskClassifier(safe_click_names=frozenset({"Search"})),
        ValueResolver(inputs or {}),
    )


class _AlwaysDeclareSuccessModel:
    provider = "fake"
    model_id = "premature"

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)


class _BogusOutputModel:
    provider = "fake"
    model_id = "bogus-output"

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        return ProposedAction(
            action=ProposedActionType.EXTRACT, candidate_id="c1", output="not_a_real_output"
        )


class _CellSurface(_LoopSurface):
    async def observe(self) -> Observation:
        return Observation(
            route="/profile",
            candidates=[
                Candidate(
                    id="cell1", role="cell", row="Share Savings",
                    column="Current Balance", frame="lc-workspace",
                )
            ],
        )

    async def extract(self, target: TargetDescriptor) -> str:
        return "$8,421.31"


class _ExtractThenSucceedModel:
    provider = "fake"
    model_id = "ok"

    def __init__(self) -> None:
        self._extracted = False

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        if not self._extracted:
            self._extracted = True
            return ProposedAction(
                action=ProposedActionType.EXTRACT, candidate_id="cell1", output="savings_balance"
            )
        return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)


async def test_repeated_action_trips_stuck_before_max_steps() -> None:
    surface = _LoopSurface()
    outcome = await discover(
        _RepeatingModel(), surface, _kernel(surface, {"member_number": "12345"}),
        _spec(), "http://localhost", max_steps=12,
    )
    assert outcome.stop_reason == "STUCK"
    assert outcome.model_calls == 3  # bounded, not the full 12-step budget


async def test_premature_declare_success_is_not_ratified() -> None:
    surface = _LoopSurface()
    outcome = await discover(
        _AlwaysDeclareSuccessModel(), surface, _kernel(surface), _spec(), "http://localhost"
    )
    assert outcome.stop_reason != "GOAL_REACHED"
    assert outcome.stop_reason == "STUCK"
    assert outcome.trace.steps == []


async def test_extract_of_undeclared_output_is_rejected() -> None:
    surface = _LoopSurface()
    outcome = await discover(
        _BogusOutputModel(), surface, _kernel(surface), _spec(), "http://localhost"
    )
    assert outcome.stop_reason != "GOAL_REACHED"
    assert outcome.trace.steps == []  # nothing was executed


async def test_success_after_required_extraction_is_ratified() -> None:
    surface = _CellSurface()
    outcome = await discover(
        _ExtractThenSucceedModel(), surface, _kernel(surface), _spec(), "http://localhost"
    )
    assert outcome.stop_reason == "GOAL_REACHED"
    assert len(outcome.trace.steps) == 1  # the extract


class _ReExtractThenSucceedModel:
    """Extracts the same output twice (redundantly) before declaring success."""

    provider = "fake"
    model_id = "reextract"

    def __init__(self) -> None:
        self._calls = 0

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        self._calls += 1
        if self._calls <= 2:
            return ProposedAction(
                action=ProposedActionType.EXTRACT, candidate_id="cell1", output="savings_balance"
            )
        return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)


class _ObservationRecordingModel:
    provider = "fake"
    model_id = "recorder"

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self._calls = 0

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        self.seen.append(list(observation.obtained_outputs))
        self._calls += 1
        if self._calls == 1:
            return ProposedAction(
                action=ProposedActionType.EXTRACT, candidate_id="cell1", output="savings_balance"
            )
        return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)


async def test_redundant_extract_of_obtained_output_is_not_recorded() -> None:
    surface = _CellSurface()
    outcome = await discover(
        _ReExtractThenSucceedModel(), surface, _kernel(surface), _spec(), "http://localhost"
    )
    assert outcome.stop_reason == "GOAL_REACHED"
    # the second (redundant) extract is nudged away, not executed or recorded
    assert len(outcome.trace.steps) == 1


async def test_obtained_outputs_is_surfaced_to_the_model() -> None:
    model = _ObservationRecordingModel()
    surface = _CellSurface()
    outcome = await discover(model, surface, _kernel(surface), _spec(), "http://localhost")
    assert outcome.stop_reason == "GOAL_REACHED"
    assert model.seen[0] == []  # nothing obtained on the first turn
    assert model.seen[1] == ["savings_balance"]  # after the extract, the model sees it
