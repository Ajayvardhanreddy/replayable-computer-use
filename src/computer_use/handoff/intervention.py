"""The typed intervention request routed to a human operator.

An ``InterventionRequest`` carries only what a human needs to decide and act:
stable identifiers, a reason code, current ownership, the structural route, and a
sanitized structural snapshot. It deliberately excludes raw PII, financial values,
arbitrary DOM, and unsafe screenshots — it reuses the same evidence-safety
boundary as every other persisted artifact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from computer_use.model import ControlOwner
from computer_use.observability import FailureEvidence


class InterventionReason(StrEnum):
    """Stable reason codes for routing a blocked run to a human.

    ``UNKNOWN_DIALOG`` is a blocking state observed by deterministic replay that the
    artifact does not model. ``HUMAN_REQUESTED`` is raised during discovery when the
    model itself judges it cannot safely proceed and proposes escalation. Both route
    to the same exclusive same-session takeover mechanism.
    """

    UNKNOWN_DIALOG = "UNKNOWN_DIALOG"
    HUMAN_REQUESTED = "HUMAN_REQUESTED"


class InterventionRequest(BaseModel):
    """Safe, self-contained context handed to an operator when automation pauses."""

    model_config = ConfigDict(extra="forbid")
    intervention_id: str
    run_id: str
    capability: str
    version: int
    step_id: str | None
    reason: InterventionReason
    control_owner: ControlOwner
    control_epoch: int
    # Structural route pattern (e.g. /workspace/member/:member_number), never the
    # concrete path — a path parameter can be sensitive.
    route: str
    evidence: FailureEvidence
    ts: datetime = Field(...)
