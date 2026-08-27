"""Discovery-side same-session handoff against LegacyCore, driven deterministically.

A stub model (no API key, no credits) makes the decisions so the whole mechanism is
proven end to end on a real browser: the model reaches a flagged account it cannot
verify, proposes ``request_human`` from its normal action schema, discovery pauses on
the SAME live session, a human enters the employee verification code and continues,
control returns to automation, discovery re-observes and the model finishes the goal,
and the capability compiles. The real Anthropic version of this is opt-in
(``test_discovery_handoff_live``); this deterministic run guards the plumbing.
"""

from pathlib import Path

from computer_use.discovery import GoalSpec, OutcomeBinding, compile_capability, discover
from computer_use.discovery.model import GoalContext, ModelObservation
from computer_use.execution import ControlLease, TrustedKernel, ValueResolver, replay
from computer_use.handoff import OperatorController, TypeControl
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedAction,
    ProposedActionType,
    Sensitivity,
    TargetDescriptor,
)
from computer_use.model import (
    Condition as Cond,
)
from computer_use.observability import EvidenceStore
from computer_use.safety import EnvSecretProvider, NavigationPolicy, Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
_SAFE_CLICKS = frozenset({"Search"})


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
        business_outcomes=[
            OutcomeBinding(
                action=ProposedActionType.CLICK,
                target=TargetDescriptor(role="button", name="Search"),
                outcome=Outcome(
                    code="MEMBER_NOT_FOUND",
                    outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                    detector=Cond(text_present="Member record not found"),
                ),
            )
        ],
    )


class _StubModel:
    """Decides purely from the current observation — no scripted step sequence.

    It types the member number, searches, and — when it reaches a control it cannot
    satisfy (a verification code it was never given) with no other progress available —
    proposes request_human. After the block clears it extracts the balance.
    """

    provider = "stub"
    model_id = "stub-1"

    def __init__(self) -> None:
        self.calls = 0
        self.observations: list[ModelObservation] = []
        self.requested_human = False

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        self.calls += 1
        self.observations.append(observation)
        cands = observation.candidates

        def find(role: str, name: str) -> object | None:
            return next((c for c in cands if c.role == role and c.name == name), None)

        member_field = find("textbox", "Member Number")
        if member_field is not None and not member_field.filled:  # type: ignore[attr-defined]
            return ProposedAction(
                action=ProposedActionType.TYPE,
                candidate_id=member_field.id,  # type: ignore[attr-defined]
                value=ParameterRef(name="member_number"),
            )
        search = find("button", "Search")
        if search is not None:
            return ProposedAction(
                action=ProposedActionType.CLICK,
                candidate_id=search.id,  # type: ignore[attr-defined]
                expected_effect="member profile or a verification step",
            )
        # A required control the automation cannot satisfy and no other progress: escalate.
        if find("textbox", "Employee Verification Code") is not None:
            self.requested_human = True
            return ProposedAction(
                action=ProposedActionType.REQUEST_HUMAN,
                reason="verification requires an employee credential that was not provided",
            )
        if "savings_balance" not in observation.obtained_outputs:
            cell = next(
                (
                    c
                    for c in cands
                    if c.role == "cell" and c.row and "Share Savings" in c.row
                    and c.column == "Current Balance"
                ),
                None,
            )
            if cell is not None:
                return ProposedAction(
                    action=ProposedActionType.EXTRACT,
                    candidate_id=cell.id,
                    output="savings_balance",
                )
        return ProposedAction(action=ProposedActionType.DECLARE_SUCCESS)


async def test_discovery_side_handoff_completes(
    legacy_core_url: str, nav_policy: NavigationPolicy, tmp_path: Path
) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    store = EvidenceStore(tmp_path / "trace.jsonl")
    lease = ControlLease()
    model = _StubModel()
    took_over: dict[str, int] = {}

    async def human_handler(operator: OperatorController, reason: str | None = None) -> bool:
        # An authorized employee takes the SAME live session, enters the code, continues.
        took_over["obs_before"] = len(model.observations)
        operator.take_control()
        await operator.perform(
            TypeControl(
                TargetDescriptor(role="textbox", name="Employee Verification Code"),
                "4729",
                submit=True,
            )
        )
        operator.release_to_automation()
        return True

    try:
        await surface.goto(f"{legacy_core_url}/?scenario=verification_required")
        session_id = surface.session_id
        context = surface.context
        page = surface.page
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver({"member_number": "12345"}, EnvSecretProvider()),
            lease=lease,
        )
        outcome = await discover(
            model, surface, kernel, _spec(), legacy_core_url,
            nav_policy=nav_policy, evidence=store, lease=lease, on_human_request=human_handler,
        )
        # The whole handoff used one live session — never reconstructed (assert before
        # the surface is closed).
        assert surface.session_id == session_id
        assert surface.context is context
        assert surface.page is page
        assert surface.is_live
    finally:
        await surface.close()

    # The model genuinely escalated, and discovery still completed the goal.
    assert model.requested_human is True
    assert outcome.stop_reason == "GOAL_REACHED"
    assert outcome.model_calls >= 4

    # A fresh observation and a new model decision happened after the handoff.
    assert len(model.observations) > took_over["obs_before"]

    # The capability compiles from the resumed run.
    capability = compile_capability(outcome.trace, _spec())
    assert any(step.output == "savings_balance" for step in capability.steps)

    # Deterministic replay of the compiled capability is model-free.
    result = await replay(
        capability, {"member_number": "12345"}, legacy_core_url,
        nav_policy=nav_policy, safe_clicks=_SAFE_CLICKS,
    )
    assert result.model_calls == 0

    # Audit: intervention + control transfers + the human action, code redacted.
    events = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"intervention_raised"' in events
    assert '"HUMAN_REQUESTED"' in events
    assert '"human_action"' in events
    assert '"<redacted>"' in events
    assert "4729" not in events
