"""Opt-in genuine Anthropic discovery-side handoff.

Run with ``uv run pytest --run-live`` and ``ANTHROPIC_API_KEY`` set. The real model
drives an ordinary member lookup, reaches a flagged account whose verification it
was never given the credential for, and — from its normal action schema — proposes
``request_human``. A human then enters the employee code on the SAME live session and
control returns to the model, which finishes the goal. A bare ``pytest`` never runs
this, so ordinary runs spend no credits.
"""

import os
from pathlib import Path

import pytest

from computer_use.discovery import GoalSpec, OutcomeBinding, compile_capability, discover
from computer_use.discovery.anthropic_model import AnthropicDiscoveryModel
from computer_use.execution import ControlLease, TrustedKernel, ValueResolver
from computer_use.handoff import OperatorController, TypeControl
from computer_use.model import (
    CapabilityTarget,
    Condition,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParamType,
    ProposedActionType,
    Sensitivity,
    TargetDescriptor,
)
from computer_use.observability import EvidenceStore
from computer_use.safety import EnvSecretProvider, NavigationPolicy, Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface

pytestmark = pytest.mark.live

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
_SAFE_CLICKS = frozenset({"Search"})


def _spec() -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal="Look up this member and return their savings balance",
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        success_output="savings_balance",
        business_outcomes=[
            OutcomeBinding(
                action=ProposedActionType.CLICK,
                target=TargetDescriptor(role="button", name="Search"),
                outcome=Outcome(
                    code="MEMBER_NOT_FOUND",
                    outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                    detector=Condition(text_present="Member record not found"),
                ),
            )
        ],
    )


async def _one_live_run(
    legacy_core_url: str, nav_policy: NavigationPolicy, trace_path: Path
) -> tuple[bool, object]:
    """One genuine discovery run; returns (model_escalated, outcome)."""
    surface = PlaywrightSurface()
    await surface.start()
    store = EvidenceStore(trace_path)
    lease = ControlLease()
    called: dict[str, bool] = {"handler": False}

    async def human_handler(operator: OperatorController, reason: str | None = None) -> bool:
        called["handler"] = True  # only invoked because the real model proposed request_human
        operator.take_control()
        await operator.perform(
            TypeControl(
                TargetDescriptor(role="textbox", name="Employee Verification Code"),
                "4729",
                submit=True,
            )
        )
        operator.release_to_automation()
        return True

    try:
        await surface.goto(f"{legacy_core_url}/?scenario=verification_required")
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver({"member_number": "12345"}, EnvSecretProvider()),
            lease=lease,
        )
        outcome = await discover(
            AnthropicDiscoveryModel(), surface, kernel, _spec(), legacy_core_url,
            nav_policy=nav_policy, evidence=store, lease=lease, on_human_request=human_handler,
        )
    finally:
        await surface.close()
    return called["handler"], outcome


async def test_live_model_requests_human_and_continues(
    legacy_core_url: str, nav_policy: NavigationPolicy, tmp_path: Path
) -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY required for --run-live")

    # A genuine model is stochastic: it usually escalates when it lacks the credential,
    # but not on every run. Give it a few attempts, and if it never escalates, skip
    # rather than fail — the mechanism itself is proven deterministically elsewhere.
    for attempt in range(3):
        trace_path = tmp_path / f"trace_{attempt}.jsonl"
        escalated, outcome = await _one_live_run(legacy_core_url, nav_policy, trace_path)
        if escalated:
            assert outcome.stop_reason == "GOAL_REACHED"  # type: ignore[attr-defined]
            capability = compile_capability(outcome.trace, _spec())  # type: ignore[attr-defined]
            assert any(step.output == "savings_balance" for step in capability.steps)
            events = trace_path.read_text(encoding="utf-8")
            assert '"intervention_raised"' in events
            assert "4729" not in events  # the human's employee code never reaches evidence
            return
    pytest.skip("the model did not escalate to a human in the allotted attempts this run")
