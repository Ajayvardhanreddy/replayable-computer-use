"""Same-session human-in-the-loop handoff, exercised deterministically.

A scripted fake surface drives every branch without a browser and proves
model_calls stays 0: an unhandled blocking dialog pauses automation before the
next step (C31/C32), the kernel refuses automation actions while a human owns the
session and rejects stale-epoch work (C34, stale-epoch), human actions are audited
with values redacted (C35), resume reconciles against a real checkpoint rather
than blindly advancing (C36), and the whole path completes after handback (C37).
"""

import json
from pathlib import Path
from urllib.parse import urlparse

from computer_use.execution import (
    ControlLease,
    KernelRejection,
    RejectionCode,
    ReplaySession,
    TrustedKernel,
    ValueResolver,
)
from computer_use.handoff import (
    ClickControl,
    InterventionReason,
    OperatorController,
    OperatorError,
    OperatorScopeError,
    TypeControl,
)
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    ControlOwner,
    Escalated,
    ExtractAction,
    Failure,
    Heading,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedActionType,
    RiskClass,
    Sensitivity,
    Step,
    Success,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
)
from computer_use.observability import EvidenceStore
from computer_use.safety import NavigationPolicy, Policy, RiskClassifier
from computer_use.surface import StructuralSnapshot as Snapshot

_SAFE = frozenset({"Search"})
_ROUTES = frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"})
_NAV = NavigationPolicy(allowed_origins=frozenset({"http://legacy"}), allowed_routes=_ROUTES)
_MEMBER_PATH = "/workspace/member/CANARY_PII_123"


class _HandoffSurface:
    """Scripted surface: clicking Search lands on a profile guarded by a modal;
    a human click on Acknowledge clears the modal (optionally to a different page)."""

    def __init__(self, *, ack_heading: str = "Member Profile") -> None:
        self.heading: str | None = None
        self.dialog = False
        self.url = "http://legacy/"
        self.route = "/"
        self._ack_heading = ack_heading
        self.clicks: list[str | None] = []
        self.types: list[tuple[str | None, str]] = []
        self.extracts: list[TargetDescriptor] = []
        self.closed = False
        self.session_id = "fake-session"

    async def start(self) -> None: ...

    async def goto(self, url: str) -> None:
        self.url = url
        self.route = urlparse(url).path

    async def wait_settled(self) -> None: ...

    async def count(self, target: TargetDescriptor) -> int:
        return 1

    async def click(self, target: TargetDescriptor) -> None:
        self.clicks.append(target.name)
        if target.name == "Search":
            self.heading = "Member Profile"
            self.dialog = True
            self.url = f"http://legacy{_MEMBER_PATH}"
            self.route = _MEMBER_PATH
        elif target.name == "Acknowledge":
            self.dialog = False
            self.heading = self._ack_heading

    async def type_text(
        self, target: TargetDescriptor, text: str, *, submit: bool = False
    ) -> None:
        self.types.append((target.name, text))

    async def extract(self, target: TargetDescriptor) -> str:
        self.extracts.append(target)
        return "312.45"

    async def has_text(self, text: str) -> bool:
        return False

    async def has_heading(self, name: str) -> bool:
        return name == self.heading

    async def has_blocking_dialog(self) -> bool:
        return self.dialog

    async def current_route(self) -> str:
        return self.route

    async def current_url(self) -> str:
        return self.url

    async def primary_heading(self) -> str | None:
        return self.heading

    async def capture(self) -> Snapshot:
        landmarks = [self.heading] if self.heading else []
        return Snapshot(route=self.route, frames=["main"], landmarks=landmarks)

    async def close(self) -> None:
        self.closed = True


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
                outcomes=[
                    Outcome(
                        code="MEMBER_NOT_FOUND",
                        outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                        detector=Condition(text_present="Member record not found"),
                    )
                ],
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


def _session(surface: _HandoffSurface, *, resolve_timeout_ms: int = 5000) -> ReplaySession:
    return ReplaySession(
        _capability(),
        {"member_number": "CANARY_PII_123"},
        "http://legacy",
        nav_policy=_NAV,
        safe_clicks=_SAFE,
        surface=surface,
        resolve_timeout_ms=resolve_timeout_ms,
    )


# --- C31 / C32: detect, pause before the next step, raise a sanitized request ---


