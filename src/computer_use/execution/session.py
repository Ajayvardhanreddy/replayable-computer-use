"""Resumable deterministic replay driven by a single trusted kernel.

A ``ReplaySession`` executes a compiled Capability step by step against one live
session, with no model in the loop (``model_calls`` is always 0). It is the unit
that makes same-session human handoff possible: when an unhandled blocking state
is observed the session pauses in place — the live surface stays open and the
cursor is preserved — and reports an intervention. After a human resolves the
state and returns control, the session reconciles the observable page against the
capability's own checkpoints before it resumes; it never blindly advances to the
next step.

Runtime conditions are handled deliberately: a transient surface condition is
retried under a bounded budget, a declared business outcome is a legitimate
domain answer, a satisfied checkpoint continues, and anything else stops with a
typed Failure. No raw driver exception crosses this boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from computer_use.model import (
    BusinessOutcome,
    Capability,
    Condition,
    EffectState,
    Escalated,
    Failure,
    FailureCode,
    MutationVerification,
    OutcomeClass,
    OutputSpec,
    ParamType,
    PolicyEffect,
    ProposedActionType,
    RiskClass,
    RunResult,
    Step,
    Success,
)
from computer_use.safety import (
    AuthorityPolicy,
    ConfirmationPolicy,
    NavigationPolicy,
    Policy,
    RiskClassifier,
    SecretProvider,
    route_label,
    route_matches,
)
from computer_use.surface import (
    PlaywrightSurface,
    Surface,
    SurfaceError,
    SurfaceTransientError,
    TargetAmbiguousError,
    TargetNotFoundError,
)

from .kernel import (
    KernelExecution,
    KernelRejection,
    MutationDispatchUncertain,
    RejectionCode,
    TrustedKernel,
    ValueResolver,
)
from .lease import ControlLease

# Stable reason codes (plain strings so the execution layer stays free of the handoff
# layer; the operator surface maps them onto its typed InterventionReason). UNKNOWN_DIALOG
# is an unmodeled blocking state; MUTATION_AMBIGUOUS is a consequential write whose effect
# could not be established by read-back.
UNKNOWN_DIALOG = "UNKNOWN_DIALOG"
MUTATION_AMBIGUOUS = "MUTATION_AMBIGUOUS"

_REPLAY_ACTIONS = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
_RESOLVE_TIMEOUT_MS = 5000
# Bounded recovery for a classified transient surface condition.
_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_MS = 200

# Kernel authorization rejections mapped to the public failure taxonomy. Codes that
# a statically-validated artifact cannot reach (missing value/output, non-executable)
# default to POLICY_DENIED: the kernel refused to authorize the step. An ambiguous or
# missing locator is returned as a fail-closed typed Failure rather than acted on.
# CONTROL_NOT_OWNED also maps here: automation acting without the lease is refused.
_REJECTION_TO_FAILURE = {
    RejectionCode.TARGET_MISSING: FailureCode.TARGET_MISSING,
    RejectionCode.LOCATOR_AMBIGUOUS: FailureCode.LOCATOR_AMBIGUOUS,
    RejectionCode.POLICY_DENIED: FailureCode.POLICY_DENIED,
    RejectionCode.RISK_CONFIRMATION_REQUIRED: FailureCode.POLICY_DENIED,
    RejectionCode.CONTROL_NOT_OWNED: FailureCode.POLICY_DENIED,
}


@dataclass(frozen=True)
class InterventionSignal:
    """Neutral handle to a paused session for the handoff layer to route.

    Carries only stable identifiers and a reason code — no page content. The
    handoff layer adds sanitized structural evidence when it builds the request.
    """

    reason: str
    run_id: str
    capability: str
    version: int
    step_id: str | None
    intervention_id: str
    epoch: int


def _failure_code(code: RejectionCode) -> FailureCode:
    return _REJECTION_TO_FAILURE.get(code, FailureCode.POLICY_DENIED)


def _surface_failure(run_id: str, error: SurfaceError, step_id: str | None) -> Failure:
    """Map any Surface-level error to a typed hard Failure; nothing raw escapes.

    A resolve/act race (the DOM changes between count and click) surfaces as a
    not-found or ambiguous target and is reported as such; everything else is a
    driver failure or exhausted transient.
    """
    if isinstance(error, TargetNotFoundError):
        code = FailureCode.TARGET_MISSING
    elif isinstance(error, TargetAmbiguousError):
        code = FailureCode.LOCATOR_AMBIGUOUS
    else:
        code = FailureCode.SURFACE_ERROR
    return Failure(
        run_id=run_id,
        code=code,
        step_id=step_id,
        # No raw driver text (it can embed the URL / a path parameter); the code is
        # the signal.
        observed="surface error during replay",
        retryable=False,
        model_calls=0,
    )


def _nav_denied(
    run_id: str, nav_policy: NavigationPolicy, urls: list[str], step_id: str | None
) -> Failure | None:
    """A typed Failure if any current frame URL is out of navigation scope, else None."""
    decision = nav_policy.check_all(urls)
    if decision.effect is PolicyEffect.DENY:
        return Failure(
            run_id=run_id,
            code=FailureCode.POLICY_DENIED,
            step_id=step_id,
            # Structural rule only. The raw reason embeds the concrete URL/path (a
            # member id), so it must not reach a persisted failure.
            observed=f"navigation denied: {decision.rule}",
            retryable=False,
            model_calls=0,
        )
    return None


def _coerce_output(value: str, spec: OutputSpec | None) -> str:
    if spec is not None and spec.type is ParamType.DECIMAL:
        return value.replace("$", "").replace(",", "").strip()
    return value


def _sole_verification(capability: Capability) -> MutationVerification | None:
    """The one consequential write's embedded verification recipe, if the capability
    has one. Capability construction admits at most one, so the first is authoritative."""
    for step in capability.steps:
        if step.verification is not None:
            return step.verification
    return None


_ACTION_BY_TYPE = {
    "click": ProposedActionType.CLICK,
    "type": ProposedActionType.TYPE,
    "extract": ProposedActionType.EXTRACT,
}


def _step_action(step: Step) -> ProposedActionType:
    return _ACTION_BY_TYPE[step.action.type]


async def _matches(surface: Surface, condition: Condition) -> bool:
    """Evaluate a live condition. Every set matcher is required (AND); ``any_of`` is
    an OR subgroup. Unsupported matchers raise rather than silently returning False."""
    checks: list[bool] = []
    if condition.text_present is not None:
        checks.append(await surface.has_text(condition.text_present))
    if condition.heading is not None:
        checks.append(await surface.has_heading(condition.heading.name))
    if condition.route_pattern is not None:
        checks.append(route_matches(condition.route_pattern, await surface.current_route()))
    if condition.any_of is not None:
        checks.append(any([await _matches(surface, sub) for sub in condition.any_of]))
    if condition.output_present is not None:
        raise ValueError("output_present is a success-checkpoint matcher, not a live condition")
    if not checks:
        raise ValueError("condition has no evaluable matcher")
    return all(checks)


async def _success_satisfied(
    surface: Surface, checkpoint: Condition, outputs: dict[str, str]
) -> bool:
    if checkpoint.output_present is not None and checkpoint.output_present not in outputs:
        return False
    has_live = (
        checkpoint.text_present is not None
        or checkpoint.heading is not None
        or checkpoint.route_pattern is not None
        or checkpoint.any_of is not None
    )
    if has_live:
        live = checkpoint.model_copy(update={"output_present": None})
        if not await _matches(surface, live):
            return False
    return True


async def _observe(surface: Surface, routes: frozenset[str]) -> str:
    """A short, structural description of the current state for failure diagnostics.

    The route is recorded as its allowed-route *pattern*, never the concrete path, so
    a sensitive path parameter (a member id) is not persisted.
    """
    try:
        heading = await surface.primary_heading()
        route = await surface.current_route()
    except SurfaceError:
        return "observed state unavailable"
    return f"heading={heading!r} route={route_label(route, routes)!r}"


async def _resolve_step(surface: Surface, step: Step, timeout_ms: int) -> tuple[str, str | None]:
    """Race the step's business outcomes against its checkpoint after execution.

    A transient condition during polling is treated as not-yet-settled and the poll
    continues within the budget; a non-transient surface error propagates.
    """
    if not step.outcomes and step.postcondition is None:
        return ("continue", None)
    waited = 0
    while True:
        try:
            for outcome in step.outcomes:
                if outcome.outcome_class is OutcomeClass.BUSINESS_OUTCOME and await _matches(
                    surface, outcome.detector
                ):
                    return ("outcome", outcome.code)
            if step.postcondition is None or await _matches(surface, step.postcondition):
                return ("continue", None)
        except SurfaceTransientError:
            pass  # mid-navigation; keep polling within budget
        if waited >= timeout_ms:
            return ("failed", None)
        await asyncio.sleep(0.1)
        waited += 100


class ReplaySession:
    """Drives a compiled Capability against one live surface, pausable for handoff.

    The caller may inject a surface (then it owns the surface's lifecycle — the seam
    that lets a human operate the same session while replay is paused); otherwise the
    session creates and closes its own. A ``ControlLease`` guards the kernel so
    automation acts only while it owns the session at the current epoch.
    """

    def __init__(
        self,
        capability: Capability,
        inputs: dict[str, str],
        target_url: str,
        *,
        nav_policy: NavigationPolicy,
        safe_clicks: frozenset[str] = frozenset(),
        surface: Surface | None = None,
        resolve_timeout_ms: int = _RESOLVE_TIMEOUT_MS,
        secrets: SecretProvider | None = None,
        lease: ControlLease | None = None,
        confirmation: ConfirmationPolicy | None = None,
        commit_timeout_ms: int | None = None,
        authority: AuthorityPolicy | None = None,
    ) -> None:
        self._capability = capability
        self._inputs = inputs
        self._target_url = target_url
        self._nav_policy = nav_policy
        self._resolve_timeout_ms = resolve_timeout_ms
        self.run_id = f"run_{uuid4().hex[:8]}"
        self.owns_surface = surface is None
        self.surface: Surface = surface if surface is not None else PlaywrightSurface()
        self.lease = lease if lease is not None else ControlLease()
        # The classifier is shared with the kernel and reused to independently derive
        # risk during verification (defense in depth: a verification step must be read-only).
        self._classifier = RiskClassifier(safe_click_names=safe_clicks)
        # Trusted authority over verification read sources: whether an absent effect is a
        # definite non-commit. Conservative by default (absence -> ambiguous, not failure).
        self._authority = authority if authority is not None else AuthorityPolicy()
        self._kernel = TrustedKernel(
            self.surface,
            Policy(allowed_actions=_REPLAY_ACTIONS),
            self._classifier,
            ValueResolver(inputs, secrets),
            confirmation=confirmation,
            lease=self.lease,
            commit_timeout_ms=commit_timeout_ms,
        )
        self._outputs: dict[str, str] = {}
        self._cursor = 0
        self.pending: InterventionSignal | None = None
        # Certainty telemetry for the most recent consequential mutation (exposed for
        # evidence at the persistence boundary; the session never imports observability).
        self.last_effect_state: EffectState = EffectState.NOT_DISPATCHED
        self.last_verification_attempted = False
        self.last_effect_reason = ""
        self._pending_commit: Step | None = None
        # The single consequential write's verification recipe (if any) and the trusted
        # pre-dispatch baseline: True/False once evaluated on the effect view, else None.
        self._commit_verification: MutationVerification | None = _sole_verification(capability)
        self._baseline_present: bool | None = None

    @property
    def nav_policy(self) -> NavigationPolicy:
        return self._nav_policy

    @property
    def _routes(self) -> frozenset[str]:
        return self._nav_policy.allowed_routes

    async def current_route(self) -> str:
        return await self.surface.current_route()

    def route_label(self, path: str) -> str:
        """The structural route pattern for a path (never the concrete PII path)."""
        return route_label(path, self._routes)

    async def start(self) -> Failure | None:
        """Start an owned surface and navigate to the in-scope entry point.

        Returns a typed Failure if the entry is out of scope or the driver fails,
        else None (ready to advance).
        """
        try:
            if self.owns_surface:
                await self.surface.start()
            entry = self._target_url.rstrip("/") + "/"
            denied = _nav_denied(self.run_id, self._nav_policy, [entry], None)
            if denied is not None:
                return denied
            await self.surface.goto(entry)
        except SurfaceError as error:
            return _surface_failure(self.run_id, error, None)
        return None

    async def _execute_step(self, step: Step) -> KernelExecution:
        """Execute a step, retrying only a classified transient condition within budget.

        The lease epoch is captured immediately before each attempt so that a human
        takeover between scheduling and dispatch invalidates the automation action.
        """
        last: SurfaceTransientError | None = None
        for _ in range(_TRANSIENT_RETRIES):
            try:
                return await self._kernel.execute_step(step, epoch=self.lease.epoch)
            except SurfaceTransientError as transient:
                last = transient
                await asyncio.sleep(_RETRY_BACKOFF_MS / 1000)
        assert last is not None
        raise last

    def _escalate(self, reason: str, step: Step) -> Escalated:
        """Pause in place and report an intervention; the surface stays open."""
        if self.pending is None or self.pending.step_id != step.id:
            self.pending = InterventionSignal(
                reason=reason,
                run_id=self.run_id,
                capability=self._capability.id,
                version=self._capability.version,
                step_id=step.id,
                intervention_id=f"int_{uuid4().hex[:8]}",
                epoch=self.lease.epoch,
            )
        return Escalated(
            run_id=self.run_id,
            code=reason,
            step_id=step.id,
            intervention_id=self.pending.intervention_id,
            model_calls=0,
        )

    def _operation_id(self, step: Step) -> str:
        """Confirmation approval is scoped to a specific trusted operation, so an approval
        for one capability's step cannot authorize another capability's same-named step."""
        return f"{self._capability.id}:v{self._capability.version}:{step.id}"

    def _commit_success(self) -> Success:
        return Success(
            run_id=self.run_id,
            capability=self._capability.id,
            version=self._capability.version,
            outputs=dict(self._outputs),
            model_calls=0,
        )

    def _mutation_ambiguous(self, step: Step, reason: str) -> Escalated:
        self.last_effect_state = EffectState.AMBIGUOUS
        self.last_effect_reason = reason
        self._pending_commit = step
        return self._escalate(MUTATION_AMBIGUOUS, step)

    async def _maybe_capture_baseline(self) -> None:
        """Record the effect's presence on its view before the write, for attribution."""
        verification = self._commit_verification
        if verification is None:
            return
        try:
            if await _matches(self.surface, verification.page):
                self._baseline_present = await _matches(self.surface, verification.effect_present)
        except SurfaceError:
            pass  # baseline stays unestablished; a later present effect will be ambiguous

    async def _run_commit_verified(self, step: Step) -> RunResult:
        """A consequential write with an embedded, discovered verification recipe.

        Dispatch exactly once (never wrapped in transient retry); an explicit rejection
        on the immediate response is a business outcome; otherwise — clean or uncertain —
        confirm the effect through the discovered independent read. The write is never
        re-issued regardless of how its completion looked.
        """
        verification = step.verification
        assert verification is not None
        self.last_effect_state = EffectState.DISPATCHING
        self.last_verification_attempted = False
        self.last_effect_reason = ""
        try:
            await self._kernel.execute_step(
                step, epoch=self.lease.epoch, operation_id=self._operation_id(step)
            )
        except KernelRejection as rejection:
            self.last_effect_state = EffectState.NOT_DISPATCHED
            self.last_effect_reason = "not dispatched: authorization or resolution refused"
            return Failure(
                run_id=self.run_id,
                code=_failure_code(rejection.code),
                step_id=step.id,
                observed=await _observe(self.surface, self._routes),
                retryable=False,
                model_calls=0,
            )
        except MutationDispatchUncertain:
            self.last_effect_state = EffectState.DISPATCHED
            return await self._verify_embedded(step, verification)
        except SurfaceError as error:
            self.last_effect_state = EffectState.NOT_DISPATCHED
            return _surface_failure(self.run_id, error, step.id)
        # Dispatch returned. An explicit application rejection is a business outcome and
        # is decided before any verification; otherwise verify the effect independently.
        self.last_effect_state = EffectState.DISPATCHED
        try:
            await self.surface.wait_settled()
            for outcome in step.outcomes:
                if outcome.outcome_class is OutcomeClass.BUSINESS_OUTCOME and await _matches(
                    self.surface, outcome.detector
                ):
                    self.last_effect_state = EffectState.NOT_COMMITTED
                    self.last_effect_reason = f"explicit application rejection: {outcome.code}"
                    return BusinessOutcome(
                        run_id=self.run_id, capability=self._capability.id,
                        code=outcome.code, model_calls=0,
                    )
        except SurfaceError:
            pass  # fall through to independent verification
        return await self._verify_embedded(step, verification)

    async def _assert_read_only(self, vstep: Step) -> bool:
        """Runtime verification-mode gate: refuse any verification step whose software-
        derived risk is not READ_ONLY, independent of what the artifact claims."""
        if vstep.target is None:
            return False
        risk = self._classifier.classify(_step_action(vstep), vstep.target)
        return risk is RiskClass.READ_ONLY

    async def _verify_embedded(self, step: Step, v: MutationVerification) -> RunResult:
        """Establish the effect through the discovered read-only re-derivation.

        Runs the verification steps under a read-only-enforced mode, evaluates the effect
        on its view, and attributes a commit only as a baseline-absent -> present
        transition. Declared outputs are read (and published) only once the effect is
        confirmed present, so a failed/ambiguous verification never leaks a result.
        """
        self.last_verification_attempted = True
        try:
            for vstep in v.navigate:
                if not await self._assert_read_only(vstep):
                    return self._mutation_ambiguous(step, "verification step is not read-only")
                await self._execute_step(vstep)
                await self.surface.wait_settled()
                denied = _nav_denied(
                    self.run_id, self._nav_policy, await self.surface.scope_urls(), step.id
                )
                if denied is not None:
                    return self._mutation_ambiguous(step, "verification navigated out of scope")
            # An unexpected blocking dialog on the read means the effect cannot be
            # trusted from this page: escalate rather than read past it. Recoverable —
            # a human can clear it and resume, which re-runs verification (not the write).
            if await self.surface.has_blocking_dialog():
                return self._mutation_ambiguous(step, "verification blocked by a dialog")
            on_view = await _matches(self.surface, v.page)
            present = on_view and await _matches(self.surface, v.effect_present)
        except SurfaceError:
            return self._mutation_ambiguous(step, "verification could not establish effect view")
        if not on_view:
            return self._mutation_ambiguous(step, "verification did not reach effect view")
        if present:
            if self._baseline_present is not False:
                # Absent-before could not be established (or the effect pre-existed): the
                # present effect cannot be attributed to this write.
                return self._mutation_ambiguous(
                    step, "effect not attributable: baseline was not established absent"
                )
            if v.extract is not None:
                try:
                    if not await self._assert_read_only(v.extract):
                        return self._mutation_ambiguous(step, "verification extract not read-only")
                    execution = await self._execute_step(v.extract)
                    if v.extract.output is not None and execution.extracted is not None:
                        self._outputs[v.extract.output] = _coerce_output(
                            execution.extracted, self._capability.outputs.get(v.extract.output)
                        )
                except SurfaceError:
                    return self._mutation_ambiguous(
                        step, "effect present but its output could not be read"
                    )
            self.last_effect_state = EffectState.COMMITTED
            self.last_effect_reason = "verification: effect present"
            return self._commit_success()
        if self._baseline_present is False and self._authority.absence_is_authoritative():
            self.last_effect_state = EffectState.NOT_COMMITTED
            self.last_effect_reason = "verification: effect absent on authoritative read"
            return Failure(
                run_id=self.run_id,
                code=FailureCode.MUTATION_NOT_COMMITTED,
                step_id=step.id,
                observed=await _observe(self.surface, self._routes),
                retryable=False,
                model_calls=0,
            )
        return self._mutation_ambiguous(step, "verification: effect could not be established")

    async def reverify_mutation(self) -> RunResult:
        """Re-establish a paused ambiguous mutation after a human makes state observable.

        Re-runs the same independent verification; resolves to Success /
        MUTATION_NOT_COMMITTED, or stays paused (Escalated) if still uncertain. Never
        re-dispatches the write.
        """
        step = self._pending_commit
        if step is None:
            return self._commit_success()
        assert step.verification is not None
        result: RunResult = await self._verify_embedded(step, step.verification)
        if isinstance(result, Escalated):
            return result
        self.pending = None
        self._pending_commit = None
        return result

    async def advance(self) -> RunResult:
        """Run steps from the current cursor to a terminal result or an intervention.

        Returns Success / BusinessOutcome / Failure when the run resolves, or an
        Escalated (leaving the session paused and resumable) when a blocking state
        the artifact does not model is observed before the next step executes.
        """
        try:
            steps = self._capability.steps
            while self._cursor < len(steps):
                step = steps[self._cursor]
                await self.surface.wait_settled()
                denied = _nav_denied(
                    self.run_id, self._nav_policy, await self.surface.scope_urls(), step.id
                )
                if denied is not None:
                    return denied
                # Stop before executing another step if an unhandled blocking state is
                # present: it is not a known business outcome or recoverable condition,
                # so acting now would be unsafe. Route it to a human instead.
                if await self.surface.has_blocking_dialog():
                    return self._escalate(UNKNOWN_DIALOG, step)
                # A consequential write: dispatch once, then confirm the effect through
                # its discovered independent verification rather than retrying.
                if step.verification is not None:
                    return await self._run_commit_verified(step)
                try:
                    execution = await self._execute_step(step)
                    if step.output is not None and execution.extracted is not None:
                        self._outputs[step.output] = _coerce_output(
                            execution.extracted, self._capability.outputs.get(step.output)
                        )
                    # An action may navigate; re-check scope on the resulting page
                    # before reading it for an outcome or checkpoint.
                    await self.surface.wait_settled()
                    post = _nav_denied(
                        self.run_id, self._nav_policy, await self.surface.scope_urls(), step.id
                    )
                    if post is not None:
                        return post
                    kind, code = await _resolve_step(self.surface, step, self._resolve_timeout_ms)
                except KernelRejection as rejection:
                    return Failure(
                        run_id=self.run_id,
                        code=_failure_code(rejection.code),
                        step_id=step.id,
                        observed=await _observe(self.surface, self._routes),
                        retryable=False,
                        model_calls=0,
                    )
                except MutationDispatchUncertain:
                    # A consequential dispatch the artifact carried no verification recipe
                    # for: the effect may have happened and cannot be verified here. Fail
                    # closed to a human rather than retrying.
                    self.last_effect_state = EffectState.AMBIGUOUS
                    return self._escalate(MUTATION_AMBIGUOUS, step)
                except SurfaceError as error:
                    # Driver failure, exhausted transient, or a mid-act target race.
                    return _surface_failure(self.run_id, error, step.id)
                if kind == "outcome":
                    return BusinessOutcome(
                        run_id=self.run_id,
                        capability=self._capability.id,
                        code=code or "",
                        model_calls=0,
                    )
                if kind == "failed":
                    return Failure(
                        run_id=self.run_id,
                        code=FailureCode.CHECKPOINT_FAILED,
                        step_id=step.id,
                        expected=repr(step.postcondition),
                        observed=await _observe(self.surface, self._routes),
                        retryable=False,
                        model_calls=0,
                    )
                # Trusted pre-dispatch baseline: whenever replay is on the effect view
                # before the write, evaluate the discovered effect matcher. Absence here
                # is what makes a later present effect attributable to this write.
                await self._maybe_capture_baseline()
                self._cursor += 1
            return await self._finish()
        except SurfaceError as error:
            # Success-check / navigation errors not tied to a specific step.
            return _surface_failure(self.run_id, error, None)

    async def _finish(self) -> RunResult:
        denied = _nav_denied(
            self.run_id, self._nav_policy, await self.surface.scope_urls(), None
        )
        if denied is not None:
            return denied
        if await _success_satisfied(
            self.surface, self._capability.success_checkpoint, self._outputs
        ):
            return Success(
                run_id=self.run_id,
                capability=self._capability.id,
                version=self._capability.version,
                outputs=dict(self._outputs),
                model_calls=0,
            )
        return Failure(
            run_id=self.run_id,
            code=FailureCode.CHECKPOINT_FAILED,
            step_id=None,
            expected=repr(self._capability.success_checkpoint),
            observed=await _observe(self.surface, self._routes),
            retryable=False,
            model_calls=0,
        )

    def _nearest_prior_checkpoint(self) -> tuple[Step, Condition] | None:
        """The closest already-executed step below the cursor that has a postcondition."""
        for index in range(self._cursor - 1, -1, -1):
            step = self._capability.steps[index]
            if step.postcondition is not None:
                return step, step.postcondition
        return None

    async def assess_reconciliation(self) -> RunResult | None:
        """Judge whether automation may safely resume, without changing ownership.

        Returns ``None`` when it is safe to resume from the current cursor. Otherwise
        it fails closed: an ``Escalated`` when the blocking state remains (the session
        stays paused and resumable, so control should be retained by the human), or a
        typed ``Failure`` when navigation scope is now invalid, there is no prior
        checkpoint to trust, or the nearest prior checkpoint no longer holds. The
        resume cursor is derived from the capability's own checkpoints against the live
        page, never from ``cursor + 1``. It polls within the resolve budget so a human
        action that navigates is allowed to settle before its state is judged.
        """
        pending = self.pending
        if pending is None or self._cursor >= len(self._capability.steps):
            return None  # nothing paused; resuming is a normal advance/finish
        step = self._capability.steps[self._cursor]
        checkpoint = self._nearest_prior_checkpoint()
        if checkpoint is None:
            # No trustworthy established state to reconcile against: do not resume.
            self.pending = None
            return Failure(
                run_id=self.run_id,
                code=FailureCode.CHECKPOINT_FAILED,
                step_id=step.id,
                expected="a prior checkpoint to reconcile against",
                retryable=False,
                model_calls=0,
            )
        checkpoint_step, condition = checkpoint
        try:
            await self.surface.wait_settled()
            denied = _nav_denied(
                self.run_id, self._nav_policy, await self.surface.scope_urls(), step.id
            )
            if denied is not None:
                self.pending = None
                return denied
            dialog_present, checkpoint_holds = await self._await_reconciliation(condition)
        except SurfaceError as error:
            self.pending = None
            return _surface_failure(self.run_id, error, step.id)
        if dialog_present:
            # The human did not clear the blocking state; stay paused/resumable.
            return self._escalate(pending.reason, step)
        if not checkpoint_holds:
            # The established checkpoint no longer holds (the page moved during human
            # control): refuse to blindly run the pending step.
            self.pending = None
            return Failure(
                run_id=self.run_id,
                code=FailureCode.CHECKPOINT_FAILED,
                step_id=checkpoint_step.id,
                expected=repr(condition),
                observed=await _observe(self.surface, self._routes),
                retryable=False,
                model_calls=0,
            )
        return None  # reconciled: the checkpoint holds and the blocking state is gone

    async def resume_from_cursor(self) -> RunResult:
        """Continue automation from the current cursor after a successful reconcile."""
        self.pending = None
        return await self.advance()

    async def _await_reconciliation(self, condition: Condition) -> tuple[bool, bool]:
        """Poll until the blocking state clears and the checkpoint holds, or budget ends.

        Returns ``(dialog_present, checkpoint_holds)`` describing the final observed
        state so the caller can distinguish "still blocked" from "checkpoint failed".
        """
        waited = 0
        dialog_present = True
        checkpoint_holds = False
        while True:
            try:
                dialog_present = await self.surface.has_blocking_dialog()
                checkpoint_holds = (not dialog_present) and await _matches(self.surface, condition)
            except SurfaceTransientError:
                pass  # mid-navigation after the human action; keep polling
            if checkpoint_holds or waited >= self._resolve_timeout_ms:
                return dialog_present, checkpoint_holds
            await asyncio.sleep(0.1)
            waited += 100

    async def run_to_completion(self) -> RunResult:
        """Advance to a terminal result. A pause (Escalated) is terminal here: without
        an operator wired to take over, the intervention is reported and the run ends."""
        opened = await self.start()
        if opened is not None:
            return opened
        return await self.advance()

    async def close(self) -> None:
        if self.owns_surface:
            try:
                await self.surface.close()
            except SurfaceError:
                pass  # a close error must never mask the RunResult
