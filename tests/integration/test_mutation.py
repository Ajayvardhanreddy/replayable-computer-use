"""Consequential-mutation correctness against LegacyCore (Capability B).

A small authored capability opens a sub-account. These prove the commit semantics that
matter in production: confirmation at the commit boundary, an explicit rejection as a
business outcome, an uncertain completion resolved by read-back rather than a retry, and
a dispatched write verified committed or not committed through an independent read.
Capability B is a mutation-semantics fixture; genuine model discovery is proven by
Capability A.
"""

from urllib.parse import urlsplit

import pytest

from computer_use.execution import ControlLease, ReplaySession, replay
from computer_use.handoff import ClickControl, OperatorController
from computer_use.model import (
    BusinessOutcome,
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ControlOwner,
    Escalated,
    ExtractAction,
    Failure,
    FailureCode,
    Heading,
    InputSpec,
    MutationVerification,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    RiskClass,
    Sensitivity,
    Step,
    Success,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)
from computer_use.safety import AuthorityPolicy, ConfirmationPolicy, NavigationPolicy
from computer_use.surface import PlaywrightSurface
from legacy_core import mutations

# "Member Inquiry" is the persistent shell nav the verification reuses to re-derive the
# member's accounts independently; "Create Account" is deliberately never safe.
_SAFE_B = frozenset({"Search", "Open Sub-Account", "Member Inquiry"})
_APPROVE = ConfirmationPolicy(approved=frozenset({"member.open_sub_account:v1:s4_create"}))
# LegacyCore's member profile is an immediately-consistent, authoritative read source.
_AUTHORITATIVE = AuthorityPolicy(authoritative_absence=True)


@pytest.fixture
def nav_policy(legacy_core_url: str) -> NavigationPolicy:
    """Capability B navigation scope: the read routes plus the sub-account write route.

    Overrides the shared conftest fixture so the write route the workspace iframe visits
    is in scope for these tests, while Capability A's integration scope stays tight. This
    mirrors the workstation's Capability B scope; frame-aware enforcement judges the iframe
    URL, not just the top page, so the write route must be allowlisted here.
    """
    parts = urlsplit(legacy_core_url)
    return NavigationPolicy(
        allowed_origins=frozenset({f"{parts.scheme}://{parts.netloc}"}),
        allowed_routes=frozenset(
            {
                "/",
                "/workspace/inquiry",
                "/workspace/member/:member_number",
                "/workspace/member/:member_number/sub-account",
            }
        ),
    )


@pytest.fixture(autouse=True)
def _reset_mutations() -> None:
    mutations.reset()


def _capability_b() -> Capability:
    return Capability(
        id="member.open_sub_account",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={"sub_account_status": OutputSpec(type=ParamType.STRING)},
        steps=[
            Step(
                id="s1_type",
                action=TypeAction(value=ParameterRef(name="member_number")),
                target=TargetDescriptor(role="textbox", name="Member Number", frame="lc-workspace"),
                risk=RiskClass.READ_ONLY,
            ),
            Step(
                id="s2_search",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Search", frame="lc-workspace"),
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
            ),
            Step(
                id="s3_open",
                action=ClickAction(),
                target=TargetDescriptor(role="link", name="Open Sub-Account", frame="lc-workspace"),
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Open Sub-Account")),
            ),
            Step(
                id="s4_create",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Create Account", frame="lc-workspace"),
                risk=RiskClass.CONSEQUENTIAL_WRITE,
                postcondition=Condition(text_present="Sub-account created"),
                outcomes=[
                    Outcome(
                        code="ACCOUNT_ALREADY_EXISTS",
                        outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                        detector=Condition(
                            text_present="A sub-account of this type already exists"
                        ),
                    )
                ],
                # The discovered independent verification: re-derive the member's accounts
                # through the persistent shell (re-query by the SAME parameter), then judge
                # the effect on the profile. Flat and read-only.
                verification=MutationVerification(
                    navigate=[
                        Step(
                            id="v1_inquiry",
                            action=ClickAction(),
                            target=TargetDescriptor(role="link", name="Member Inquiry"),
                            risk=RiskClass.READ_ONLY,
                        ),
                        Step(
                            id="v2_type",
                            action=TypeAction(value=ParameterRef(name="member_number")),
                            target=TargetDescriptor(
                                role="textbox", name="Member Number", frame="lc-workspace"
                            ),
                            risk=RiskClass.READ_ONLY,
                        ),
                        Step(
                            id="v3_search",
                            action=ClickAction(),
                            target=TargetDescriptor(
                                role="button", name="Search", frame="lc-workspace"
                            ),
                            risk=RiskClass.READ_ONLY,
                        ),
                    ],
                    page=Condition(heading=Heading(role="heading", name="Member Profile")),
                    effect_present=Condition(text_present="Share Savings Sub"),
                    extract=Step(
                        id="v4_extract",
                        action=ExtractAction(),
                        target=TargetDescriptor(
                            table_cell=TableCellTarget(
                                row_contains="Share Savings Sub", column_header="Status"
                            ),
                            frame="lc-workspace",
                        ),
                        risk=RiskClass.READ_ONLY,
                        output="sub_account_status",
                    ),
                ),
            ),
        ],
        success_checkpoint=Condition(output_present="sub_account_status"),
    )


