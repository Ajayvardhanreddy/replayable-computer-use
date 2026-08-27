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
    # The structural route pattern the action occurred on (a route_label, never a
    # concrete path with a member id). Lets a later compiler reason about where an
    # independent verification read happened without inspecting the live surface.
    route: str | None = None
    # A stable landmark (e.g. a heading) observed after the action, used to
    # synthesize a deterministic postcondition.
    observed_landmark: str | None = None
    # The primary heading before a state-changing action. The compiler requires a
    # genuine before/after delta, so a checkpoint is never an already-true state.
    heading_before: str | None = None
    # Model-proposed expected effect (non-authoritative intent) carried for review;
    # it never establishes a postcondition — software owns the observed delta.
    expected_effect: str | None = None


class DiscoveryTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[TraceStep]
