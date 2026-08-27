"""The minimal real operator control path for same-session human handoff.

A human takes exclusive control of the exact live session automation was using,
performs bounded computer-use actions to resolve a blocked state, and hands
control back. The operator is the decision authority during takeover, so their
actions bypass the automation risk/confirmation gate — but they are not an
unrestricted escape hatch: the action vocabulary is a narrow set of semantic
computer-use primitives (no arbitrary script, shell, URL, or raw driver access),
the navigation allowlist still applies, and every action is audited through the
same evidence-safety boundary. Only the current lease owner may act, so automation
and the human can never drive the session simultaneously.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from computer_use.execution import ControlLease, InterventionSignal, ReplaySession
from computer_use.model import ControlOwner, Escalated, PolicyEffect, RunResult, TargetDescriptor
from computer_use.observability import (
    EvidenceCollector,
    EvidencePolicy,
    EvidenceStore,
    control_transferred_event,
    human_action_event,
)
from computer_use.safety import NavigationPolicy
from computer_use.surface import Surface

from .intervention import InterventionReason, InterventionRequest

_DEFAULT_OPERATOR_ID = "local-operator"


class HandoffSession(Protocol):
    """What the operator needs from any pausable session (replay or discovery).

    Both the deterministic replay session and the discovery loop expose this shape,
    so a single operator control path serves both without a second architecture.
    """

    run_id: str
    surface: Surface
    lease: ControlLease
    pending: InterventionSignal | None

    @property
    def nav_policy(self) -> NavigationPolicy: ...
    async def current_route(self) -> str: ...
    def route_label(self, path: str) -> str: ...


@dataclass(frozen=True)
class ClickControl:
    """Human-triggered click on a semantic control."""

    target: TargetDescriptor


@dataclass(frozen=True)
class TypeControl:
    """Human-triggered text entry into a semantic control.

    ``value`` is used transiently to drive the surface and is never persisted;
    the audit records only that a value was entered. ``submit`` submits the field's
    form (Enter) for a control that has no separate submit button.
    """

    target: TargetDescriptor
    value: str
    submit: bool = False


# The bounded human action vocabulary. Deliberately the same semantic
# computer-use primitives automation uses — not arbitrary execution.
HumanAction = ClickControl | TypeControl


class OperatorError(Exception):
    """An operator action was attempted out of turn or is unsupported."""


class OperatorScopeError(OperatorError):
    """A human action left the allowed navigation scope (fail closed)."""


class OperatorController:
    """Drives a paused session through a real, exclusive human control transfer."""

    def __init__(
        self,
        session: HandoffSession,
        *,
        evidence: EvidenceStore | None = None,
        evidence_collector: EvidenceCollector | None = None,
        operator_id: str | None = None,
    ) -> None:
        self._session = session
        self._evidence = evidence
        self._operator_id: str = (
            operator_id or os.getenv("CUA_OPERATOR_ID") or _DEFAULT_OPERATOR_ID
        )
        self._collector = evidence_collector or EvidenceCollector(
            EvidencePolicy(), session.nav_policy.allowed_routes
        )

    async def raise_intervention(self) -> InterventionRequest:
        """Build the sanitized request for the current pause (no page content)."""
        pending = self._session.pending
        if pending is None:
            raise OperatorError("no pending intervention to raise")
        route = await self._session.current_route()
        evidence = await self._collector.collect_failure_evidence(self._session.surface, route)
        return InterventionRequest(
            intervention_id=pending.intervention_id,
            run_id=pending.run_id,
            capability=pending.capability,
            version=pending.version,
            step_id=pending.step_id,
            reason=InterventionReason(pending.reason),
            control_owner=self._session.lease.owner,
            control_epoch=self._session.lease.epoch,
            route=self._session.route_label(route),
            evidence=evidence,
            ts=datetime.now(tz=UTC),
        )

    async def visible_controls(self) -> list[str]:
        """The interactable controls on the current page, as ``role:name`` strings.

        Lets an operator see the exact field/button names to act on, rather than guess.
        """
        observation = await self._session.surface.observe()
        return [f"{c.role}:{c.name}" for c in observation.candidates if c.name]

    def take_control(self) -> int:
        """Transfer exclusive control to the human; returns the new control epoch."""
        pending = self._session.pending
        if pending is None:
            raise OperatorError("cannot take control without a pending intervention")
        if self._session.lease.owner is ControlOwner.HUMAN:
            raise OperatorError("control is already held by the operator")
        before = self._session.lease.owner
        epoch = self._session.lease.to_human()
        self._audit_transfer(before, ControlOwner.HUMAN, epoch, reason=pending.reason)
        return epoch

    async def perform(self, action: HumanAction) -> None:
        """Perform one bounded human action on the same live session.

        The operator must currently hold control. The action executes directly on
        the surface (the human, not the automation kernel, is the authority), then
        the navigation allowlist is re-checked so human control cannot be used to
        leave the permitted scope.
        """
        if self._session.lease.owner is not ControlOwner.HUMAN:
            raise OperatorError("operator must hold control to act")
        surface = self._session.surface
        # Settle before acting: a human action must land on a stable page, not one still
        # navigating from the step that raised the intervention (a submit control would
        # otherwise detach mid-click).
        await surface.wait_settled()
        if isinstance(action, ClickControl):
            await surface.click(action.target)
            action_type, target, value_present = "click", action.target, False
        elif isinstance(action, TypeControl):
            await surface.type_text(action.target, action.value, submit=action.submit)
            action_type, target, value_present = "type", action.target, True
        else:  # pragma: no cover - exhaustive over the bounded HumanAction union
            raise OperatorError("unsupported human action")
        await surface.wait_settled()
        route = self._session.route_label(await surface.current_route())
        # Audit first (even a scope-violating action is recorded), then enforce scope.
        self._audit_action(action_type, target, route, value_present)
        scope = self._session.nav_policy.check_all(await surface.scope_urls())
        if scope.effect is PolicyEffect.DENY:
            raise OperatorScopeError("human action left the allowed navigation scope")

    def release_to_automation(self) -> int:
        """Hand exclusive control back to automation; returns the new control epoch.

        This is the generic hand-back used when the caller performs its own
        continuation after a human resolves a block (e.g. discovery re-observing).
        Replay's reconcile-before-resume path is ``resume`` instead.
        """
        if self._session.lease.owner is not ControlOwner.HUMAN:
            raise OperatorError("cannot release control without currently holding it")
        epoch = self._session.lease.to_automation()
        self._audit_transfer(ControlOwner.HUMAN, ControlOwner.AUTOMATION, epoch)
        return epoch

    async def resume(self) -> RunResult:
        """Reconcile, then hand control back to automation only if it is safe to resume.

        Replay-specific: the resume cursor is derived from the capability's checkpoints.
        If the blocking state remains, control is *retained* by the human (so they can
        resolve it and resume again) and the pending intervention is re-reported. On a
        fail-closed reconciliation or a clean resume, control returns to automation.
        """
        session = self._session
        if not isinstance(session, ReplaySession):
            raise OperatorError(
                "resume() reconciles a replay session; use release_to_automation() otherwise"
            )
        if session.lease.owner is not ControlOwner.HUMAN:
            raise OperatorError("resume requires the operator to currently hold control")
        # An ambiguous mutation is resolved by re-establishing the effect, not by
        # checkpoint reconciliation. Hand control back to automation first: the embedded
        # verification re-runs its read-only steps through the kernel, which fences
        # automation while the human holds the lease. It re-verifies only, never the write.
        if session.pending is not None and session.pending.reason == "MUTATION_AMBIGUOUS":
            resumed = session.lease.to_automation()
            self._audit_transfer(ControlOwner.HUMAN, ControlOwner.AUTOMATION, resumed)
            mutation_result = await session.reverify_mutation()
            if isinstance(mutation_result, Escalated):
                # Still ambiguous: return control to the human to resolve and retry.
                held = session.lease.to_human()
                self._audit_transfer(ControlOwner.AUTOMATION, ControlOwner.HUMAN, held)
                return mutation_result
            return mutation_result
        # Judge readiness while the human still owns the session (reads only).
        outcome = await session.assess_reconciliation()
        if isinstance(outcome, Escalated):
            # Still blocked: keep human control so the operator can resolve and retry.
            return outcome
        # Ready (None) or a terminal fail-closed result: return control to automation.
        epoch = session.lease.to_automation()
        self._audit_transfer(ControlOwner.HUMAN, ControlOwner.AUTOMATION, epoch)
        if outcome is not None:
            return outcome  # terminal fail-closed reconciliation result
        return await session.resume_from_cursor()

    def _audit_transfer(
        self, frm: ControlOwner, to: ControlOwner, epoch: int, reason: str | None = None
    ) -> None:
        if self._evidence is None:
            return
        self._evidence.write(
            control_transferred_event(
                self._session.run_id, frm.value, to.value, epoch, self._operator_id, reason
            )
        )

    def _audit_action(
        self, action: str, target: TargetDescriptor, route: str, value_present: bool
    ) -> None:
        if self._evidence is None:
            return
        self._evidence.write(
            human_action_event(
                self._session.run_id,
                self._session.lease.epoch,
                self._operator_id,
                action,
                target,
                route,
                value_present,
            )
        )
