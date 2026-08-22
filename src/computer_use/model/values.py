"""Typed value provenance (ValueRef) and observable checks (Condition).

These are the reusable leaf primitives of the capability artifact:

- ``ValueRef`` makes the source of every value written into a UI explicit
  (parameter / safe literal / secret / derived) so provenance is *created*,
  never inferred later from a coincidental raw scalar.
- ``Condition`` expresses an observable check reused by step postconditions,
  the capability success checkpoint, and business-outcome detectors.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ParameterRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["parameter"] = "parameter"
    name: str


class SafeLiteral(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["safe_literal"] = "safe_literal"
    value: str


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["secret"] = "secret"
    name: str


class DerivedValue(BaseModel):
    """A value computed deterministically from other ValueRefs.

    ``function`` is a *symbolic* identifier for a deterministic transform (e.g.
    ``"format_date"``). This data layer never executes it: there is no eval, no
    arbitrary function dispatch, and no transform engine. Resolving a derived
    value against an allowlisted registry is the runtime's responsibility.
    """

    model_config = ConfigDict(extra="forbid")
    source: Literal["derived"] = "derived"
    function: str
    args: list[ValueRef] = Field(default_factory=list)


ValueRef = Annotated[
    ParameterRef | SafeLiteral | SecretRef | DerivedValue,
    Field(discriminator="source"),
]

DerivedValue.model_rebuild()


class Heading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    name: str


class Condition(BaseModel):
    """An observable check. At least one matcher must be set (fail closed)."""

    model_config = ConfigDict(extra="forbid")
    text_present: str | None = None
    heading: Heading | None = None
    route_pattern: str | None = None
    output_present: str | None = None
    any_of: list[Condition] | None = None

    @model_validator(mode="after")
    def _require_one_matcher(self) -> Self:
        if not (
            self.text_present
            or self.heading
            or self.route_pattern
            or self.output_present
            or self.any_of
        ):
            raise ValueError("Condition requires at least one matcher")
        return self


Condition.model_rebuild()
