"""Deterministic replay reliability: bounded transient recovery, ambiguity fail-
closed with no action, typed failures for driver errors, ordered locator fallbacks,
the caller-owned surface seam, and observed state on checkpoint failure.

These use an injected fake surface so every branch is exercised deterministically
without a browser, and prove model_calls stays 0.
"""

import os

from computer_use.execution import replay
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ExtractAction,
    Failure,
    FailureCode,
    Heading,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    RiskClass,
    SecretRef,
    Sensitivity,
    Step,
    Success,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)
from computer_use.safety import EnvSecretProvider, NavigationPolicy
from computer_use.surface import (
    SurfaceDriverError,
    SurfaceError,
    SurfaceTransientError,
    TargetAmbiguousError,
    TargetNotFoundError,
)

_SAFE = frozenset({"Search"})
# The fake's URLs live under this scope, so navigation checks pass deterministically.
_NAV = NavigationPolicy(
    allowed_origins=frozenset({"http://legacy"}),
    allowed_routes=frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"}),
)


class _FakeSurface:
    """A scripted surface exercising only the methods replay calls."""

    def __init__(
        self,
        *,
        extract_value: str = "312.45",
        heading: str = "Member Profile",
        counts: dict[str, int] | None = None,
        transient_before: int = 0,
        driver_on: str | None = None,
        goto_error: SurfaceError | None = None,
        click_error: SurfaceError | None = None,
    ) -> None:
        self.clicks: list[TargetDescriptor] = []
        self.types: list[tuple[str | None, str]] = []
        self.extracts: list[TargetDescriptor] = []
        self.closed = False
        self._extract_value = extract_value
        self._heading = heading
        self._counts = counts or {}
        self._transient_before = transient_before
        self._driver_on = driver_on
        self._goto_error = goto_error
        self._click_error = click_error

    @staticmethod
    def _key(target: TargetDescriptor) -> str | None:
        if target.name:
            return target.name
        if target.table_cell:
            return target.table_cell.column_header
        return target.text or target.label

    async def start(self) -> None: ...

    async def goto(self, url: str) -> None:
        if self._goto_error is not None:
            raise self._goto_error

    async def wait_settled(self) -> None: ...

    async def count(self, target: TargetDescriptor) -> int:
        if self._transient_before > 0:
            self._transient_before -= 1
            raise SurfaceTransientError("execution context was destroyed")
        if self._driver_on == "count":
            raise SurfaceDriverError("driver crashed")
        return self._counts.get(self._key(target) or "", 1)

    async def click(self, target: TargetDescriptor) -> None:
        if self._driver_on == "click":
            raise SurfaceDriverError("driver crashed")
        if self._click_error is not None:
            raise self._click_error
        self.clicks.append(target)

    async def type_text(self, target: TargetDescriptor, text: str) -> None:
        self.types.append((self._key(target), text))

    async def extract(self, target: TargetDescriptor) -> str:
        self.extracts.append(target)
        return self._extract_value

    async def has_text(self, text: str) -> bool:
        return False

    async def has_heading(self, name: str) -> bool:
        return name == self._heading

    async def has_blocking_dialog(self) -> bool:
        return False

    async def current_route(self) -> str:
        return "/workspace/member/00000"

    async def current_url(self) -> str:
        return "http://legacy/workspace/member/00000"

    async def scope_urls(self) -> list[str]:
        return [await self.current_url()]

    async def primary_heading(self) -> str | None:
        return self._heading

    async def close(self) -> None:
        self.closed = True


def _capability(*, fallback: bool = False) -> Capability:
    search: TargetDescriptor = TargetDescriptor(role="button", name="Search")
    if fallback:
        search = TargetDescriptor(
            role="button",
            name="Find",
            fallbacks=[TargetDescriptor(role="button", name="Search")],
        )
    return Capability(
        id="member.lookup_savings_balance",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        steps=[
            Step(
                id="s1",
                action=TypeAction(value=ParameterRef(name="member_number")),
                target=TargetDescriptor(role="textbox", name="Member Number"),
                risk=RiskClass.READ_ONLY,
            ),
            Step(
                id="s2",
                action=ClickAction(),
                target=search,
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
                outcomes=[
                    Outcome(
                        code="MEMBER_NOT_FOUND",
                        outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                        detector=Condition(text_present="Member record not found"),
                    )
                ],
            ),
            Step(
                id="s3",
                action=ExtractAction(),
                target=TargetDescriptor(
                    table_cell=TableCellTarget(
                        row_contains="Share Savings", column_header="Current Balance"
                    )
                ),
                risk=RiskClass.READ_ONLY,
                output="savings_balance",
            ),
        ],
        success_checkpoint=Condition(output_present="savings_balance"),
    )


async def _run(surface: _FakeSurface, *, fallback: bool = False, timeout: int = 5000) -> object:
    return await replay(
        _capability(fallback=fallback),
        {"member_number": "12345"},
        "http://legacy",
        nav_policy=_NAV,
        safe_clicks=_SAFE,
        surface=surface,
        resolve_timeout_ms=timeout,
    )


