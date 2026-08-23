"""Opt-in genuine Anthropic discovery — the only test that calls a real model API.

Run it explicitly with ``uv run pytest --run-live`` and ``ANTHROPIC_API_KEY`` set.
A bare ``pytest`` never runs it, even when the key is exported, so an ordinary run
does not spend credits.
"""

import os

import pytest

from computer_use.discovery import GoalSpec, compile_capability, discover
from computer_use.discovery.anthropic_model import AnthropicDiscoveryModel
from computer_use.execution import TrustedKernel, ValueResolver, replay
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    OutputSpec,
    ParamType,
    ProposedActionType,
    Sensitivity,
    Success,
)
from computer_use.safety import Policy, RiskClassifier
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
    )


async def test_genuine_anthropic_discovery_then_replay(legacy_core_url: str) -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY required for --run-live")
    spec = _spec()
    surface = PlaywrightSurface()
    await surface.start()
    try:
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver({"member_number": "12345"}),
        )
        outcome = await discover(
            AnthropicDiscoveryModel(), surface, kernel, spec, legacy_core_url
        )
    finally:
        await surface.close()

    assert outcome.stop_reason == "GOAL_REACHED"
    capability = compile_capability(outcome.trace, spec)

    result = await replay(
        capability, {"member_number": "54321"}, legacy_core_url, safe_clicks=_SAFE_CLICKS
    )
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "312.45"
    assert result.model_calls == 0
