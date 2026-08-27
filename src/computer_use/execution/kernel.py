"""The trusted kernel: the single authority path from proposal to side effect.

No model output causes a side effect directly. Every proposal is validated
(vocabulary, required fields, value provenance, policy scope), resolved to a
unique target (fail closed on missing/ambiguous), risk-classified by software,
and gated before the surface acts. The model reasons about symbolic value refs;
the raw invocation value is resolved here, immediately before acting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from computer_use.model import (
    ClickAction,
    ExtractAction,
    ParameterRef,
    PolicyEffect,
    ProposedAction,
    ProposedActionType,
    RiskClass,
    SafeLiteral,
    SecretRef,
    Step,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
    ValueRef,
)
from computer_use.safety import (
    ConfirmationPolicy,
    MissingSecret,
    Policy,
    RiskClassifier,
    SecretProvider,
)
from computer_use.surface import Candidate, Surface, SurfaceError

from .approval import ApprovalGrant, ApprovalRequest, ApprovalRequired, fingerprint_of
from .lease import ControlLease, ControlLeaseError

_EXECUTABLE = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)


class RejectionCode(StrEnum):
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    UNKNOWN_CANDIDATE = "UNKNOWN_CANDIDATE"
    UNRESOLVABLE_CANDIDATE = "UNRESOLVABLE_CANDIDATE"
    MISSING_VALUE = "MISSING_VALUE"
    MISSING_OUTPUT = "MISSING_OUTPUT"
    POLICY_DENIED = "POLICY_DENIED"
    TARGET_MISSING = "TARGET_MISSING"
    LOCATOR_AMBIGUOUS = "LOCATOR_AMBIGUOUS"
    RISK_CONFIRMATION_REQUIRED = "RISK_CONFIRMATION_REQUIRED"
    APPROVAL_STALE = "APPROVAL_STALE"
    UNKNOWN_PARAMETER = "UNKNOWN_PARAMETER"
    UNSUPPORTED_VALUE = "UNSUPPORTED_VALUE"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    CONTROL_NOT_OWNED = "CONTROL_NOT_OWNED"


class KernelRejection(Exception):
    def __init__(self, code: RejectionCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


class MutationDispatchUncertain(Exception):
    """A consequential write was dispatched and its outcome is not yet known.

    Raised only from the dispatch call itself (never from resolution/authorization,
    which precede any side effect). The effect may have reached the application, so
    the action must not be retried; the runtime verifies through read-back.
    """

    def __init__(self, operation_id: str, target: TargetDescriptor) -> None:
        self.operation_id = operation_id
        self.target = target
        super().__init__(f"consequential dispatch uncertain: {operation_id or '<unknown>'}")


@dataclass(frozen=True)
class KernelExecution:
    action: ProposedActionType
    target: TargetDescriptor
    risk: RiskClass
    value: ValueRef | None
    extracted: str | None


class ValueResolver:
    """Resolves a typed ValueRef to a concrete string inside the trusted runtime.

    A resolved value (especially a secret) exists only transiently here, immediately
    before it is used; it is never sent to the model or persisted in evidence.
    """

    def __init__(self, inputs: dict[str, str], secrets: SecretProvider | None = None) -> None:
        self._inputs = inputs
        self._secrets = secrets

    def resolve(self, ref: ValueRef) -> str:
        if isinstance(ref, ParameterRef):
            if ref.name not in self._inputs:
                raise KernelRejection(RejectionCode.UNKNOWN_PARAMETER, ref.name)
            return self._inputs[ref.name]
        if isinstance(ref, SafeLiteral):
            return ref.value
        if isinstance(ref, SecretRef):
            if self._secrets is None:
                raise KernelRejection(RejectionCode.SECRET_UNAVAILABLE, ref.name)
            try:
                return self._secrets.resolve(ref.name)
            except MissingSecret:
                raise KernelRejection(RejectionCode.SECRET_UNAVAILABLE, ref.name) from None
        raise KernelRejection(RejectionCode.UNSUPPORTED_VALUE, ref.source)


def _descriptor_from_candidate(candidate: Candidate) -> TargetDescriptor:
    if candidate.role == "cell" and candidate.row and candidate.column:
        return TargetDescriptor(
            frame=candidate.frame,
            table_cell=TableCellTarget(
                row_contains=candidate.row, column_header=candidate.column
            ),
        )
    if candidate.name:
        return TargetDescriptor(role=candidate.role, name=candidate.name, frame=candidate.frame)
    if candidate.text:
        # text is resolved by text alone; a role qualifier would be ignored downstream.
        return TargetDescriptor(text=candidate.text, frame=candidate.frame)
    raise KernelRejection(RejectionCode.UNRESOLVABLE_CANDIDATE, candidate.id)


class TrustedKernel:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        classifier: RiskClassifier,
        values: ValueResolver,
        confirmation: ConfirmationPolicy | None = None,
        lease: ControlLease | None = None,
        commit_timeout_ms: int | None = None,
        interactive_approval: bool = False,
    ) -> None:
        self._surface = surface
        self._policy = policy
        self._classifier = classifier
        self._values = values
        self._confirmation = confirmation if confirmation is not None else ConfirmationPolicy()
        # Bounded timeout applied to a consequential click so a withheld completion
        # fails fast as an uncertain dispatch instead of hanging.
        self._commit_timeout_ms = commit_timeout_ms
        # When enabled (discovery/authoring), a consequential action with no standing
        # approval raises a typed ApprovalRequired for orchestration to resolve with a
        # human, rather than a terminal RISK_CONFIRMATION_REQUIRED. Off by default, so
        # replay through a static ConfirmationPolicy is unchanged.
        self._interactive_approval = interactive_approval
        # When a lease is present, automation may act only while it owns the session
        # at the expected epoch; otherwise every side effect fails closed. Absent a
        # lease the kernel behaves as an unguarded single-owner authority path.
        self._lease = lease

    async def execute(
        self,
        proposal: ProposedAction,
        candidates: dict[str, Candidate],
        epoch: int | None = None,
        approval: ApprovalGrant | None = None,
    ) -> KernelExecution:
        """Discovery entry point: validate + resolve a model proposal, then act.

        A consequential proposal may require a one-time ``approval`` (see the
        interactive approval seam); the kernel re-resolves and re-fingerprints the
        operation here and validates the grant immediately before dispatch.
        """
        action = proposal.action
        if action not in _EXECUTABLE:
            raise KernelRejection(RejectionCode.NOT_EXECUTABLE, action.value)
        if proposal.candidate_id is None:
            raise KernelRejection(RejectionCode.UNRESOLVABLE_CANDIDATE, "no candidate_id")
        candidate = candidates.get(proposal.candidate_id)
        if candidate is None:
            raise KernelRejection(RejectionCode.UNKNOWN_CANDIDATE, proposal.candidate_id)
        target = _descriptor_from_candidate(candidate)
        return await self._authorize_and_execute(
            action, target, proposal.value, proposal.output,
            operation_id=None, epoch=epoch, approval=approval,
        )

    async def execute_step(
        self, step: Step, epoch: int | None = None, operation_id: str | None = None
    ) -> KernelExecution:
        """Replay entry point: execute a compiled step through the same authority path.

        ``operation_id`` scopes the confirmation approval to a specific trusted
        operation (e.g. capability + version + step); it defaults to the step id.
        """
        if step.target is None:
            raise KernelRejection(RejectionCode.UNRESOLVABLE_CANDIDATE, "step has no target")
        value: ValueRef | None = None
        if isinstance(step.action, ClickAction):
            action = ProposedActionType.CLICK
        elif isinstance(step.action, TypeAction):
            action = ProposedActionType.TYPE
            value = step.action.value
        elif isinstance(step.action, ExtractAction):
            action = ProposedActionType.EXTRACT
        else:
            raise KernelRejection(RejectionCode.NOT_EXECUTABLE, step.action.type)
        return await self._authorize_and_execute(
            action, step.target, value, step.output,
            operation_id=operation_id if operation_id is not None else step.id, epoch=epoch,
        )

    def _assert_may_act(self, epoch: int | None) -> None:
        """Fail closed unless automation currently owns the session at ``epoch``."""
        if self._lease is None:
            return
        try:
            self._lease.assert_automation_may_act(epoch)
        except ControlLeaseError as error:
            raise KernelRejection(RejectionCode.CONTROL_NOT_OWNED, str(error)) from error

    async def _authorize_and_execute(
        self,
        action: ProposedActionType,
        target: TargetDescriptor,
        value: ValueRef | None,
        output: str | None,
        operation_id: str | None,
        epoch: int | None = None,
        approval: ApprovalGrant | None = None,
    ) -> KernelExecution:
        # Ownership gate first: while a human holds the lease (or the epoch is stale),
        # automation touches nothing on the surface — not even target resolution.
        self._assert_may_act(epoch)

        # Policy scope. This gates on action type today; a target-scoped rule should
        # gate on the resolved control (see _resolve_target), not the primary descriptor.
        decision = self._policy.check(action, target)
        if decision.effect is PolicyEffect.DENY:
            raise KernelRejection(RejectionCode.POLICY_DENIED, decision.reason)

        # Required fields.
        if action is ProposedActionType.TYPE and value is None:
            raise KernelRejection(RejectionCode.MISSING_VALUE)
        if action is ProposedActionType.EXTRACT and not output:
            raise KernelRejection(RejectionCode.MISSING_OUTPUT)

        # Resolve the primary, then ordered fallbacks. Ambiguity fails closed and is
        # never dodged by falling through to a lower-priority locator.
        resolved = await self._resolve_target(target)

        # Software-owned risk classification, on the control actually resolved,
        # before any side effect.
        risk = self._classifier.classify(action, resolved)
        if risk is not RiskClass.READ_ONLY:
            # A consequential action dispatches only if this specific operation is
            # approved; an irreversible action is never auto-approved.
            statically_approved = self._confirmation.is_approved(operation_id)
            if risk is RiskClass.IRREVERSIBLE or not statically_approved:
                interactive = (
                    self._interactive_approval
                    and risk is not RiskClass.IRREVERSIBLE
                    and not statically_approved
                )
                if interactive:
                    await self._require_one_time_approval(action, resolved, epoch, approval)
                else:
                    raise KernelRejection(RejectionCode.RISK_CONFIRMATION_REQUIRED, risk.value)

        # Re-check ownership immediately before the side effect: a human may have
        # taken over while the target was being resolved, superseding this epoch.
        self._assert_may_act(epoch)

        # Execute. The dispatch boundary is the click call itself: everything above
        # (resolution, policy, confirmation, ownership) precedes any side effect, so a
        # failure there is definitely NOT_DISPATCHED. Once a consequential click is
        # invoked, a failure means the effect may have committed -> uncertain, no retry.
        extracted: str | None = None
        if action is ProposedActionType.CLICK:
            if risk is RiskClass.READ_ONLY:
                await self._surface.click(resolved)
            else:
                try:
                    await self._surface.click(resolved, timeout_ms=self._commit_timeout_ms)
                except SurfaceError as error:
                    raise MutationDispatchUncertain(operation_id or "", resolved) from error
        elif action is ProposedActionType.TYPE:
            assert value is not None
            await self._surface.type_text(resolved, self._values.resolve(value))
        else:  # EXTRACT
            extracted = await self._surface.extract(resolved)
        return KernelExecution(
            action=action, target=resolved, risk=risk, value=value, extracted=extracted
        )

    async def _require_one_time_approval(
        self,
        action: ProposedActionType,
        resolved: TargetDescriptor,
        epoch: int | None,
        approval: ApprovalGrant | None,
    ) -> None:
        """Emit or validate a one-time human approval for a consequential action.

        The fingerprint binds the operation to the resolved target and the current
        landmark, computed here — immediately before dispatch, after re-resolution.
        With no grant, a typed requirement is raised for orchestration to resolve.
        With a grant whose fingerprint no longer matches the live operation, the
        approval is stale (the page moved between request and grant) and dispatch is
        refused so orchestration re-observes. The kernel never prompts a human.
        """
        landmark = await self._surface.primary_heading()
        current = fingerprint_of(action, resolved, landmark, epoch)
        if approval is None:
            raise ApprovalRequired(
                ApprovalRequest(proposal_nonce=uuid4().hex, risk=RiskClass.CONSEQUENTIAL_WRITE,
                                fingerprint=current)
            )
        if approval.fingerprint != current:
            raise KernelRejection(
                RejectionCode.APPROVAL_STALE,
                "the operation or its observable state changed before authorization",
            )

    async def _resolve_target(self, target: TargetDescriptor) -> TargetDescriptor:
        """Resolve the primary then ordered fallbacks to a uniquely-matching descriptor.

        Exactly one match -> use it. More than one -> LOCATOR_AMBIGUOUS immediately
        (never dodged). Zero -> try the next fallback. None matched -> TARGET_MISSING.
        """
        for descriptor in (target, *target.fallbacks):
            matches = await self._surface.count(descriptor)
            if matches > 1:
                raise KernelRejection(RejectionCode.LOCATOR_AMBIGUOUS, str(matches))
            if matches == 1:
                return descriptor
        raise KernelRejection(RejectionCode.TARGET_MISSING)
