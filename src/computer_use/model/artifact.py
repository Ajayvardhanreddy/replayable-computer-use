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
from .values import Condition, DerivedValue, ParameterRef, ValueRef

SCHEMA_VERSION = "1.0"


class TableCellTarget(BaseModel):
    """Identify a table cell relationally, by stable identity rather than value.

    The cell is the one under ``column_header`` in the row containing
    ``row_contains``. Both are workflow constants (static selector metadata, not
    action values), so they are plain strings. This is the minimal baseline
    primitive for a table with a real header row; resilient/hostile-table
    resolution (missing headers, reordered or duplicate rows, parameterized row
    matching) is a resolver concern handled elsewhere.
    """

    model_config = ConfigDict(extra="forbid")
    row_contains: str
    column_header: str

    @field_validator("row_contains", "column_header")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("table_cell identifiers must be non-empty")
        return value


class TargetDescriptor(BaseModel):
    """Semantic identity of a control. Prefers accessibility over CSS.

    ``frame`` is context only; at least one of role/name/label/text or a
    ``table_cell`` relation is required so an empty descriptor fails closed
    rather than matching anything.
    """

    model_config = ConfigDict(extra="forbid")
    role: str | None = None
    name: str | None = None
    label: str | None = None
    text: str | None = None
    frame: str | None = None
    table_cell: TableCellTarget | None = None
    # Ordered deterministic fallbacks tried only when the primary resolves to zero
    # matches. Each fallback is itself a single-identity descriptor; fallbacks are
    # depth-1 (a fallback carries no fallbacks of its own). Ambiguity is never
    # dodged: a locator that matches more than once fails closed instead.
    fallbacks: list[TargetDescriptor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fallbacks_are_depth_one(self) -> Self:
        for fallback in self.fallbacks:
            if fallback.fallbacks:
                raise ValueError("locator fallbacks must not be nested (depth-1 only)")
        return self

    @model_validator(mode="after")
    def _single_identity_form(self) -> Self:
        # Exactly one non-conflicting semantic identity form (frame is orthogonal
        # context). Forms: role+name, label, text (role-qualified optional),
        # table_cell. Mixed forms are rejected so a descriptor is unambiguous.
        if self.table_cell is not None:
            if self.role or self.name or self.label or self.text:
                raise ValueError("table_cell target must not mix with role/name/label/text")
            return self
        if self.label is not None:
            if self.role or self.name or self.text:
                raise ValueError("label target must not mix with role/name/text")
            return self
        if self.name is not None:
            if self.role is None:
                raise ValueError("a name target requires a role (role+name form)")
            if self.text is not None:
                raise ValueError("name and text are conflicting identity forms")
            return self
        if self.text is not None:
            # text is a standalone form: the surface resolves it by text, so a role
            # qualifier here would be silently ignored (a contract mismatch).
            if self.role is not None:
                raise ValueError("a text target must not be qualified by a role")
            return self
        raise ValueError(
            "TargetDescriptor requires one identity form: role+name, label, text, or table_cell"
        )


TargetDescriptor.model_rebuild()  # resolve the self-referential `fallbacks` field


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

    @model_validator(mode="after")
    def _only_business_outcome(self) -> Self:
        # An authored step outcome is a business outcome. Recoverable conditions are
        # deterministic replay runtime behavior, and hard failures are a Failure, so
        # neither is expressible as a per-step authored outcome.
        if self.outcome_class is not OutcomeClass.BUSINESS_OUTCOME:
            raise ValueError("an authored step outcome must be a business_outcome")
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    action: Action
    target: TargetDescriptor | None = None
    risk: RiskClass
    postcondition: Condition | None = None
    outcomes: list[Outcome] = Field(default_factory=list)
    output: str | None = None
    # Present only on a consequential commit step: the discovered, embedded independent-
    # verification recipe. A single flat read-only sequence — never a nested workflow —
    # that re-derives the effect's view and observes it.
    verification: MutationVerification | None = None

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
        # A table_cell is a relational read; the current adapter resolves it for
        # extract only, never as a click/type target. Applies to every fallback too.
        if self.target is not None and action_type != "extract":
            for descriptor in (self.target, *self.target.fallbacks):
                if descriptor.table_cell is not None:
                    raise ValueError(
                        f"table_cell target is only valid for extract, not '{action_type}'"
                    )
        # A step with authored alternative outcomes must also define its normal
        # path, so the successful branch is explicit rather than implied.
        if self.outcomes and self.postcondition is None:
            raise ValueError("a step with outcomes must also have a normal-path postcondition")
        if self.verification is not None and self.risk is RiskClass.READ_ONLY:
            # A verification recipe belongs only on a consequential commit step.
            raise ValueError("verification is only valid on a consequential write step")
        return self


class MutationVerification(BaseModel):
    """The embedded, discovered recipe for independently confirming a consequential write.

    It is a normalized execution unit, not a transcript fragment: the ``navigate``
    steps are the read-only re-derivation the model performed after the commit (e.g.
    return to inquiry, re-query the member by ``ParameterRef``, reach the accounts
    view); ``page`` identifies that view so a baseline can be evaluated on it before
    dispatch; ``effect_present`` is the effect matcher whose truth on that view proves
    the write landed; ``extract`` optionally reads the declared output once present.

    Deliberately flat and non-recursive: exactly one consequential write maps to one
    ordered read-only sequence. A verification step may not itself carry mutation
    metadata or be anything but read-only, so this can never become a workflow tree.
    The artifact says where/how to verify; whether an *absent* effect is authoritative
    enough to be a definite non-commit is a trusted-runtime decision, never serialized.
    """

    model_config = ConfigDict(extra="forbid")
    navigate: list[Step]
    page: Condition
    effect_present: Condition
    extract: Step | None = None

    @model_validator(mode="after")
    def _flat_and_read_only(self) -> Self:
        recipe = [*self.navigate, *([self.extract] if self.extract is not None else [])]
        if not recipe:
            raise ValueError("verification must contain at least one read-only step")
        for member in recipe:
            if member.risk is not RiskClass.READ_ONLY:
                raise ValueError("every verification step must be read_only")
            if member.verification is not None:
                raise ValueError("a verification step cannot itself carry mutation metadata")
        if self.extract is not None and self.extract.action.type != "extract":
            raise ValueError("verification.extract must be an extract step")
        _forbid_matchers(self.page, _STEP_MATCHERS, "verification.page")
        _forbid_matchers(self.effect_present, _STEP_MATCHERS, "verification.effect_present")
        return self


# Step forward-references MutationVerification (embedded below it); resolve now that
# both are defined.
Step.model_rebuild()


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


# Matchers a step postcondition / outcome detector may use as a live condition.
# `output_present` is a success-checkpoint-only matcher (verified against extracted
# outputs, not the live surface), so it is excluded from step conditions.
_STEP_MATCHERS = frozenset({"text_present", "heading", "route_pattern", "any_of"})
_SUCCESS_MATCHERS = frozenset(
    {"text_present", "heading", "route_pattern", "any_of", "output_present"}
)


def _parameter_names(value: ValueRef) -> set[str]:
    """ParameterRef names a value references, recursively through DerivedValue.args."""
    if isinstance(value, ParameterRef):
        return {value.name}
    if isinstance(value, DerivedValue):
        names: set[str] = set()
        for arg in value.args:
            names |= _parameter_names(arg)
        return names
    return set()  # SafeLiteral / SecretRef carry no declared-input reference


def _top_level_matchers(condition: Condition) -> set[str]:
    used: set[str] = set()
    if condition.text_present is not None:
        used.add("text_present")
    if condition.heading is not None:
        used.add("heading")
    if condition.route_pattern is not None:
        used.add("route_pattern")
    if condition.output_present is not None:
        used.add("output_present")
    if condition.any_of is not None:
        used.add("any_of")
    return used


def _forbid_matchers(condition: Condition, allowed: frozenset[str], where: str) -> None:
    extra = _top_level_matchers(condition) - allowed
    if extra:
        raise ValueError(f"{where}: matcher(s) {sorted(extra)} are not accepted here")
    # any_of subconditions are live conditions; output_present is top-level only
    # (it is verified against extracted outputs, never as a live matcher).
    for sub in condition.any_of or []:
        _forbid_matchers(sub, _STEP_MATCHERS, f"{where} (any_of)")


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

    @model_validator(mode="after")
    def _semantic_consistency(self) -> Self:
        """Artifact-alone static validation, enforced on every construction/load.

        A hand-edited or externally produced artifact cannot bypass these
        invariants: every dynamic value has a declared-input provenance, every
        extracted output is declared, the success output is declared and actually
        produced, and step/outcome conditions use only replay-supported matchers.
        """
        input_names = set(self.inputs)
        output_names = set(self.outputs)
        produced: set[str] = set()
        for step in self.steps:
            # An embedded verification's read-only steps carry the same provenance
            # contract: their ParameterRefs must be declared inputs, and the terminal
            # extract is what produces the declared success output.
            action_steps = [step]
            if step.verification is not None:
                action_steps.extend(step.verification.navigate)
                if step.verification.extract is not None:
                    action_steps.append(step.verification.extract)
            for member in action_steps:
                action = member.action
                if isinstance(action, (TypeAction, SelectAction)):
                    for name in _parameter_names(action.value):
                        if name not in input_names:
                            raise ValueError(
                                f"step {member.id!r}: ParameterRef {name!r} is not a declared input"
                            )
                if isinstance(action, ExtractAction) and member.output is not None:
                    if member.output not in output_names:
                        raise ValueError(
                            f"step {member.id!r}: extract output {member.output!r} "
                            "is not a declared output"
                        )
                    produced.add(member.output)
            if step.postcondition is not None:
                _forbid_matchers(
                    step.postcondition, _STEP_MATCHERS, f"step {step.id!r} postcondition"
                )
            for outcome in step.outcomes:
                _forbid_matchers(
                    outcome.detector, _STEP_MATCHERS, f"step {step.id!r} outcome {outcome.code!r}"
                )
        _forbid_matchers(self.success_checkpoint, _SUCCESS_MATCHERS, "success_checkpoint")
        target_output = self.success_checkpoint.output_present
        if target_output is not None:
            if target_output not in output_names:
                raise ValueError(f"success output {target_output!r} is not a declared output")
            if target_output not in produced:
                raise ValueError(
                    f"success output {target_output!r} is never produced by an extract step"
                )
        return self

    @model_validator(mode="after")
    def _single_final_verification(self) -> Self:
        """A capability carries at most one consequential mutation-verification, and it must
        be the final step. The runtime dispatches the write and returns after independent
        verification, so a second verification step — or any step after it — would never
        execute; rejecting that topology at load keeps the artifact contract closed."""
        verified = [i for i, step in enumerate(self.steps) if step.verification is not None]
        if len(verified) > 1:
            raise ValueError("a capability may carry at most one mutation-verification step")
        if verified and verified[0] != len(self.steps) - 1:
            raise ValueError("a mutation-verification step must be the final step")
        return self
