"""Redaction-before-persistence (C25-C30): the write boundary sanitizes, the raw
goal is never persisted, sensitive outputs are masked, and failure evidence falls
closed to sanitized structural evidence with no screenshot."""

import json
from pathlib import Path

from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    EvidenceEvent,
    ExtractAction,
    Failure,
    FailureCode,
    Heading,
    InputSpec,
    OutputSpec,
    ParameterRef,
    ParamType,
    RiskClass,
    Sensitivity,
    Step,
    Success,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)
from computer_use.observability import (
    EvidenceCollector,
    EvidencePolicy,
    EvidenceStore,
    ScreenshotPolicy,
    discovery_started_event,
    persistable_result,
)
from computer_use.surface import StructuralSnapshot


def _capability() -> Capability:
    return Capability(
        id="member.lookup_savings_balance",
        version=1,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        steps=[
            Step(
                id="s1",
                action=TypeAction(value=ParameterRef(name="member_number")),
                target=TargetDescriptor(role="textbox", name="Member Number"),
                risk=RiskClass.READ_ONLY,
            ),
            Step(
                id="s2",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Search"),
                risk=RiskClass.READ_ONLY,
                postcondition=Condition(heading=Heading(role="heading", name="Member Profile")),
            ),
            Step(
                id="s3",
                action=ExtractAction(),
                target=TargetDescriptor(
                    table_cell=TableCellTarget(
                        row_contains="Share Savings", column_header="Current Balance"
                    )
                ),
                risk=RiskClass.READ_ONLY,
                output="savings_balance",
            ),
        ],
        success_checkpoint=Condition(output_present="savings_balance"),
    )


def test_write_boundary_drops_non_allowlisted_attributes(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "trace.jsonl")
    # a caller mistakenly attaches a raw goal (with PII) to the event
    store.write(
        EvidenceEvent(
            event="discovery_started",
            run_id="r",
            attributes={
                "provider": "anthropic",
                "capability_id": "cap",
                "goal": "look up CANARY_PII_JohnDoe SSN 111-22-3333",
            },
        )
    )
    text = (tmp_path / "trace.jsonl").read_text()
    assert "CANARY_PII" not in text  # dropped at the write boundary
    assert "anthropic" in text  # allowlisted attribute kept


def test_unknown_event_type_persists_no_attributes(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "t.jsonl")
    store.write(EvidenceEvent(event="mystery", run_id="r", attributes={"raw": "CANARY_LEAK"}))
    assert "CANARY_LEAK" not in (tmp_path / "t.jsonl").read_text()


def test_discovery_started_persists_no_raw_goal(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "t.jsonl")
    store.write(discovery_started_event("r", "anthropic", "m", "cap", "raw CANARY_GOAL text"))
    text = (tmp_path / "t.jsonl").read_text()
    assert "CANARY_GOAL" not in text
    assert "goal_present" in text


def test_persisted_result_masks_financial_output_but_result_keeps_it() -> None:
    cap = _capability()
    result = Success(
        run_id="r", capability=cap.id, version=1, outputs={"savings_balance": "CANARY_8421"}
    )
    masked = persistable_result(result, cap)
    assert masked["outputs"] == {"savings_balance": "<financial>"}
    assert "CANARY_8421" not in json.dumps(masked)
    # the in-memory result returned to the caller keeps the raw deliverable value
    assert result.outputs["savings_balance"] == "CANARY_8421"


def test_persisted_failure_drops_free_text_expected_and_observed() -> None:
    # A failure's expected/observed are free text that can carry a raw value; the
    # persisted result keeps only the stable structural code + step id.
    result = Failure(
        run_id="r",
        code=FailureCode.CHECKPOINT_FAILED,
        step_id="s2",
        expected="CANARY_EXPECTED_text",
        observed="CANARY_OBSERVED /workspace/member/12345",
    )
    masked = persistable_result(result, _capability())
    text = json.dumps(masked)
    assert "CANARY_EXPECTED" not in text
    assert "CANARY_OBSERVED" not in text
    assert "12345" not in text
    assert masked["code"] == "CHECKPOINT_FAILED"  # stable structural signal kept
    assert masked["step_id"] == "s2"


def test_evidence_policy_fails_closed_to_structural_only() -> None:
    policy = EvidencePolicy(mask_known_routes=frozenset({"/workspace/inquiry"}))
    assert policy.for_route("/workspace/member/12345") is ScreenshotPolicy.STRUCTURAL_ONLY
    assert policy.for_route("/workspace/inquiry") is ScreenshotPolicy.MASK_KNOWN
    assert policy.for_route("/anything/unknown") is ScreenshotPolicy.STRUCTURAL_ONLY


class _CaptureSurface:
    def __init__(self, route: str, landmarks: list[str]) -> None:
        self._route = route
        self._landmarks = landmarks

    async def capture(self) -> StructuralSnapshot:
        return StructuralSnapshot(route=self._route, frames=["main"], landmarks=self._landmarks)


async def test_failure_evidence_refuses_screenshot_and_keeps_structural() -> None:
    # a member-profile page could contain unknown PII; the collector refuses a
    # screenshot and persists sanitized structural evidence instead.
    surface = _CaptureSurface("/workspace/member/12345", ["Member Profile"])
    collector = EvidenceCollector(EvidencePolicy())
    evidence = await collector.collect_failure_evidence(surface, "/workspace/member/12345")
    assert evidence.policy is ScreenshotPolicy.STRUCTURAL_ONLY  # explicit fallback decision
    assert evidence.screenshot is None  # screenshot persistence refused
    assert evidence.landmarks == ["Member Profile"]  # structural evidence WAS collected
    assert "12345" not in " ".join(evidence.landmarks)  # no raw record value
