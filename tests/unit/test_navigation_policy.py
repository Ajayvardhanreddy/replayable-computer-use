"""C18: a configured domain/route allowlist, enforced in trusted software.

The policy validates the full URL (scheme/host/port/path), so an out-of-scope
redirect that keeps a valid-looking path is still refused.
"""

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
    OutputSpec,
    ParameterRef,
    ParamType,
    PolicyEffect,
    RiskClass,
    Sensitivity,
    Step,
    TargetDescriptor,
    TypeAction,
)
from computer_use.safety import NavigationPolicy, route_label

_ORIGINS = frozenset({"http://localhost:8000"})
_ROUTES = frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"})


def _policy() -> NavigationPolicy:
    return NavigationPolicy(allowed_origins=_ORIGINS, allowed_routes=_ROUTES)


def test_allowed_origin_and_route_pass() -> None:
    decision = _policy().check("http://localhost:8000/workspace/member/12345")
    assert decision.effect is PolicyEffect.ALLOW


def test_out_of_scope_origin_blocked_even_with_valid_route() -> None:
    # a redirect to another host with a valid-looking path is still refused
    decision = _policy().check("http://evil.example/workspace/member/12345")
    assert decision.effect is PolicyEffect.DENY
    assert decision.rule == "navigation_origin"


def test_out_of_scope_route_blocked() -> None:
    decision = _policy().check("http://localhost:8000/admin/settings")
    assert decision.effect is PolicyEffect.DENY
    assert decision.rule == "navigation_route"


def test_param_route_matches_exactly_one_segment() -> None:
    ok = _policy().check("http://localhost:8000/workspace/member/54321")
    assert ok.effect is PolicyEffect.ALLOW
    extra = _policy().check("http://localhost:8000/workspace/member/54321/edit")
    assert extra.effect is PolicyEffect.DENY


class _NoopSurface:
    """A surface that must never be touched, so we can assert no action happened."""

    def __init__(self) -> None:
        self.touched = False

    async def start(self) -> None:
        self.touched = True

    async def goto(self, url: str) -> None:
        self.touched = True


def _min_capability() -> Capability:
    return Capability(
        id="c",
        version=1,
        target=CapabilityTarget(vendor="v", application_family="f"),
        inputs={},
        outputs={"balance": OutputSpec(type=ParamType.DECIMAL)},
        steps=[
            Step(
                id="s1",
                action=ExtractAction(),
                target=TargetDescriptor(text="Balance"),
                risk=RiskClass.READ_ONLY,
                output="balance",
            )
        ],
        success_checkpoint=Condition(output_present="balance"),
    )


async def test_replay_refuses_out_of_scope_target_before_acting() -> None:
    fake = _NoopSurface()
    result = await replay(
        _min_capability(), {}, "http://evil.example", surface=fake, nav_policy=_policy()
    )
    assert isinstance(result, Failure)
    assert result.code is FailureCode.POLICY_DENIED
    assert fake.touched is False  # blocked before goto / any action


async def test_navigation_denial_persists_only_the_structural_rule() -> None:
    # An off-scope target whose path carries a canary: the denial diagnostic must be
    # the structural rule, never the concrete URL/path.
    fake = _NoopSurface()
    result = await replay(
        _min_capability(),
        {},
        "http://evil.example/workspace/member/CANARY_9",
        surface=fake,
        nav_policy=_policy(),
    )
    assert isinstance(result, Failure)
    assert result.code is FailureCode.POLICY_DENIED
    assert result.observed == "navigation denied: navigation_origin"
    assert "CANARY_9" not in (result.observed or "")
    assert "evil.example" not in (result.observed or "")


def test_route_label_returns_pattern_not_concrete_path() -> None:
    routes = frozenset({"/workspace/member/:member_number", "/workspace/inquiry"})
    assert route_label("/workspace/member/CANARY_9", routes) == "/workspace/member/:member_number"
    assert route_label("/admin/secret", routes) == "<off-scope>"
    assert "CANARY_9" not in route_label("/workspace/member/CANARY_9", routes)


class _MemberPageSurface:
    """A surface stuck on a member page (checkpoint never satisfied) so we can
    inspect what a failure persists. The member number is a canary."""

    def __init__(self, member: str) -> None:
        self._member = member
        self.clicks: list[TargetDescriptor] = []

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def wait_settled(self) -> None: ...
    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def click(self, target: TargetDescriptor) -> None:
        self.clicks.append(target)

    async def type_text(self, target: TargetDescriptor, text: str) -> None: ...
    async def extract(self, target: TargetDescriptor) -> str:
        return "9,999.99"

    async def has_text(self, text: str) -> bool:
        return False

    async def has_heading(self, name: str) -> bool:
        return False  # never reaches "Member Profile" -> checkpoint fails

    async def has_blocking_dialog(self) -> bool:
        return False

    async def current_route(self) -> str:
        return f"/workspace/member/{self._member}"

    async def current_url(self) -> str:
        return f"http://localhost:8000/workspace/member/{self._member}"

    async def primary_heading(self) -> str | None:
        return "Member Inquiry"

    async def close(self) -> None: ...


def _member_capability() -> Capability:
    return Capability(
        id="c",
        version=1,
        target=CapabilityTarget(vendor="v", application_family="f"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={"balance": OutputSpec(type=ParamType.DECIMAL)},
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
                target=TargetDescriptor(role="button", name="Search"),
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
            ),
            Step(
                id="s3",
                action=ExtractAction(),
                target=TargetDescriptor(text="Balance"),
                risk=RiskClass.READ_ONLY,
                output="balance",
            ),
        ],
        success_checkpoint=Condition(output_present="balance"),
    )


async def test_failure_observed_never_persists_the_member_number() -> None:
    fake = _MemberPageSurface("CANARY_MEMBER_9")
    result = await replay(
        _member_capability(),
        {"member_number": "CANARY_MEMBER_9"},
        "http://localhost:8000",
        safe_clicks=frozenset({"Search"}),
        surface=fake,
        nav_policy=_policy(),
        resolve_timeout_ms=100,
    )
    assert isinstance(result, Failure)
    assert result.code is FailureCode.CHECKPOINT_FAILED
    assert result.observed is not None
    assert "CANARY_MEMBER_9" not in result.observed  # PII (member id) not persisted
    assert "/workspace/member/:member_number" in result.observed  # safe route pattern kept
