from pathlib import Path

from computer_use.discovery import (
    GoalContext,
    GoalSpec,
    ModelObservation,
    compile_capability,
    discover,
)
from computer_use.execution import TrustedKernel, ValueResolver, replay
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedAction,
    ProposedActionType,
    Sensitivity,
    Success,
)
from computer_use.observability import EvidenceStore
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
_SAFE_CLICKS = frozenset({"Search"})


class FakeDiscoveryModel:
    """A scripted stand-in for a real model: drives Capability A from page state."""

    provider = "fake"
    model_id = "fake-scripted"

    def __init__(self) -> None:
        self._typed = False
        self._extracted = False

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        def find(**attrs: str | None) -> str | None:
            for candidate in observation.candidates:
                if all(getattr(candidate, key) == value for key, value in attrs.items()):
                    return candidate.id
            return None

        balance = find(role="cell", row="Share Savings", column="Current Balance")
        if balance is not None:
            if not self._extracted:
                self._extracted = True
                return ProposedAction(
                    action=ProposedActionType.EXTRACT,
                    candidate_id=balance,
                    output="savings_balance",
                )
            return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)

        member = find(role="textbox", name="Member Number")
        search = find(role="button", name="Search")
        if not self._typed and member is not None:
            self._typed = True
            return ProposedAction(
                action=ProposedActionType.TYPE,
                candidate_id=member,
                value=ParameterRef(name="member_number"),
            )
        if search is not None:
            return ProposedAction(action=ProposedActionType.CLICK, candidate_id=search)
        return ProposedAction(action=ProposedActionType.REQUEST_HUMAN, reason="stuck")


def _spec() -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal="Look up this member and return their savings balance",
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        success_output="savings_balance",
    )


async def test_fake_model_discovery_compiles_and_replays(
    legacy_core_url: str, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "discovery.jsonl"
    store = EvidenceStore(evidence_path)
    spec = _spec()

    surface = PlaywrightSurface()
    await surface.start()
    try:
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver({"member_number": "12345"}),
        )
        outcome = await discover(
            FakeDiscoveryModel(), surface, kernel, spec, legacy_core_url, evidence=store
        )
    finally:
        await surface.close()

    assert outcome.stop_reason == "GOAL_REACHED"
    assert outcome.model_calls >= 3
    capability = compile_capability(outcome.trace, spec)

    # Evidence reproducibility + safety.
    evidence_text = evidence_path.read_text()
    assert "fake-scripted" in evidence_text
    assert "GOAL_REACHED" in evidence_text
    assert "<param:member_number>" in evidence_text
    assert "12345" not in evidence_text  # raw invocation value never persisted
    assert "8,421.31" not in evidence_text and "8421.31" not in evidence_text  # no financial value

    # Deterministic replay of the discovered artifact with a different member, no model.
    result = await replay(
        capability, {"member_number": "54321"}, legacy_core_url, safe_clicks=_SAFE_CLICKS
    )
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "312.45"
    assert result.model_calls == 0