async def _run(
    url: str, nav_policy: NavigationPolicy, *, scenario: str | None = None, approve: bool = True,
    commit_timeout_ms: int | None = None, member: str = "12345",
) -> object:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        if scenario is not None:
            await surface.goto(f"{url}/?scenario={scenario}")
        return await replay(
            _capability_b(), {"member_number": member}, url,
            nav_policy=nav_policy, safe_clicks=_SAFE_B, surface=surface,
            confirmation=_APPROVE if approve else None, commit_timeout_ms=commit_timeout_ms,
            authority=_AUTHORITATIVE,
        )
    finally:
        await surface.close()


# D14 — confirmation at the commit boundary
async def test_commit_requires_confirmation(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    blocked = await _run(legacy_core_url, nav_policy, approve=False)
    assert isinstance(blocked, Failure)
    assert blocked.code is FailureCode.POLICY_DENIED
    assert mutations.commit_dispatch_count() == 0  # never dispatched without confirmation

    approved = await _run(legacy_core_url, nav_policy, approve=True)
    assert isinstance(approved, Success)
    assert mutations.commit_dispatch_count() == 1


# D15 — explicit application rejection is a business outcome
async def test_explicit_rejection_is_business_outcome(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    first = await _run(legacy_core_url, nav_policy)
    assert isinstance(first, Success)
    second = await _run(legacy_core_url, nav_policy)  # already exists
    assert isinstance(second, BusinessOutcome)
    assert second.code == "ACCOUNT_ALREADY_EXISTS"


# D16 — an ambiguous completion is resolved committed via read-back
async def test_commit_ambiguous_verified_committed(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(legacy_core_url, nav_policy, scenario="commit_ambiguous")
    assert isinstance(result, Success)  # neither confirmation nor rejection; read-back committed
    assert mutations.has_sub_account("12345")
    assert mutations.commit_dispatch_count() == 1


# D18 — read-back distinguishes committed vs not committed vs ambiguous
async def test_commit_dropped_is_not_committed(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(legacy_core_url, nav_policy, scenario="commit_dropped")
    assert isinstance(result, Failure)
    assert result.code is FailureCode.MUTATION_NOT_COMMITTED
    assert not mutations.has_sub_account("12345")


async def test_commit_unverifiable_is_ambiguous(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(legacy_core_url, nav_policy, scenario="commit_unverifiable")
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"


# D16/D17 — a REAL bounded timeout: the click raises after the server committed; the
# runtime does not re-dispatch and recovers the truth by read-back.
async def test_real_timeout_dispatched_once(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(
        legacy_core_url, nav_policy, scenario="commit_then_timeout", commit_timeout_ms=300
    )
    assert isinstance(result, Success)  # server committed; read-back recovered it
    assert mutations.commit_dispatch_count() == 1  # never blindly re-dispatched
    assert mutations.has_sub_account("12345")


# The embedded verification re-queries by ParameterRef, so an uncertain write replayed for
# a different member confirms THAT member — never a value baked in at discovery time.
async def test_verification_uses_the_replay_parameter(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(
        legacy_core_url, nav_policy, scenario="commit_then_timeout",
        commit_timeout_ms=300, member="54321",
    )
    assert isinstance(result, Success)
    assert mutations.has_sub_account("54321")  # verification re-derived the replay member
    assert not mutations.has_sub_account("12345")  # never a discovery-baked value


async def _escalated_dialog_session(
    url: str, nav_policy: NavigationPolicy
) -> tuple[ReplaySession, OperatorController]:
    """Drive Capability B until its post-commit verification is blocked by a dialog and
    the session pauses (MUTATION_AMBIGUOUS), leaving the live surface open for takeover."""
    surface = PlaywrightSurface()
    await surface.start()
    await surface.goto(f"{url}/?scenario=verification_dialog")
    session = ReplaySession(
        _capability_b(), {"member_number": "12345"}, url,
        nav_policy=nav_policy, safe_clicks=_SAFE_B, surface=surface,
        confirmation=_APPROVE, authority=_AUTHORITATIVE, lease=ControlLease(),
    )
    assert await session.start() is None
    result = await session.advance()
    assert isinstance(result, Escalated)
    assert result.code == "MUTATION_AMBIGUOUS"
    assert mutations.commit_dispatch_count() == 1  # the write dispatched exactly once
    return session, OperatorController(session)


# The end-to-end mutation handoff: commit once, verification blocked, human takes the SAME
# session, clears the blocker, resumes; automation re-verifies (read-only) and completes.
async def test_ambiguous_mutation_recovers_via_same_session_takeover(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    session, operator = await _escalated_dialog_session(legacy_core_url, nav_policy)
    surface_before = session.surface
    try:
        operator.take_control()
        assert session.lease.owner is ControlOwner.HUMAN  # automation is fenced
        # The human resolves only the read blocker on the same live session.
        await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
        final = await operator.resume()
        assert isinstance(final, Success)
        assert final.model_calls == 0
        assert session.surface is surface_before  # same Surface/session throughout
        assert mutations.commit_dispatch_count() == 1  # re-verify never re-dispatched
        assert session.lease.owner is ControlOwner.AUTOMATION  # control handed back
    finally:
        await session.surface.close()


async def test_ambiguous_mutation_stays_escalated_until_resolved(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    session, operator = await _escalated_dialog_session(legacy_core_url, nav_policy)
    try:
        operator.take_control()
        # Resume WITHOUT clearing the blocker: re-verification still cannot establish
        # the effect, so it remains escalated (never a false success), dispatch unchanged.
        still = await operator.resume()
        assert isinstance(still, Escalated)
        assert still.code == "MUTATION_AMBIGUOUS"
        assert mutations.commit_dispatch_count() == 1
        assert session.lease.owner is ControlOwner.HUMAN  # control retained for retry
    finally:
        await session.surface.close()
