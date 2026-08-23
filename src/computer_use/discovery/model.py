"""The discovery model interface and the minimized observation it receives.

The model is a pure adapter: it maps (goal context, observation) -> ProposedAction
and never touches the browser. The observation is deliberately minimized — inputs
are symbolic (name/type/sensitivity, never the raw value) and candidate cells omit
their text so financial values are not sent to the model.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from computer_use.model import ProposedAction


class ModelOutputError(Exception):
    """The model produced output that could not be parsed into a ProposedAction."""


class InputInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str
    sensitivity: str
    required: bool


class GoalContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    inputs: list[InputInfo]
    outputs: list[str]


class ModelCandidate(BaseModel):
    """A candidate as shown to the model: structural identity only, no raw text/value."""

    model_config = ConfigDict(extra="forbid")
    id: str
    role: str
    name: str | None = None
    frame: str | None = None
    row: str | None = None
    column: str | None = None
    filled: bool | None = None


class ModelObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str
    candidates: list[ModelCandidate]
    # Actions already completed this run, so the (stateless-per-call) model does not repeat.
    actions_taken: list[str] = Field(default_factory=list)
    last_error: str | None = None
    steps_remaining: int


class DiscoveryModel(Protocol):
    provider: str
    model_id: str

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction: ...
