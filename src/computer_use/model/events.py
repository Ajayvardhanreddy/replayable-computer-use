"""Structured evidence event shape (the unit of JSONL observability).

This type defines only the *shape* of an evidence record. The redaction /
sanitization boundary that guarantees no raw secret or PII enters
``attributes`` is enforced by the evidence layer, not by this type; on its own
it makes no safety claim.
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
