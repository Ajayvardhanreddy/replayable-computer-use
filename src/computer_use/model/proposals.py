"""The untrusted model -> kernel boundary: a proposed action.

Deliberately carries NO risk classification. The model cannot authorize itself
by declaring an action safe; the trusted kernel derives risk. Action values are
typed ValueRefs, so a raw scalar can never be smuggled in as an action value.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .values import ValueRef


class ProposedActionType(StrEnum):
    OBSERVE = "observe"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    EXTRACT = "extract"
    DECLARE_SUCCESS = "declare_success"
    REQUEST_HUMAN = "request_human"


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ProposedActionType
    candidate_id: str | None = None
    value: ValueRef | None = None
    output: str | None = None
    reason: str | None = None
