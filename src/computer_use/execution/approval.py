"""Typed one-time approval seam for consequential actions.

The trusted kernel never prompts a human directly. When a consequential proposal
lacks a standing approval, the kernel raises ``ApprovalRequired`` carrying a
sanitized ``ApprovalRequest``. Orchestration (an operator console) obtains a human
decision and, on approval, returns a one-time ``ApprovalGrant``. The kernel then
re-resolves the target and recomputes the operation fingerprint immediately before
dispatch, consuming the grant only if it still describes the same operation on the
same observable state; otherwise the approval is stale and nothing is dispatched.

This keeps ``TrustedKernel`` deterministic and testable and gives a production seam
in which authority to commit a consequential write comes from a human, never from
the model and never from serialized artifact data.
"""

from __future__ import annotations

from dataclasses import dataclass

from computer_use.model import ProposedActionType, RiskClass, TargetDescriptor


@dataclass(frozen=True)
class OperationFingerprint:
    """Minimal semantic identity of a consequential operation.

    Structural only (never a raw value): the action, the resolved target's identity,
    the primary landmark at the confirmation point, and the control epoch. It is the
    smallest fingerprint sufficient to prove the approved operation has not materially
    changed before dispatch — deliberately not a hash of the whole page, so an
    unrelated dynamic element (a clock, a banner) cannot invalidate a safe approval.
    """

    action: str
    target_role: str | None
    target_name: str | None
    row_contains: str | None
    column_header: str | None
    landmark: str | None
    epoch: int | None


def fingerprint_of(
    action: ProposedActionType,
    target: TargetDescriptor,
    landmark: str | None,
    epoch: int | None,
) -> OperationFingerprint:
    cell = target.table_cell
    return OperationFingerprint(
        action=action.value,
        target_role=target.role,
        target_name=target.name,
        row_contains=cell.row_contains if cell else None,
        column_header=cell.column_header if cell else None,
        landmark=landmark,
        epoch=epoch,
    )


@dataclass(frozen=True)
class ApprovalRequest:
    """A sanitized request for one-time human authorization of a consequential action."""

    proposal_nonce: str
    risk: RiskClass
    fingerprint: OperationFingerprint


@dataclass(frozen=True)
class ApprovalGrant:
    """A one-time human authorization for exactly one consequential operation."""

    proposal_nonce: str
    fingerprint: OperationFingerprint


class ApprovalRequired(Exception):
    """Raised by the trusted kernel when a consequential action needs authorization.

    Not a ``KernelRejection``: it is a control-flow signal to orchestration, not a
    terminal refusal. Orchestration obtains a human decision and re-invokes the kernel
    with an ``ApprovalGrant`` (or treats a denial as a blocked action and escalates).
    """

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        super().__init__(f"approval required: {request.fingerprint.action}")