async def test_injected_surface_stays_open_and_model_free() -> None:
    fake = _FakeSurface()
    result = await _run(fake)
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "312.45"
    assert result.model_calls == 0
    assert fake.closed is False  # caller owns the injected session (handoff seam)


async def test_ambiguous_locator_fails_closed_without_acting() -> None:
    fake = _FakeSurface(counts={"Member Number": 2})
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.LOCATOR_AMBIGUOUS
    assert fake.types == []  # never acted on an ambiguous target
    assert fake.clicks == []


async def test_target_missing_when_nothing_resolves() -> None:
    fake = _FakeSurface(counts={"Member Number": 0})
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.TARGET_MISSING


async def test_transient_condition_recovers_within_budget() -> None:
    fake = _FakeSurface(transient_before=1)  # first resolve raises transient, retry succeeds
    result = await _run(fake)
    assert isinstance(result, Success)
    assert result.model_calls == 0


async def test_transient_condition_exhausts_to_typed_failure() -> None:
    fake = _FakeSurface(transient_before=999)  # never recovers
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.SURFACE_ERROR


async def test_driver_error_becomes_typed_failure_not_exception() -> None:
    fake = _FakeSurface(driver_on="click")
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.SURFACE_ERROR


async def test_goto_failure_is_a_typed_failure_not_an_exception() -> None:
    fake = _FakeSurface(goto_error=SurfaceDriverError("navigation failed"))
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.SURFACE_ERROR


async def test_resolve_act_race_missing_target_is_typed() -> None:
    # resolve counts 1, but the element vanishes before the click.
    fake = _FakeSurface(click_error=TargetNotFoundError("element gone"))
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.TARGET_MISSING


async def test_resolve_act_race_ambiguous_target_is_typed() -> None:
    fake = _FakeSurface(click_error=TargetAmbiguousError("two matches now"))
    result = await _run(fake)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.LOCATOR_AMBIGUOUS


async def test_locator_fallback_succeeds_when_primary_missing() -> None:
    fake = _FakeSurface(counts={"Find": 0})  # primary missing; fallback "Search" resolves
    result = await _run(fake, fallback=True)
    assert isinstance(result, Success)
    assert fake.clicks[0].name == "Search"  # the ordered fallback was used


async def test_ambiguous_primary_is_not_dodged_by_a_fallback() -> None:
    fake = _FakeSurface(counts={"Find": 2})  # primary ambiguous, though a clean fallback exists
    result = await _run(fake, fallback=True)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.LOCATOR_AMBIGUOUS
    assert fake.clicks == []  # ambiguity is never escaped by trying the fallback


async def test_checkpoint_failure_reports_observed_state() -> None:
    fake = _FakeSurface(heading="Member Inquiry")  # never reaches "Member Profile"
    result = await _run(fake, timeout=200)
    assert isinstance(result, Failure)
    assert result.code is FailureCode.CHECKPOINT_FAILED
    assert result.observed is not None
    assert "Member Inquiry" in result.observed


def _secret_capability() -> Capability:
    return Capability(
        id="member.authenticated_lookup",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={},
        outputs={},
        steps=[
            Step(
                id="s1",
                action=TypeAction(value=SecretRef(name="legacy_password")),
                target=TargetDescriptor(role="textbox", name="Password"),
                risk=RiskClass.READ_ONLY,
            )
        ],
        success_checkpoint=Condition(heading=Heading(role="heading", name="Member Profile")),
    )


async def test_replay_resolves_secret_through_the_provider_seam() -> None:
    # A SecretRef step must actually execute in normal replay when a provider is
    # supplied — and the raw secret is used transiently, never returned or persisted.
    os.environ["LC_SECRET_LEGACY_PASSWORD"] = "CANARY_SECRET_ZZ"
    try:
        fake = _FakeSurface()
        result = await replay(
            _secret_capability(),
            {},
            "http://legacy",
            nav_policy=_NAV,
            safe_clicks=_SAFE,
            surface=fake,
            secrets=EnvSecretProvider(),
        )
        assert isinstance(result, Success)
        assert ("Password", "CANARY_SECRET_ZZ") in fake.types  # the secret was actually typed
        assert "CANARY_SECRET_ZZ" not in result.model_dump_json()  # not in the returned result
    finally:
        del os.environ["LC_SECRET_LEGACY_PASSWORD"]


async def test_replay_without_provider_cannot_resolve_a_secret() -> None:
    # No provider wired: a SecretRef step fails closed (never a blank or guessed value).
    fake = _FakeSurface()
    result = await replay(
        _secret_capability(), {}, "http://legacy", nav_policy=_NAV, safe_clicks=_SAFE, surface=fake
    )
    assert isinstance(result, Failure)
    assert result.code is FailureCode.POLICY_DENIED  # SECRET_UNAVAILABLE maps here
    assert fake.types == []
