"""Real same-session handoff against LegacyCore in a headless browser.

Proves the two properties that only a live browser can: an unexpected modal on
Capability A pauses automation and is resolved by a human operating the *same*
live session (same PlaywrightSurface, BrowserContext, and Page objects, never
closed or reconstructed), after which automation reconciles and completes with the
correct balance and zero model calls.
"""

from pathlib import Path

from computer_use.execution import ReplaySession
from computer_use.handoff import ClickControl, InterventionReason, OperatorController
from computer_use.model import (
    Capability,
    ControlOwner,
    Escalated,
    Success,
    TargetDescriptor,
)
from computer_use.safety import NavigationPolicy
from computer_use.surface import PlaywrightSurface

_SAFE_CLICKS = frozenset({"Search"})
_ARTIFACT = Path(__file__).parents[2] / "artifacts" / "member_lookup.v1.json"


def _capability() -> Capability:
    return Capability.model_validate_json(_ARTIFACT.read_text(encoding="utf-8"))


async def test_same_session_handoff_completes(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        # Arm the unexpected-dialog scenario for the whole flow (sets the cookie).
        await surface.goto(f"{legacy_core_url}/?scenario=unexpected_dialog")
        # Capture the live-session identity before anything pauses.
        session_id = surface.session_id
        context = surface.context
        page = surface.page

        session = ReplaySession(
            _capability(),
            {"member_number": "12345"},
            legacy_core_url,
            nav_policy=nav_policy,
            safe_clicks=_SAFE_CLICKS,
            surface=surface,
        )
        assert await session.start() is None

        paused = await session.advance()
        assert isinstance(paused, Escalated)
        assert paused.code == "UNKNOWN_DIALOG"
        assert paused.model_calls == 0
        # Still the same live session, paused (not closed).
        assert surface.is_live
        assert surface.session_id == session_id
        assert surface.context is context
        assert surface.page is page

        operator = OperatorController(session)
        request = await operator.raise_intervention()
        assert request.reason is InterventionReason.UNKNOWN_DIALOG
        # LegacyCore runs the workspace in an iframe, so the top-level route is the
        # shell; the member id lives only in the frame URL and never reaches the
        # persisted request. The structural landmark carries the useful signal.
        assert request.route in nav_policy.allowed_routes
        assert "Member Profile" in request.evidence.landmarks
        assert request.evidence.screenshot is None
        assert "12345" not in request.model_dump_json()

        operator.take_control()
        assert session.lease.owner is ControlOwner.HUMAN
        # The human resolves the modal on the exact same page automation was using.
        await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
        result = await operator.resume()

        assert session.lease.owner is ControlOwner.AUTOMATION
        assert isinstance(result, Success)
        assert result.outputs["savings_balance"] == "8421.31"
        assert result.model_calls == 0
        # The same objects survived the whole handoff — no reconstruction.
        assert surface.session_id == session_id
        assert surface.context is context
        assert surface.page is page
        assert surface.is_live
    finally:
        await surface.close()


async def test_resume_without_resolving_refuses_then_completes(
    legacy_core_url: str, nav_policy: NavigationPolicy
) -> None:
    # Resuming while the modal is still up must refuse (re-escalate) and keep the
    # human in control; only after the human actually resolves it does it complete.
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{legacy_core_url}/?scenario=unexpected_dialog")
        session = ReplaySession(
            _capability(),
            {"member_number": "12345"},
            legacy_core_url,
            nav_policy=nav_policy,
            safe_clicks=_SAFE_CLICKS,
            surface=surface,
            resolve_timeout_ms=1000,
        )
        await session.start()
        assert isinstance(await session.advance(), Escalated)
        operator = OperatorController(session)
        operator.take_control()
        refused = await operator.resume()  # modal not yet dismissed
        assert isinstance(refused, Escalated)
        assert session.lease.owner is ControlOwner.HUMAN  # control retained
        # The human resolves it on the same session, then resume completes.
        await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
        result = await operator.resume()
        assert isinstance(result, Success)
        assert result.outputs["savings_balance"] == "8421.31"
        assert result.model_calls == 0
    finally:
        await surface.close()
