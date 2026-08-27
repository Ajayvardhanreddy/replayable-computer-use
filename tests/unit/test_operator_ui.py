"""The operator presentation layer renders the control-transfer state machine faithfully
and leaks nothing sensitive.

These are content assertions on plain (non-terminal) output: the renderers must show the
ownership transitions, epochs, reasons, and the caller's own deliverable outputs — and must
never invent raw PII or financial values that were not already in the caller's result.
"""

from datetime import UTC, datetime
from io import StringIO

from rich.console import Console

from computer_use.handoff import operator_ui
from computer_use.handoff.intervention import InterventionReason, InterventionRequest
from computer_use.model import ControlOwner, Failure, FailureCode, Success
from computer_use.observability import FailureEvidence, ScreenshotPolicy


def _plain_console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    # force_terminal=False -> rich emits plain text, so styling never pollutes assertions
    # (and, by the same path, never leaks into piped/CI/evidence output).
    return Console(file=buffer, force_terminal=False, width=100), buffer


def _request(reason: InterventionReason) -> InterventionRequest:
    return InterventionRequest(
        intervention_id="int_test01",
        run_id="run_test01",
        capability="member.open_sub_account",
        version=1,
        step_id="step_4_click",
        reason=reason,
        control_owner=ControlOwner.AUTOMATION,
        control_epoch=0,
        route="/workspace/member/:member_number",
        evidence=FailureEvidence(
            policy=ScreenshotPolicy.STRUCTURAL_ONLY,
            route="/workspace/member/:member_number",
            landmarks=["Verification Required"],
        ),
        ts=datetime.now(tz=UTC),
    )


def test_prompt_shows_owner_and_epoch() -> None:
    console, buffer = _plain_console()
    console.print(operator_ui.prompt_text("AUTOMATION", 0))
    console.print(operator_ui.prompt_text("HUMAN", 1))
    out = buffer.getvalue()
    assert "[AUTOMATION · epoch 0]" in out
    assert "[HUMAN · epoch 1]" in out
    assert "operator ❯" in out


def test_control_transfer_shows_direction_epoch_and_session_preserved() -> None:
    console, buffer = _plain_console()
    operator_ui.render_control_transfer(
        "AUTOMATION", "HUMAN", 1, session_id="sess_8f2a1b3c", console=console
    )
    out = buffer.getvalue()
    assert "Control transferred" in out
    assert "AUTOMATION" in out and "HUMAN" in out and "→" in out
    assert "epoch → 1" in out
    # The same-live-session invariant is made visible.
    assert "sess_8f2a1b3c" in out and "preserved" in out


def test_reconciliation_reports_read_only_reverify_and_no_retry() -> None:
    console, buffer = _plain_console()
    operator_ui.render_reconciliation(
        [
            "Reconciling…",
            "Control returned HUMAN → AUTOMATION (epoch 2)",
            "Re-ran READ-ONLY verification — the consequential write was NOT re-dispatched",
            "model_calls: 0",
        ],
        console=console,
    )
    out = buffer.getvalue()
    assert "READ-ONLY" in out
    assert "NOT re-dispatched" in out
    assert "model_calls: 0" in out


def test_intervention_panel_shows_reason_step_and_epoch_no_route_param() -> None:
    console, buffer = _plain_console()
    operator_ui.render_intervention(
        _request(InterventionReason.MUTATION_AMBIGUOUS),
        title="INTERVENTION REQUIRED",
        commands=[("take", "take control"), ("resume", "hand back")],
        console=console,
    )
    out = buffer.getvalue()
    assert "INTERVENTION REQUIRED" in out
    assert "MUTATION_AMBIGUOUS" in out
    assert "step_4_click" in out
    assert "epoch" in out.lower()
    # Structural route pattern only — never a concrete member number.
    assert ":member_number" in out


