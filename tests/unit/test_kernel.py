import pytest
from pydantic import ValidationError

from computer_use.execution import (
    KernelRejection,
    RejectionCode,
    TrustedKernel,
    ValueResolver,
)
from computer_use.model import (
    ParameterRef,
    ProposedAction,
    ProposedActionType,
    RiskClass,
    TargetDescriptor,
)
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import Candidate, Observation, StructuralSnapshot

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)

SEARCH = Candidate(id="c1", role="button", name="Search", frame="lc-workspace")
MEMBER = Candidate(id="c2", role="textbox", name="Member Number", frame="lc-workspace")
TRANSFER = Candidate(id="c3", role="button", name="Transfer", frame="lc-workspace")
BALANCE = Candidate(
    id="c4", role="cell", row="Share Savings", column="Current Balance",
    text="$8,421.31", frame="lc-workspace",
)


class FakeSurface:
    """A minimal in-memory Surface stand-in for offline kernel tests."""

    def __init__(self, count: int = 1) -> None:
        self._count = count
        self.clicks: list[TargetDescriptor] = []
        self.typed: list[tuple[TargetDescriptor, str]] = []
        self.extract_value = "$8,421.31"

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def observe(self) -> Observation:
        raise NotImplementedError

    async def count(self, target: TargetDescriptor) -> int:
        return self._count

    async def click(self, target: TargetDescriptor) -> None:
        self.clicks.append(target)

    async def type_text(self, target: TargetDescriptor, text: str) -> None:
        self.typed.append((target, text))

    async def extract(self, target: TargetDescriptor) -> str:
        return self.extract_value

    async def capture(self) -> StructuralSnapshot:
        raise NotImplementedError

    async def wait_for_frame_url(self, fragment: str, timeout_ms: int = 5000) -> None: ...
    async def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool:
        return True

    async def has_text(self, text: str) -> bool:
        return False

    async def wait_settled(self) -> None: ...
    async def primary_heading(self) -> str | None:
        return None

    async def wait_for_heading_change(
        self, previous: str | None, timeout_ms: int = 5000
    ) -> str | None:
        return None

    async def close(self) -> None: ...


def _kernel(surface: FakeSurface, inputs: dict[str, str] | None = None) -> TrustedKernel:
    return TrustedKernel(
        surface,
        Policy(allowed_actions=_ALLOWED),
        RiskClassifier(safe_click_names=frozenset({"Search"})),
        ValueResolver(inputs or {}),
    )


def test_model_cannot_supply_a_risk_classification() -> None:
    with pytest.raises(ValidationError):
        ProposedAction.model_validate(
            {"action": "click", "candidate_id": "c1", "risk": "read_only"}
        )


def test_raw_scalar_action_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProposedAction.model_validate({"action": "type", "candidate_id": "c2", "value": "12345"})


async def test_read_only_click_executes() -> None:
    surface = FakeSurface(count=1)
    result = await _kernel(surface).execute(
        ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1"), {"c1": SEARCH}
    )
    assert result.risk is RiskClass.READ_ONLY
    assert surface.clicks and surface.clicks[0].name == "Search"


async def test_type_substitutes_parameter_value_inside_the_kernel() -> None:
    surface = FakeSurface(count=1)
    kernel = _kernel(surface, inputs={"member_number": "12345"})
    await kernel.execute(
        ProposedAction(
            action=ProposedActionType.TYPE,
            candidate_id="c2",
            value=ParameterRef(name="member_number"),
        ),
        {"c2": MEMBER},
    )
    assert surface.typed[0][1] == "12345"


async def test_extract_returns_cell_value() -> None:
    surface = FakeSurface(count=1)
    result = await _kernel(surface).execute(
        ProposedAction(
            action=ProposedActionType.EXTRACT, candidate_id="c4", output="savings_balance"
        ),
        {"c4": BALANCE},
    )
    assert result.extracted == "$8,421.31"


async def test_consequential_click_blocked_by_software_risk() -> None:
    surface = FakeSurface(count=1)
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface).execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="c3"), {"c3": TRANSFER}
        )
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert surface.clicks == []  # never executed


async def test_unknown_click_fails_closed_not_read_only() -> None:
    # "History" is benign-sounding but is NOT on the safe allowlist, so it must
    # fail closed rather than silently classifying as READ_ONLY.
    surface = FakeSurface(count=1)
    history = Candidate(id="c5", role="button", name="History", frame="lc-workspace")
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface).execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="c5"), {"c5": history}
        )
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert surface.clicks == []


async def test_unknown_candidate_is_rejected() -> None:
    with pytest.raises(KernelRejection) as exc:
        await _kernel(FakeSurface()).execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="zzz"), {}
        )
    assert exc.value.code is RejectionCode.UNKNOWN_CANDIDATE


async def test_disallowed_action_is_denied() -> None:
    surface = FakeSurface(count=1)
    kernel = TrustedKernel(
        surface,
        Policy(allowed_actions=frozenset({ProposedActionType.EXTRACT})),
        RiskClassifier(),
        ValueResolver({}),
    )
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1"), {"c1": SEARCH}
        )
    assert exc.value.code is RejectionCode.POLICY_DENIED


async def test_ambiguous_target_fails_closed() -> None:
    surface = FakeSurface(count=2)
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface).execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1"), {"c1": SEARCH}
        )
    assert exc.value.code is RejectionCode.LOCATOR_AMBIGUOUS
    assert surface.clicks == []


async def test_missing_target_fails_closed() -> None:
    surface = FakeSurface(count=0)
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface).execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1"), {"c1": SEARCH}
        )
    assert exc.value.code is RejectionCode.TARGET_MISSING


async def test_type_without_value_is_rejected() -> None:
    with pytest.raises(KernelRejection) as exc:
        await _kernel(FakeSurface()).execute(
            ProposedAction(action=ProposedActionType.TYPE, candidate_id="c2"), {"c2": MEMBER}
        )
    assert exc.value.code is RejectionCode.MISSING_VALUE
