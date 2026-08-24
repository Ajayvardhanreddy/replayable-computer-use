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

from computer_use.model import (
    ClickAction,
    ExtractAction,
    ParameterRef,
    PolicyEffect,
    ProposedAction,
    ProposedActionType,
    RiskClass,
    SafeLiteral,
    Step,
    TableCellTarget,
    TargetDescriptor,
    TypeAction,
    ValueRef,
)
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import Candidate, Surface

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
    UNKNOWN_PARAMETER = "UNKNOWN_PARAMETER"
    UNSUPPORTED_VALUE = "UNSUPPORTED_VALUE"


class KernelRejection(Exception):
    def __init__(self, code: RejectionCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


@dataclass(frozen=True)
class KernelExecution:
    action: ProposedActionType
    target: TargetDescriptor
    risk: RiskClass
    value: ValueRef | None
    extracted: str | None


class ValueResolver:
    """Resolves a typed ValueRef to a concrete string inside the trusted runtime."""

    def __init__(self, inputs: dict[str, str]) -> None:
        self._inputs = inputs

    def resolve(self, ref: ValueRef) -> str:
        if isinstance(ref, ParameterRef):
            if ref.name not in self._inputs:
                raise KernelRejection(RejectionCode.UNKNOWN_PARAMETER, ref.name)
            return self._inputs[ref.name]
        if isinstance(ref, SafeLiteral):
            return ref.value
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
    ) -> None:
        self._surface = surface
        self._policy = policy
        self._classifier = classifier
        self._values = values

    async def execute(
        self, proposal: ProposedAction, candidates: dict[str, Candidate]
    ) -> KernelExecution:
        """Discovery entry point: validate + resolve a model proposal, then act."""
        action = proposal.action
        if action not in _EXECUTABLE:
            raise KernelRejection(RejectionCode.NOT_EXECUTABLE, action.value)
        if proposal.candidate_id is None:
            raise KernelRejection(RejectionCode.UNRESOLVABLE_CANDIDATE, "no candidate_id")
        candidate = candidates.get(proposal.candidate_id)
        if candidate is None:
            raise KernelRejection(RejectionCode.UNKNOWN_CANDIDATE, proposal.candidate_id)
        target = _descriptor_from_candidate(candidate)
        return await self._authorize_and_execute(action, target, proposal.value, proposal.output)

    async def execute_step(self, step: Step) -> KernelExecution:
        """Replay entry point: execute a compiled step through the same authority path."""
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
        return await self._authorize_and_execute(action, step.target, value, step.output)

    async def _authorize_and_execute(
        self,
        action: ProposedActionType,
        target: TargetDescriptor,
        value: ValueRef | None,
        output: str | None,
    ) -> KernelExecution:
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
            raise KernelRejection(RejectionCode.RISK_CONFIRMATION_REQUIRED, risk.value)

        # Execute.
        extracted: str | None = None
        if action is ProposedActionType.CLICK:
            await self._surface.click(resolved)
        elif action is ProposedActionType.TYPE:
            assert value is not None
            await self._surface.type_text(resolved, self._values.resolve(value))
        else:  # EXTRACT
            extracted = await self._surface.extract(resolved)
        return KernelExecution(
            action=action, target=resolved, risk=risk, value=value, extracted=extracted
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
