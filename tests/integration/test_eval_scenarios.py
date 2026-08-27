"""Deterministic eval scenarios: LegacyCore world states map to typed replay outcomes.

Each is a model-free replay (``model_calls = 0``) against an armed scenario, asserting the typed
RunResult and its safe evidence. These are the baseline rows of the deterministic eval harness —
the runtime classifies each world state generically (a bound business outcome, or an unmet
postcondition), never by app-specific text.
"""

from pathlib import Path

from computer_use.execution import replay
from computer_use.model import BusinessOutcome, Capability, Failure, FailureCode, RunResult
from computer_use.safety import NavigationPolicy
from computer_use.surface import PlaywrightSurface

_ARTIFACT = Path(__file__).resolve().parents[2] / "evidence/capability/member_lookup.v1.json"
_SAFE_CLICKS = frozenset({"Search"})


def _capability() -> Capability:
    return Capability.model_validate_json(_ARTIFACT.read_text(encoding="utf-8"))


async def _run(url: str, nav_policy: NavigationPolicy, scenario: str) -> RunResult:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{url}/?scenario={scenario}")  # arm the scenario (sets a cookie)
        return await replay(
            _capability(), {"member_number": "54321"}, url,
            nav_policy=nav_policy, safe_clicks=_SAFE_CLICKS, surface=surface,
        )
    finally:
        await surface.close()


async def test_not_found_scenario_yields_business_outcome(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    # A test-world switch forces "no record" even for an otherwise valid id.
    result = await _run(legacy_core_url, nav_policy, "not_found")
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.model_calls == 0


async def test_session_expired_yields_typed_failure(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(legacy_core_url, nav_policy, "session_expired")
    assert isinstance(result, Failure)
    assert result.code is FailureCode.CHECKPOINT_FAILED
    # The unexpected state is named in the typed failure's observed detail.
    assert "Session Expired" in (result.observed or "")


async def test_permission_denied_yields_typed_failure(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    result = await _run(legacy_core_url, nav_policy, "permission_denied")
    assert isinstance(result, Failure)
    assert result.code is FailureCode.CHECKPOINT_FAILED
    assert "Access Denied" in (result.observed or "")
