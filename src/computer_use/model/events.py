"""Structured evidence event shape (the unit of JSONL observability).

Phase 1 defines only the *shape*. The redaction / sanitization boundary that
guarantees no raw secret or PII enters ``attributes`` is a later phase; this
type makes no safety claim on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

AttributeValue = str | int | bool | None


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str
    run_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    step_id: str | None = None
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
