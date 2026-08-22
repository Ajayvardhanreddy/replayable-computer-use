"""Runtime result contracts: policy decisions and the public RunResult union.

The result contract is deliberately four distinct shapes so a calling agent
never has to parse a log line: a Success carries outputs, a BusinessOutcome is a
legitimate domain answer (not a crash), an Escalated hands off to a human, and a
Failure carries enough detail to debug.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import PolicyEffect


class PolicyDecision(BaseModel):
    """Result of a policy/allowlist check. The engine itself is a later phase."""

    model_config = ConfigDict(extra="forbid")
    effect: PolicyEffect
    reason: str
    rule: str | None = None


class _RunResultBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    model_calls: int = 0


class Success(_RunResultBase):
    status: Literal["success"] = "success"
    capability: str
    version: int
    outputs: dict[str, str] = Field(default_factory=dict)


class BusinessOutcome(_RunResultBase):
    status: Literal["business_outcome"] = "business_outcome"
    capability: str
    code: str
    outputs: dict[str, str] = Field(default_factory=dict)


class Escalated(_RunResultBase):
    status: Literal["escalated"] = "escalated"
    code: str
    step_id: str | None = None
    intervention_id: str | None = None


class Failure(_RunResultBase):
    status: Literal["failure"] = "failure"
    code: str
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


RunResult = Annotated[
    Success | BusinessOutcome | Escalated | Failure,
    Field(discriminator="status"),
]
