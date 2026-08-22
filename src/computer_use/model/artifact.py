"""The capability artifact: a typed, versioned, reviewable execution IR.

A capability is not a click log. It is a small typed contract a human reviewer
and a calling agent can both understand: what it needs (inputs), what it returns
(outputs), the ordered steps with semantic targets and risk, per-step observable
postconditions and business outcomes, and a success checkpoint.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import OutcomeClass, ParamType, RiskClass, Sensitivity
from .values import Condition, ValueRef

SCHEMA_VERSION = "1.0"


class TargetDescriptor(BaseModel):
    """Semantic identity of a control. Prefers accessibility over CSS.

    ``frame`` is context only; at least one of role/name/label/text is required
    so an empty descriptor fails closed rather than matching anything.
    """

    model_config = ConfigDict(extra="forbid")
    role: str | None = None
    name: str | None = None
    label: str | None = None
    text: str | None = None
    frame: str | None = None

    @model_validator(mode="after")
    def _require_identity(self) -> Self:
        if not (self.role or self.name or self.label or self.text):
            raise ValueError("TargetDescriptor requires at least one of role/name/label/text")
        return self


class ObserveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["observe"] = "observe"


class ClickAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click"] = "click"


class TypeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["type"] = "type"
    value: ValueRef


class SelectAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["select"] = "select"
    value: ValueRef


class ScrollAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["scroll"] = "scroll"


class ExtractAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["extract"] = "extract"


class DeclareSuccessAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["declare_success"] = "declare_success"


class RequestHumanAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["request_human"] = "request_human"
    reason: str | None = None


Action = Annotated[
    ObserveAction
    | ClickAction
    | TypeAction
    | SelectAction
    | ScrollAction
    | ExtractAction
    | DeclareSuccessAction
    | RequestHumanAction,
    Field(discriminator="type"),
]

_TARGET_REQUIRED = frozenset({"click", "type", "select", "extract"})


class Outcome(BaseModel):
    """A declared alternative outcome for a step (e.g. MEMBER_NOT_FOUND)."""

    model_config = ConfigDict(extra="forbid")
    code: str
    outcome_class: OutcomeClass
    detector: Condition


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    action: Action
    target: TargetDescriptor | None = None
    risk: RiskClass
    postcondition: Condition | None = None
    outcomes: list[Outcome] = Field(default_factory=list)
    output: str | None = None

    @field_validator("id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("id must be non-empty")
        return value

    @model_validator(mode="after")
    def _structural_rules(self) -> Self:
        action_type = self.action.type
        if action_type in _TARGET_REQUIRED and self.target is None:
            raise ValueError(f"action '{action_type}' requires a target")
        if action_type == "extract" and not self.output:
            raise ValueError("extract action requires 'output'")
        if action_type != "extract" and self.output is not None:
            raise ValueError("'output' is only valid for an extract action")
        return self


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ParamType
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.NONE


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ParamType
    sensitivity: Sensitivity = Sensitivity.NONE
    currency: str | None = None


class CapabilityTarget(BaseModel):
    """Vendor/product identity of the app a capability runs against.

    Grounds the multi-tenant reuse seam. Per-tenant/version bindings and route
    overrides are not modeled here; the resolution layer applies them as
    overrides onto this base identity.
    """

    model_config = ConfigDict(extra="forbid")
    vendor: str
    application_family: str


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    id: str
    version: int = Field(ge=1)
    target: CapabilityTarget
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: list[Step]
    success_checkpoint: Condition

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value!r}; expected {SCHEMA_VERSION!r}"
            )
        return value

    @field_validator("steps")
    @classmethod
    def _non_empty_steps(cls, value: list[Step]) -> list[Step]:
        if not value:
            raise ValueError("capability requires at least one step")
        return value

    @field_validator("id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("id must be non-empty")
        return value

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Self:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique within a capability")
        return self
