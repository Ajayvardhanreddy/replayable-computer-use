"""Core typed contracts for the computer-use system.

Pure data contracts with serialization and fail-closed validation. No
execution, surface, discovery, or replay logic lives in this layer.
"""

from __future__ import annotations

from .artifact import (
    SCHEMA_VERSION,
    Action,
    Capability,
    CapabilityTarget,
    ClickAction,
    DeclareSuccessAction,
    ExtractAction,
    InputSpec,
    ObserveAction,
    Outcome,
    OutputSpec,
    RequestHumanAction,
    ScrollAction,
    SelectAction,
    Step,
    TargetDescriptor,
    TypeAction,
)
from .enums import OutcomeClass, ParamType, PolicyEffect, RiskClass, Sensitivity
from .events import EvidenceEvent
from .results import (
    BusinessOutcome,
    Escalated,
    Failure,
    PolicyDecision,
    RunResult,
    Success,
)
from .values import (
    Condition,
    DerivedValue,
    Heading,
    ParameterRef,
    SafeLiteral,
    SecretRef,
    ValueRef,
)

__all__ = [
    "SCHEMA_VERSION",
    "Action",
    "BusinessOutcome",
    "Capability",
    "CapabilityTarget",
    "ClickAction",
    "Condition",
    "DeclareSuccessAction",
    "DerivedValue",
    "Escalated",
    "EvidenceEvent",
    "ExtractAction",
    "Failure",
    "Heading",
    "InputSpec",
    "ObserveAction",
    "Outcome",
    "OutcomeClass",
    "OutputSpec",
    "ParamType",
    "ParameterRef",
    "PolicyDecision",
    "PolicyEffect",
    "RequestHumanAction",
    "RiskClass",
    "RunResult",
    "SafeLiteral",
    "ScrollAction",
    "SecretRef",
    "SelectAction",
    "Sensitivity",
    "Step",
    "Success",
    "TargetDescriptor",
    "TypeAction",
    "ValueRef",
]
