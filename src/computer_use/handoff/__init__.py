"""Human-in-the-loop handoff: exclusive same-session control transfer.

Automation pauses and reports a typed intervention; a human takes exclusive
control of the same live session, resolves the blocked state with bounded audited
actions, and hands control back; the runtime reconciles observable state before
automation resumes.
"""

from __future__ import annotations

from .intervention import InterventionReason, InterventionRequest
from .operator import (
    ClickControl,
    HandoffSession,
    HumanAction,
    OperatorController,
    OperatorError,
    OperatorScopeError,
    TypeControl,
)

__all__ = [
    "ClickControl",
    "HandoffSession",
    "HumanAction",
    "InterventionReason",
    "InterventionRequest",
    "OperatorController",
    "OperatorError",
    "OperatorScopeError",
    "TypeControl",
]
