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
    MutationVerification,
    ObserveAction,
    Outcome,
    OutputSpec,
    RequestHumanAction,
    ScrollAction,
    SelectAction,
    Step,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)
from .enums import (
    ControlOwner,
    EffectState,
    OutcomeClass,
    ParamType,
    PolicyEffect,
    RiskClass,
    Sensitivity,
)
from .events import EvidenceEvent
from .proposals import ProposedAction, ProposedActionType
from .results import (
    BusinessOutcome,
    Escalated,
    Failure,
    FailureCode,
    PolicyDecision,
    RunResult,
    Success,
)
from .values import (
    Condition,
    DerivedValue,
    Heading,
    ParameterRef,
    ReadBack,
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
    "ControlOwner",
    "DeclareSuccessAction",
    "DerivedValue",
    "EffectState",
    "Escalated",
    "EvidenceEvent",
    "ExtractAction",
    "Failure",
    "FailureCode",
    "Heading",
    "InputSpec",
    "ObserveAction",
    "Outcome",
    "OutcomeClass",
    "OutputSpec",
    "MutationVerification",
    "ParamType",
    "ParameterRef",
    "PolicyDecision",
    "PolicyEffect",
    "ProposedAction",
    "ProposedActionType",
    "ReadBack",
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
    "TableCellTarget",
    "TargetDescriptor",
    "TypeAction",
    "ValueRef",
]
