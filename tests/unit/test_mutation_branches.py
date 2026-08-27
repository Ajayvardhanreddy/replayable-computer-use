"""Consequential-mutation execution, exercised deterministically with a fake surface.

These pin the safety-critical branches without a browser: an uncertain dispatch is never
retried (dispatched exactly once), read-back distinguishes committed / not-committed /
ambiguous, a commit without confirmation never dispatches, and an ambiguous mutation
routes to the same human-handoff seam and resolves after the human makes state readable.
"""

from computer_use.execution import ReplaySession, replay
from computer_use.handoff import OperatorController
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ControlOwner,
    EffectState,
    Escalated,
    Failure,
    FailureCode,
    Heading,
    InputSpec,
    Outcome,
    OutcomeClass,
    ParamType,
    ReadBack,
    RiskClass,
    Sensitivity,
    Step,
    Success,
    TargetDescriptor,
)
from computer_use.safety import ConfirmationPolicy, NavigationPolicy
from computer_use.surface import SurfaceDriverError

_NAV = NavigationPolicy(
    allowed_origins=frozenset({"http://legacy"}),
    allowed_routes=frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"}),
)
_APPROVE = ConfirmationPolicy(approved=frozenset({"m.open:v1:commit"}))


class _CommitFake:
    """One-step commit surface: the commit click optionally fails (uncertain dispatch),
    and the read-back page reports a configurable effect presence / loadedness."""

    def __init__(self, *, click_raises: bool, read_present: bool, read_loaded: bool = True) -> None:
        self.click_count = 0
        self._click_raises = click_raises
        self._read_present = read_present
        self._read_loaded = read_loaded
        self._on_readback = False
        self.closed = False

    async def start(self) -> None: ...

    async def goto(self, url: str) -> None:
        # The read-back navigates to the member route; the entry goto does not.
        self._on_readback = "/workspace/member/" in url

    async def wait_settled(self) -> None: ...

    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def click(self, target: TargetDescriptor, *, timeout_ms: int | None = None) -> None:
        self.click_count += 1
        if self._click_raises:
            raise SurfaceDriverError("dispatch completion withheld")

    async def type_text(
        self, target: TargetDescriptor, text: str, *, submit: bool = False
    ) -> None: ...

    async def has_text(self, text: str) -> bool:
        return self._on_readback and self._read_present and text == "Share Savings Sub"

    async def has_heading(self, name: str) -> bool:
        return self._on_readback and self._read_loaded and name == "Member Profile"

    async def has_blocking_dialog(self) -> bool:
        return False

    async def current_route(self) -> str:
        return "/workspace/member/00000"

    async def current_url(self) -> str:
        return "http://legacy/workspace/member/00000"

    async def primary_heading(self) -> str | None:
        return None

    async def close(self) -> None:
        self.closed = True


def _capability() -> Capability:
    return Capability(
        id="m.open",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={},
        steps=[
            Step(
                id="commit",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Create Account"),
                risk=RiskClass.CONSEQUENTIAL_WRITE,
                postcondition=Condition(text_present="Sub-account created"),
                outcomes=[
                    Outcome(
                        code="ACCOUNT_ALREADY_EXISTS",
                        outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                        detector=Condition(text_present="already exists"),
                    )
                ],
                read_back=ReadBack(
                    read_route="/workspace/member/:member_number",
                    page_loaded=Condition(heading=Heading(role="heading", name="Member Profile")),
                    effect_present=Condition(text_present="Share Savings Sub"),
                ),
            )
        ],
        success_checkpoint=Condition(text_present="Sub-account created"),
    )


async def _run(fake: _CommitFake, *, approve: bool = True) -> object:
    return await replay(
        _capability(), {"member_number": "12345"}, "http://legacy",
        nav_policy=_NAV, surface=fake,
        confirmation=_APPROVE if approve else None, resolve_timeout_ms=200,
    )


# D17 — an uncertain dispatch is verified committed and dispatched exactly once.
async def test_uncertain_dispatch_committed_and_dispatched_once() -> None:
    fake = _CommitFake(click_raises=True, read_present=True)
    result = await _run(fake)
    assert isinstance(result, Success)
    assert fake.click_count == 1  # never re-dispatched


# D18 — read-back distinguishes not-committed from ambiguous.
async def test_read_back_not_committed() -> None:
    fake = _CommitFake(click_raises=True, read_present=False, read_loaded=True)
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.MUTATION_NOT_COMMITTED
    assert fake.click_count == 1


async def test_read_back_ambiguous_when_unverifiable() -> None:
    fake = _CommitFake(click_raises=True, read_present=False, read_loaded=False)
    result = await _run(fake)
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"


# D14 — no confirmation, no dispatch.
async def test_commit_blocked_without_confirmation() -> None:
    fake = _CommitFake(click_raises=False, read_present=True)
    result = await _run(fake, approve=False)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.POLICY_DENIED
    assert fake.click_count == 0  # definitely not dispatched


# Ambiguous mutation reuses the human-handoff seam; resume re-verifies (no re-dispatch).
async def test_ambiguous_mutation_resolved_by_human_reverification() -> None:
    fake = _CommitFake(click_raises=True, read_present=False, read_loaded=False)
    session = ReplaySession(
        _capability(), {"member_number": "12345"}, "http://legacy",
        nav_policy=_NAV, surface=fake, confirmation=_APPROVE, resolve_timeout_ms=200,
    )
    assert await session.start() is None
    paused = await session.advance()
    assert isinstance(paused, Escalated)
    assert paused.code == "MUTATION_AMBIGUOUS"
    assert session.last_effect_state is EffectState.AMBIGUOUS
    dispatched = fake.click_count

    operator = OperatorController(session)
    operator.take_control()
    # The human makes the effect readable on the same session; resume re-verifies.
    fake._read_present = True
    fake._read_loaded = True
    result = await operator.resume()
    assert isinstance(result, Success)
    assert session.lease.owner is ControlOwner.AUTOMATION
    assert fake.click_count == dispatched  # verification never re-dispatches the write