async def test_dialog_pauses_before_next_step_and_is_escalated() -> None:
    surface = _HandoffSurface()
    session = _session(surface)
    assert await session.start() is None
    result = await session.advance()
    assert isinstance(result, Escalated)
    assert result.code == "UNKNOWN_DIALOG"
    assert result.step_id == "s3"
    assert result.model_calls == 0
    # Paused *before* the extract executed, and the surface stays open.
    assert surface.extracts == []
    assert surface.closed is False
    assert session.pending is not None and session.pending.step_id == "s3"


async def test_intervention_request_is_sanitized() -> None:
    surface = _HandoffSurface()
    session = _session(surface)
    await session.start()
    await session.advance()
    operator = OperatorController(session)
    request = await operator.raise_intervention()
    assert request.reason is InterventionReason.UNKNOWN_DIALOG
    assert request.step_id == "s3"
    assert request.control_owner is ControlOwner.AUTOMATION
    # Route is the structural pattern, never the concrete member path.
    assert request.route == "/workspace/member/:member_number"
    assert request.evidence.screenshot is None
    # The whole request is free of the canary member id / raw values.
    assert "CANARY_PII_123" not in request.model_dump_json()


# --- C34: automation cannot act while a human owns the session ---


class _KernelFake:
    def __init__(self) -> None:
        self.counts = 0
        self.clicks = 0
        self.types = 0

    async def count(self, target: TargetDescriptor) -> int:
        self.counts += 1
        return 1

    async def click(self, target: TargetDescriptor) -> None:
        self.clicks += 1

    async def type_text(self, target: TargetDescriptor, text: str) -> None:
        self.types += 1


def _type_step() -> Step:
    return Step(
        id="t1",
        action=TypeAction(value=ParameterRef(name="x")),
        target=TargetDescriptor(role="textbox", name="Field"),
        risk=RiskClass.READ_ONLY,
    )


def _kernel(surface: object, lease: ControlLease) -> TrustedKernel:
    return TrustedKernel(
        surface,  # type: ignore[arg-type]
        Policy(allowed_actions=frozenset({ProposedActionType.TYPE})),
        RiskClassifier(),
        ValueResolver({"x": "value"}),
        lease=lease,
    )


async def test_automation_cannot_act_while_human_owns() -> None:
    surface = _KernelFake()
    lease = ControlLease()
    lease.to_human()  # human takes control
    kernel = _kernel(surface, lease)
    try:
        await kernel.execute_step(_type_step(), epoch=lease.epoch)
        raise AssertionError("expected CONTROL_NOT_OWNED")
    except KernelRejection as rejection:
        assert rejection.code is RejectionCode.CONTROL_NOT_OWNED
    # The surface was never touched — not even target resolution.
    assert surface.counts == 0 and surface.types == 0 and surface.clicks == 0


async def test_stale_epoch_work_is_rejected() -> None:
    surface = _KernelFake()
    lease = ControlLease()  # automation, epoch 0
    captured = lease.epoch
    lease.to_human()  # epoch 1
    lease.to_automation()  # epoch 2, automation owns again
    kernel = _kernel(surface, lease)
    try:
        await kernel.execute_step(_type_step(), epoch=captured)  # stale epoch 0
        raise AssertionError("expected stale-epoch rejection")
    except KernelRejection as rejection:
        assert rejection.code is RejectionCode.CONTROL_NOT_OWNED
    assert surface.types == 0
    # Current epoch is accepted.
    execution = await kernel.execute_step(_type_step(), epoch=lease.epoch)
    assert execution.action.value == "type"
    assert surface.types == 1


# --- C35: human action metadata is recorded safely ---


