"""Discovery: capturing a successful run and compiling it into a capability."""

from __future__ import annotations

from .agent import DiscoveryOutcome, discover
from .compiler import CapabilityValidationError, GoalSpec, OutcomeBinding, compile_capability
from .model import (
    DiscoveryModel,
    GoalContext,
    InputInfo,
    ModelCandidate,
    ModelObservation,
    ModelOutputError,
)
from .trace import DiscoveryTrace, TraceStep

__all__ = [
    "CapabilityValidationError",
    "DiscoveryModel",
    "DiscoveryOutcome",
    "DiscoveryTrace",
    "GoalContext",
    "GoalSpec",
    "InputInfo",
    "ModelCandidate",
    "ModelObservation",
    "ModelOutputError",
    "OutcomeBinding",
    "TraceStep",
    "compile_capability",
    "discover",
]
