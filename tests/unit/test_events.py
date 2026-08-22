from datetime import UTC, datetime

from computer_use.model import EvidenceEvent


def test_evidence_event_round_trips() -> None:
    event = EvidenceEvent(
        event="step_completed",
        run_id="run_1",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        step_id="submit_lookup",
        attributes={"resolver_strategy": "role_name", "checkpoint": "passed", "model_calls": 0},
    )
    reloaded = EvidenceEvent.model_validate_json(event.model_dump_json())
    assert reloaded == event
    assert reloaded.attributes["model_calls"] == 0


def test_evidence_event_defaults_timestamp() -> None:
    event = EvidenceEvent(event="run_started", run_id="run_1")
    assert event.ts.tzinfo is not None
