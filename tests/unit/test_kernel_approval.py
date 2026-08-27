"""The one-time approval seam for consequential actions during authoring.

The trusted kernel never prompts: it raises a typed ApprovalRequired, and dispatch
happens only against a one-time grant whose fingerprint still matches the live
operation. A stale grant (the operation/state moved) is refused. With the seam off
(the default, used by replay), an unapproved consequential action is a terminal
RISK_CONFIRMATION_REQUIRED exactly as before.
"""

import pytest

from computer_use.execution import ApprovalGrant, ApprovalRequired, KernelRejection, TrustedKernel
from computer_use.execution.approval import fingerprint_of
from computer_use.execution.kernel import RejectionCode, ValueResolver
from computer_use.model import ProposedAction, ProposedActionType, RiskClass, TargetDescriptor
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import Candidate


class _Surface:
    """A minimal surface: one resolvable control and a fixed heading."""

    def __init__(self, heading: str = "Open Sub-Account") -> None:
        self.heading = heading
        self.clicks = 0

    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def primary_heading(self) -> str | None:
        return self.heading

    async def click(self, target: TargetDescriptor, *, timeout_ms: int | None = None) -> None:
        self.clicks += 1


_CANDIDATE = Candidate(id="c1", role="button", name="Create Account")
_PROPOSAL = ProposedAction(action=ProposedActionType.CLICK, candidate_id="c1")
_CANDIDATES = {"c1": _CANDIDATE}
# "Create Account" is not in the safe set -> classified CONSEQUENTIAL_WRITE.
_CLASSIFIER = RiskClassifier(safe_click_names=frozenset({"Search"}))
_POLICY = Policy(allowed_actions=frozenset({ProposedActionType.CLICK}))


def _kernel(surface: _Surface, *, interactive: bool) -> TrustedKernel:
    return TrustedKernel(
        surface, _POLICY, _CLASSIFIER, ValueResolver({}), interactive_approval=interactive
    )


async def test_consequential_action_raises_typed_approval_required() -> None:
    surface = _Surface()
    with pytest.raises(ApprovalRequired) as exc:
        await _kernel(surface, interactive=True).execute(_PROPOSAL, _CANDIDATES)
    fp = exc.value.request.fingerprint
    assert fp.action == "click" and fp.target_name == "Create Account"
    assert exc.value.request.risk is RiskClass.CONSEQUENTIAL_WRITE
    assert surface.clicks == 0  # nothing dispatched without authorization


async def test_matching_grant_dispatches_once() -> None:
    surface = _Surface()
    resolved = TargetDescriptor(role="button", name="Create Account")
    grant = ApprovalGrant(
        proposal_nonce="n1",
        fingerprint=fingerprint_of(ProposedActionType.CLICK, resolved, surface.heading, None),
    )
    execution = await _kernel(surface, interactive=True).execute(
        _PROPOSAL, _CANDIDATES, approval=grant
    )
    assert execution.risk is RiskClass.CONSEQUENTIAL_WRITE
    assert surface.clicks == 1


async def test_stale_grant_is_refused_and_does_not_dispatch() -> None:
    surface = _Surface(heading="Open Sub-Account")
    resolved = TargetDescriptor(role="button", name="Create Account")
    # The human approved against a different observable state (a different landmark).
    stale = ApprovalGrant(
        proposal_nonce="n1",
        fingerprint=fingerprint_of(ProposedActionType.CLICK, resolved, "Some Other Page", None),
    )
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface, interactive=True).execute(_PROPOSAL, _CANDIDATES, approval=stale)
    assert exc.value.code is RejectionCode.APPROVAL_STALE
    assert surface.clicks == 0


async def test_default_seam_off_is_terminal_confirmation_required() -> None:
    # Replay and every existing caller use the default (no interactive approval): an
    # unapproved consequential action is a terminal refusal, unchanged.
    surface = _Surface()
    with pytest.raises(KernelRejection) as exc:
        await _kernel(surface, interactive=False).execute(_PROPOSAL, _CANDIDATES)
    assert exc.value.code is RejectionCode.RISK_CONFIRMATION_REQUIRED
    assert surface.clicks == 0