def test_handoff_case_states_dispatch_once_and_next_action() -> None:
    console, buffer = _plain_console()
    operator_ui.render_handoff_case(
        _request(InterventionReason.MUTATION_AMBIGUOUS),
        "verification: effect could not be established",
        console=console,
    )
    out = buffer.getvalue()
    assert "HANDOFF CASE" in out
    assert "exactly once" in out
    assert "system of record" in out


def test_intervention_panel_renders_expected_vs_observed_facts_not_prose() -> None:
    console, buffer = _plain_console()
    facts = operator_ui.InterventionFacts(
        last_action="click Search",
        expected_heading="Member Profile",
        expected_output="savings_balance",
        route="/workspace/member/:member_number",
        observed_heading="Member Profile",
        blocker=operator_ui.Blocker(
            role="dialog",
            name="System Notice",
            text="Please acknowledge the account notice before continuing.",
            controls=[("c1", "link", "Acknowledge")],
        ),
    )
    operator_ui.render_intervention(
        _request(InterventionReason.UNKNOWN_DIALOG),
        title="INTERVENTION REQUIRED",
        commands=[("take", "take control")],
        facts=facts,
        session_id="sess_8f2a1b3c",
        console=console,
    )
    out = buffer.getvalue()
    # Deterministic facts, not an AI-written sentence.
    assert "Expected" in out and "Member Profile" in out and "savings_balance" in out
    assert "Observed" in out and "System Notice" in out
    # The dialog copy is labelled as the dialog's own text, not an automation note.
    assert "Dialog text" in out and "acknowledge the account notice" in out
    # The stable session id is shown (same-live-session made visible).
    assert "Session" in out and "sess_8f2a1b3c" in out
    # Controls are scoped to the blocker (the Acknowledge link), not the whole page.
    assert "c1" in out and "Acknowledge" in out


def test_observed_inspect_shows_current_state_and_blocker() -> None:
    console, buffer = _plain_console()
    facts = operator_ui.InterventionFacts(
        route="/",
        observed_heading="Member Profile",
        blocker=operator_ui.Blocker("dialog", "System Notice", "notice", [("c1", "link", "Ack")]),
    )
    operator_ui.render_observed(facts, console=console)
    out = buffer.getvalue()
    assert "Current state" in out
    assert "System Notice" in out and "c1" in out


def test_controls_scoped_to_blocker_and_commands_shown_separately() -> None:
    console, buffer = _plain_console()
    # A blocker present -> controls are scoped to it and headed as such, no whole-page dump.
    operator_ui.render_controls(
        [("c1", "link", "Acknowledge")], blocker=("dialog", "System Notice"), console=console
    )
    out = buffer.getvalue()
    assert "blocked by dialog" in out and "System Notice" in out
    assert "c1" in out and "Acknowledge" in out
    # The command vocabulary is shown separately (once, not on every refresh).
    console2, buffer2 = _plain_console()
    operator_ui.render_commands(console=console2)
    cmds = buffer2.getvalue()
    assert "click <control>" in cmds and "submit <control>=<value>" in cmds and "resume" in cmds


def test_result_panel_success_shows_only_caller_outputs() -> None:
    console, buffer = _plain_console()
    result = Success(
        run_id="run_test01",
        capability="member.open_sub_account",
        version=1,
        outputs={"sub_account_status": "OPEN"},
        model_calls=0,
    )
    operator_ui.render_result(result, console=console)
    out = buffer.getvalue()
    assert "SUCCESS" in out
    assert "sub_account_status" in out and "OPEN" in out
    assert "model_calls" in out and "0" in out


def test_result_panel_failure_shows_code() -> None:
    console, buffer = _plain_console()
    result = Failure(run_id="run_test01", code=FailureCode.MUTATION_NOT_COMMITTED)
    operator_ui.render_result(result, console=console)
    out = buffer.getvalue()
    assert "FAILURE" in out
    assert "MUTATION_NOT_COMMITTED" in out
