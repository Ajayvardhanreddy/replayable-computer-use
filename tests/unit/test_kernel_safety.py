"""Safety enforced by the trusted kernel: configured action allowlist (C19),
hostile-steered actions blocked (C20), confirmation/approval (C21), and secret
resolution that never leaks (C22/C25)."""

import os

import pytest

from computer_use.execution.kernel import (
    KernelRejection,
    RejectionCode,
    TrustedKernel,
    ValueResolver,
)
from computer_use.model import (
    ClickAction,
    ParameterRef,
    ProposedAction,
    ProposedActionType,
    RiskClass,
    SecretRef,
    Step,
    TargetDescriptor,
    TypeAction,
)
from computer_use.observability import step_executed_event
from computer_use.safety import ConfirmationPolicy, EnvSecretProvider, Policy, RiskClassifier
from computer_use.surface import Candidate

_ALL = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)


class _KSurface:
    """Minimal surface exercising only what the kernel calls."""

    def __init__(self, count: int = 1) -> None:
        self.clicks: list[TargetDescriptor] = []
        self.types: list[tuple[TargetDescriptor, str]] = []
        self._count = count

    async def count(self, target: TargetDescriptor) -> int:
        return self._count

    async def click(self, target: TargetDescriptor) -> None:
        self.clicks.append(target)

    async def type_text(self, target: TargetDescriptor, text: str) -> None:
        self.types.append((target, text))

    async def extract(self, target: TargetDescriptor) -> str:
        return "x"


def _transfer_step(step_id: str = "submit_transfer") -> Step:
    return Step(
        id=step_id,
        action=ClickAction(),
        target=TargetDescriptor(role="button", name="Transfer"),
        risk=RiskClass.CONSEQUENTIAL_WRITE,
    )


async def test_configured_action_allowlist_blocks_a_schema_valid_action() -> None:
    # TYPE is a valid action, but this capability's policy permits only CLICK/EXTRACT.
    fake = _KSurface()
    kernel = TrustedKernel(
        fake,
        Policy(frozenset({ProposedActionType.CLICK, ProposedActionType.EXTRACT})),
        RiskClassifier(),
        ValueResolver({"member_number": "1"}),
    )
    proposal = ProposedAction(
        action=ProposedActionType.TYPE, candidate_id="c1", value=ParameterRef(name="member_number")
    )
    candidates = {"c1": Candidate(id="c1", role="textbox", name="Member Number")}
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute(proposal, candidates)
    assert exc.value.code is RejectionCode.POLICY_DENIED
    assert fake.types == []  # never dispatched


async def test_hostile_steered_consequential_action_is_blocked() -> None:
    # The model is steered (say by hostile page text) to click a dangerous control;
    # trusted software blocks it regardless of what the page or model wants.
    fake = _KSurface()
    kernel = TrustedKernel(
        fake,
        Policy(_ALL),
        RiskClassifier(safe_click_names=frozenset({"Search"})),
        ValueResolver({}),
    )
    proposal = ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1")
    candidates = {"c1": Candidate(id="c1", role="button", name="Transfer Funds")}
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute(proposal, candidates)
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert fake.clicks == []


async def test_consequential_action_requires_configured_approval() -> None:
    fake = _KSurface()
    kernel = TrustedKernel(
        fake, Policy(_ALL), RiskClassifier(safe_click_names=frozenset()), ValueResolver({})
    )
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute_step(_transfer_step())
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert fake.clicks == []


async def test_consequential_action_dispatches_when_operation_approved() -> None:
    fake = _KSurface()
    kernel = TrustedKernel(
        fake,
        Policy(_ALL),
        RiskClassifier(safe_click_names=frozenset()),
        ValueResolver({}),
        ConfirmationPolicy(approved=frozenset({"submit_transfer"})),
    )
    execution = await kernel.execute_step(_transfer_step())
    assert execution.risk is RiskClass.CONSEQUENTIAL_WRITE
    assert len(fake.clicks) == 1  # approved operation dispatched


async def test_approval_is_bound_to_the_operation_not_the_control_name() -> None:
    fake = _KSurface()
    # a different step id with the SAME control name does not inherit the approval
    kernel = TrustedKernel(
        fake,
        Policy(_ALL),
        RiskClassifier(safe_click_names=frozenset()),
        ValueResolver({}),
        ConfirmationPolicy(approved=frozenset({"submit_transfer"})),
    )
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute_step(_transfer_step("submit_other"))
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert fake.clicks == []


async def test_secret_is_used_but_never_appears_in_execution_or_evidence() -> None:
    os.environ["LC_SECRET_LEGACY_PASSWORD"] = "CANARY_SECRET_9Z"
    try:
        fake = _KSurface()
        kernel = TrustedKernel(
            fake,
            Policy(_ALL),
            RiskClassifier(),
            ValueResolver({}, secrets=EnvSecretProvider()),
        )
        step = Step(
            id="pw",
            action=TypeAction(value=SecretRef(name="legacy_password")),
            target=TargetDescriptor(role="textbox", name="Password"),
            risk=RiskClass.READ_ONLY,
        )
        execution = await kernel.execute_step(step)
        assert fake.types[0][1] == "CANARY_SECRET_9Z"  # the secret was actually used
        assert execution.value == SecretRef(name="legacy_password")  # record stays symbolic
        event = step_executed_event("run", 1, execution, None)
        assert event.attributes["value"] == "<secret>"
        assert "CANARY_SECRET_9Z" not in event.model_dump_json()
    finally:
        del os.environ["LC_SECRET_LEGACY_PASSWORD"]


async def test_missing_secret_fails_safely_without_a_value() -> None:
    fake = _KSurface()
    kernel = TrustedKernel(
        fake, Policy(_ALL), RiskClassifier(), ValueResolver({}, secrets=EnvSecretProvider())
    )
    step = Step(
        id="pw",
        action=TypeAction(value=SecretRef(name="absent")),
        target=TargetDescriptor(role="textbox", name="Password"),
        risk=RiskClass.READ_ONLY,
    )
    with pytest.raises(KernelRejection) as exc:
        await kernel.execute_step(step)
    assert exc.value.code is RejectionCode.SECRET_UNAVAILABLE
    assert fake.types == []
