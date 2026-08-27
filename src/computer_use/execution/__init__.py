"""Execution layer: the trusted kernel that owns authority over the surface."""

from __future__ import annotations

from .approval import (
    ApprovalGrant,
    ApprovalRequest,
    ApprovalRequired,
    OperationFingerprint,
)
from .kernel import (
    KernelExecution,
    KernelRejection,
    MutationDispatchUncertain,
    RejectionCode,
    TrustedKernel,
    ValueResolver,
)
from .lease import ControlLease, ControlLeaseError
from .replay import replay
from .session import InterventionSignal, ReplaySession

__all__ = [
    "ApprovalGrant",
    "ApprovalRequest",
    "ApprovalRequired",
    "ControlLease",
    "ControlLeaseError",
    "InterventionSignal",
    "KernelExecution",
    "KernelRejection",
    "MutationDispatchUncertain",
    "OperationFingerprint",
    "RejectionCode",
    "ReplaySession",
    "TrustedKernel",
    "ValueResolver",
    "replay",
]
