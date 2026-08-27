"""Mutation-safety invariants, exercised deterministically with a fake surface.

These pin the safety-critical decisions of the consequential-write state machine on the
surviving representation — the discovered, embedded ``MutationVerification`` recipe — without
a browser, so the invariants have fast regression coverage independent of the integration
suite. Each test isolates one rule:

* a consequential write dispatches at most once, even when its completion is uncertain;
* an effect confirmed present by the independent read is a committed Success;
* an authoritative absent effect is a definite MUTATION_NOT_COMMITTED;
* an effect that cannot be established is MUTATION_AMBIGUOUS (never a guess);
* a write with no standing approval never dispatches;
* every verification action is re-classified READ_ONLY at runtime and fails closed otherwise;
* a present effect is attributed to this write only as an absent-before -> present transition.

Human same-session re-verification (take over, resolve, hand back, resume) is an integration
concern proven on the real ``ReplaySession`` + browser in ``tests/integration/test_mutation.py``.
"""

from computer_use.execution import replay
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    Escalated,
    Failure,
    FailureCode,
    Heading,
    MutationVerification,
    RiskClass,
    Step,
    Success,
    TargetDescriptor,
)
from computer_use.safety import AuthorityPolicy, ConfirmationPolicy, NavigationPolicy
from computer_use.surface import SurfaceDriverError

_NAV = NavigationPolicy(
    allowed_origins=frozenset({"http://legacy"}),
    allowed_routes=frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"}),
)
# "Search" and "Member Inquiry" are the known-safe read-only navigations the verification
# reuses; "Create Account" is deliberately never safe, so the classifier flags it as the write.
_SAFE = frozenset({"Search", "Member Inquiry"})
_APPROVE = ConfirmationPolicy(approved=frozenset({"m.open:v1:s2_create"}))
_AUTHORITATIVE = AuthorityPolicy(authoritative_absence=True)
_EFFECT = "Share Savings Sub"


class _MutFake:
    """A single-write surface whose effect view flips from its pre-dispatch baseline to its
    post-dispatch state, so the verification decision can be driven deterministically.

    ``dispatch_raises`` models a withheld commit response (an uncertain dispatch);
    ``baseline_present`` is whether the effect exists before the write (attribution);
    ``effect_after`` is whether the independent read finds it afterwards; ``dialog`` blocks the
    verification read. The write ("Create Account") is counted so a test can prove it is never
    re-dispatched.
    """

    def __init__(
        self,
        *,
        dispatch_raises: bool = False,
        baseline_present: bool = False,
        effect_after: bool = True,
        dialog: bool = False,
    ) -> None:
        self._dispatch_raises = dispatch_raises
        self._baseline = baseline_present
        self._after = effect_after
        self._dialog = dialog
        self._dispatched = False
        self.write_clicks = 0

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def wait_settled(self) -> None: ...

    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def click(self, target: TargetDescriptor, *, timeout_ms: int | None = None) -> None:
        if target.name == "Create Account":
            self.write_clicks += 1
            self._dispatched = True
            if self._dispatch_raises:
                raise SurfaceDriverError("commit response withheld")

    async def type_text(self, target: TargetDescriptor, text: str, *, submit: bool = False) -> None:
        ...

    async def extract(self, target: TargetDescriptor) -> str:
        return "Active"

    async def has_text(self, text: str) -> bool:
        if _EFFECT in text:
            return self._after if self._dispatched else self._baseline
        return False

    async def has_heading(self, name: str) -> bool:
        return name == "Member Profile"

    async def has_blocking_dialog(self) -> bool:
        return self._dialog and self._dispatched

    async def scope_urls(self) -> list[str]:
        return ["http://legacy/workspace/member/00000"]

    async def current_route(self) -> str:
        return "/workspace/member/00000"

    async def current_url(self) -> str:
        return "http://legacy/workspace/member/00000"

    async def primary_heading(self) -> str | None:
        return "Member Profile"

    async def close(self) -> None: ...