async def test_human_action_audit_redacts_value(tmp_path: Path) -> None:
    surface = _HandoffSurface()
    session = _session(surface)
    await session.start()
    await session.advance()
    evidence = EvidenceStore(tmp_path / "handoff.jsonl")
    operator = OperatorController(session, evidence=evidence, operator_id="op-7")
    operator.take_control()
    await operator.perform(
        TypeControl(TargetDescriptor(role="textbox", name="Note"), "CANARY_SECRET_XYZ")
    )
    lines = (tmp_path / "handoff.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    transfer = next(e for e in events if e["event"] == "control_transferred")
    action = next(e for e in events if e["event"] == "human_action")
    assert transfer["attributes"]["to_owner"] == "human"
    assert transfer["attributes"]["operator_id"] == "op-7"
    assert action["attributes"]["action"] == "type"
    assert action["attributes"]["target"] == "textbox:Note"
    assert action["attributes"]["value"] == "<redacted>"
    assert action["attributes"]["epoch"] >= 1
    # The raw human-entered value never reaches persisted evidence.
    assert "CANARY_SECRET_XYZ" not in (tmp_path / "handoff.jsonl").read_text(encoding="utf-8")


# --- C36: resume reconciles rather than blindly advancing cursor + 1 ---


async def test_resume_reconciles_and_completes() -> None:
    surface = _HandoffSurface(ack_heading="Member Profile")
    session = _session(surface)
    await session.start()
    await session.advance()  # paused at s3
    operator = OperatorController(session)
    operator.take_control()
    await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
    result = await operator.resume()
    assert isinstance(result, Success)
    assert result.outputs["savings_balance"] == "312.45"
    assert result.model_calls == 0
    assert surface.extracts != []  # the pending step ran only after reconciliation


async def test_blind_increment_would_be_wrong() -> None:
    # The human resolves the dialog but leaves the page elsewhere. Blindly running
    # the pending extract step would return a bogus balance; reconciliation instead
    # sees the established "Member Profile" checkpoint no longer holds and fails closed.
    surface = _HandoffSurface(ack_heading="Member Inquiry")
    session = _session(surface, resolve_timeout_ms=200)
    await session.start()
    await session.advance()
    operator = OperatorController(session)
    operator.take_control()
    await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
    result = await operator.resume()
    assert isinstance(result, Failure)
    assert result.step_id == "s2"  # the checkpoint that no longer holds
    assert surface.extracts == []  # never blindly extracted on the wrong page


async def test_resume_refuses_while_dialog_remains() -> None:
    # The human signals resume without clearing the modal: stay paused, do not resume,
    # and the operator keeps control so they can resolve it and resume again.
    surface = _HandoffSurface()
    session = _session(surface, resolve_timeout_ms=200)
    await session.start()
    await session.advance()
    operator = OperatorController(session)
    operator.take_control()
    result = await operator.resume()  # dialog still present
    assert isinstance(result, Escalated)
    assert result.code == "UNKNOWN_DIALOG"
    assert surface.extracts == []
    assert session.lease.owner is ControlOwner.HUMAN  # control retained
    # After actually resolving it, a second resume completes.
    await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
    final = await operator.resume()
    assert isinstance(final, Success)
    assert final.outputs["savings_balance"] == "312.45"
    assert session.lease.owner is ControlOwner.AUTOMATION


# --- C37: full path completes after handback, model-free ---


async def test_full_handback_path_is_model_free() -> None:
    surface = _HandoffSurface()
    session = _session(surface)
    await session.start()
    paused = await session.advance()
    assert isinstance(paused, Escalated)
    operator = OperatorController(session)
    request = await operator.raise_intervention()
    assert request.reason is InterventionReason.UNKNOWN_DIALOG
    operator.take_control()
    assert session.lease.owner is ControlOwner.HUMAN
    await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
    final = await operator.resume()
    assert session.lease.owner is ControlOwner.AUTOMATION
    assert isinstance(final, Success)
    assert final.outputs["savings_balance"] == "312.45"
    assert final.model_calls == 0
    # One live session throughout the handoff — never reconstructed.
    assert session.surface is surface
    assert surface.closed is False


# --- navigation and ownership safety preserved through human control ---


async def test_operator_must_hold_control_to_act() -> None:
    surface = _HandoffSurface()
    session = _session(surface)
    await session.start()
    await session.advance()
    operator = OperatorController(session)
    # No take_control() yet: the human does not own the session.
    try:
        await operator.perform(ClickControl(TargetDescriptor(role="link", name="Acknowledge")))
        raise AssertionError("expected OperatorError")
    except OperatorError:
        pass
    assert "Acknowledge" not in surface.clicks  # the human action never ran


class _OffScopeSurface(_HandoffSurface):
    async def click(self, target: TargetDescriptor) -> None:
        if target.name == "Leave":
            # A human click that navigates the session out of the allowed scope.
            self.clicks.append(target.name)
            self.url = "http://evil.example/x"
            self.route = "/x"
            self.dialog = False
            return
        await super().click(target)


async def test_human_navigation_stays_within_scope() -> None:
    surface = _OffScopeSurface()
    session = _session(surface)
    await session.start()
    await session.advance()
    operator = OperatorController(session)
    operator.take_control()
    try:
        await operator.perform(ClickControl(TargetDescriptor(role="link", name="Leave")))
        raise AssertionError("expected OperatorScopeError")
    except OperatorScopeError:
        pass
