"""A successful discovery run captured as a typed trace (the compiler's input)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from computer_use.model import ProposedActionType, RiskClass, TargetDescriptor, ValueRef


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ProposedActionType
    target: TargetDescriptor
    risk: RiskClass
    value: ValueRef | None = None
    output: str | None = None
    # A stable landmark (e.g. a heading) observed after the action, used to
    # synthesize a deterministic postcondition.
    observed_landmark: str | None = None


class DiscoveryTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[TraceStep]