def _verification(*, navigate: list[Step] | None = None) -> MutationVerification:
    steps = navigate if navigate is not None else [
        Step(
            id="v1_inquiry",
            action=ClickAction(),
            target=TargetDescriptor(role="link", name="Member Inquiry"),
            risk=RiskClass.READ_ONLY,
        )
    ]
    return MutationVerification(
        navigate=steps,
        page=Condition(heading=Heading(role="heading", name="Member Profile")),
        effect_present=Condition(text_present=_EFFECT),
    )


def _capability(*, verification: MutationVerification | None = None) -> Capability:
    # Two steps: a read-only navigation that lands on the effect view (so the pre-dispatch
    # baseline is captured there), then the consequential commit carrying its verification.
    return Capability(
        id="m.open",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={},
        outputs={},
        steps=[
            Step(
                id="s1_search",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Search"),
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
            ),
            Step(
                id="s2_create",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Create Account"),
                risk=RiskClass.CONSEQUENTIAL_WRITE,
                postcondition=Condition(text_present="Sub-account created"),
                verification=verification if verification is not None else _verification(),
            ),
        ],
        success_checkpoint=Condition(text_present=_EFFECT),
    )


async def _run(
    fake: _MutFake,
    *,
    approve: bool = True,
    authority: AuthorityPolicy | None = None,
    capability: Capability | None = None,
) -> object:
    return await replay(
        capability if capability is not None else _capability(),
        {},
        "http://legacy",
        nav_policy=_NAV,
        safe_clicks=_SAFE,
        surface=fake,
        confirmation=_APPROVE if approve else None,
        authority=authority,
        resolve_timeout_ms=200,
    )


async def test_verified_effect_present_is_committed_success() -> None:
    fake = _MutFake(baseline_present=False, effect_after=True)
    result = await _run(fake)
    assert isinstance(result, Success)
    assert fake.write_clicks == 1


async def test_uncertain_dispatch_is_verified_and_dispatched_once() -> None:
    # The commit response is lost; the write must NOT be retried, and the independent read
    # confirms the effect landed -> Success from a single dispatch.
    fake = _MutFake(dispatch_raises=True, baseline_present=False, effect_after=True)
    result = await _run(fake)
    assert isinstance(result, Success)
    assert fake.write_clicks == 1  # never re-dispatched after an uncertain completion


async def test_authoritative_absence_is_not_committed() -> None:
    fake = _MutFake(baseline_present=False, effect_after=False)
    result = await _run(fake, authority=_AUTHORITATIVE)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.MUTATION_NOT_COMMITTED
    assert fake.write_clicks == 1


async def test_unestablished_effect_is_ambiguous_escalation() -> None:
    # Absent effect, but the read source is not authoritative for absence: the runtime must
    # not conclude non-commit -> it escalates rather than guessing.
    fake = _MutFake(baseline_present=False, effect_after=False)
    result = await _run(fake)  # default authority: absence is not authoritative
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"
    assert fake.write_clicks == 1


async def test_missing_approval_prevents_dispatch() -> None:
    fake = _MutFake()
    result = await _run(fake, approve=False)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.POLICY_DENIED
    assert fake.write_clicks == 0  # a write with no standing approval never dispatches


async def test_verification_action_must_be_read_only() -> None:
    # The artifact declares the verification step READ_ONLY, but its control is the consequential
    # "Create Account": the runtime re-derives risk and refuses to execute it, so a smuggled
    # second write is never dispatched.
    smuggled = _verification(
        navigate=[
            Step(
                id="v_bad",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Create Account"),
                risk=RiskClass.READ_ONLY,
            )
        ]
    )
    fake = _MutFake(baseline_present=False, effect_after=True)
    result = await _run(fake, capability=_capability(verification=smuggled))
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"
    assert fake.write_clicks == 1  # only the real commit; the verification write is refused


async def test_present_effect_without_absent_baseline_is_not_attributed() -> None:
    # The effect already existed before the write, so a present effect afterwards cannot be
    # attributed to this execution -> ambiguous, not a false Success.
    fake = _MutFake(baseline_present=True, effect_after=True)
    result = await _run(fake)
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"
    assert fake.write_clicks == 1
