"""Deterministic replay: execute a compiled Capability with no model in the loop.

Replay drives the same TrustedKernel as discovery. It never constructs or calls a
discovery model, so the reported model_calls is always 0. Runtime conditions are
handled deliberately: a transient surface condition is retried under a bounded
budget, a declared business outcome is a legitimate domain answer, a satisfied
checkpoint continues, and anything else stops with a typed Failure. No raw driver
exception crosses this boundary.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from computer_use.model import (
    BusinessOutcome,
    Capability,
    Condition,
    Failure,
    FailureCode,
    OutcomeClass,
    OutputSpec,
    ParamType,
    PolicyEffect,
    ProposedActionType,
    RunResult,
    Step,
    Success,
)
from computer_use.safety import (
    NavigationPolicy,
    Policy,
    RiskClassifier,
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

from .kernel import KernelExecution, KernelRejection, RejectionCode, TrustedKernel, ValueResolver

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
# missing locator is returned as a fail-closed typed Failure rather than acted on;
# routing such a stop to a human operator is a separate concern.
_REJECTION_TO_FAILURE = {
    RejectionCode.TARGET_MISSING: FailureCode.TARGET_MISSING,
    RejectionCode.LOCATOR_AMBIGUOUS: FailureCode.LOCATOR_AMBIGUOUS,
    RejectionCode.POLICY_DENIED: FailureCode.POLICY_DENIED,
    RejectionCode.RISK_CONFIRMATION_REQUIRED: FailureCode.POLICY_DENIED,
}


def _failure_code(code: RejectionCode) -> FailureCode:
    return _REJECTION_TO_FAILURE.get(code, FailureCode.POLICY_DENIED)


def _surface_failure(run_id: str, error: SurfaceError, step_id: str | None) -> Failure:
    """Map any Surface-level error to a typed hard Failure; nothing raw escapes replay.

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
    run_id: str, nav_policy: NavigationPolicy, url: str, step_id: str | None
) -> Failure | None:
    """A typed Failure if the URL is out of navigation scope, else None."""
    decision = nav_policy.check(url)
    if decision.effect is PolicyEffect.DENY:
        return Failure(
            run_id=run_id,
            code=FailureCode.POLICY_DENIED,
            step_id=step_id,
            observed=f"navigation out of scope: {decision.reason}",
            retryable=False,
            model_calls=0,
        )
    return None


def _coerce_output(value: str, spec: OutputSpec | None) -> str:
    if spec is not None and spec.type is ParamType.DECIMAL:
        return value.replace("$", "").replace(",", "").strip()
    return value


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


async def _execute_step(kernel: TrustedKernel, step: Step) -> KernelExecution:
    """Execute a step, retrying only a classified transient condition within budget."""
    last: SurfaceTransientError | None = None
    for _ in range(_TRANSIENT_RETRIES):
        try:
            return await kernel.execute_step(step)
        except SurfaceTransientError as transient:
            last = transient
            await asyncio.sleep(_RETRY_BACKOFF_MS / 1000)
    assert last is not None
    raise last


async def replay(
    capability: Capability,
    inputs: dict[str, str],
    target_url: str,
    safe_clicks: frozenset[str] = frozenset(),
    surface: Surface | None = None,
    resolve_timeout_ms: int = _RESOLVE_TIMEOUT_MS,
    nav_policy: NavigationPolicy | None = None,
) -> RunResult:
    run_id = f"run_{uuid4().hex[:8]}"
    # Caller-owned session (the human-handoff seam): if a surface is injected the
    # caller owns its lifecycle; otherwise replay creates and closes its own.
    owns_surface = surface is None
    active: Surface = surface if surface is not None else PlaywrightSurface()
    routes = nav_policy.allowed_routes if nav_policy is not None else frozenset()
    try:
        try:
            if owns_surface:
                await active.start()
            entry = target_url.rstrip("/") + "/"
            if nav_policy is not None:
                denied = _nav_denied(run_id, nav_policy, entry, None)
                if denied is not None:
                    return denied
            await active.goto(entry)
            kernel = TrustedKernel(
                active,
                Policy(allowed_actions=_REPLAY_ACTIONS),
                RiskClassifier(safe_click_names=safe_clicks),
                ValueResolver(inputs),
            )
            outputs: dict[str, str] = {}
            for step in capability.steps:
                await active.wait_settled()
                if nav_policy is not None:
                    denied = _nav_denied(run_id, nav_policy, await active.current_url(), step.id)
                    if denied is not None:
                        return denied
                try:
                    execution = await _execute_step(kernel, step)
                    if step.output is not None and execution.extracted is not None:
                        outputs[step.output] = _coerce_output(
                            execution.extracted, capability.outputs.get(step.output)
                        )
                    # An action may navigate; re-check scope on the resulting page
                    # before reading it for an outcome or checkpoint.
                    if nav_policy is not None:
                        await active.wait_settled()
                        post = _nav_denied(run_id, nav_policy, await active.current_url(), step.id)
                        if post is not None:
                            return post
                    kind, code = await _resolve_step(active, step, resolve_timeout_ms)
                except KernelRejection as rejection:
                    return Failure(
                        run_id=run_id,
                        code=_failure_code(rejection.code),
                        step_id=step.id,
                        observed=await _observe(active, routes),
                        retryable=False,
                        model_calls=0,
                    )
                except SurfaceError as error:
                    # Driver failure, exhausted transient, or a mid-act target race.
                    return _surface_failure(run_id, error, step.id)
                if kind == "outcome":
                    return BusinessOutcome(
                        run_id=run_id, capability=capability.id, code=code or "", model_calls=0
                    )
                if kind == "failed":
                    return Failure(
                        run_id=run_id,
                        code=FailureCode.CHECKPOINT_FAILED,
                        step_id=step.id,
                        expected=repr(step.postcondition),
                        observed=await _observe(active, routes),
                        retryable=False,
                        model_calls=0,
                    )
            if nav_policy is not None:
                denied = _nav_denied(run_id, nav_policy, await active.current_url(), None)
                if denied is not None:
                    return denied
            if await _success_satisfied(active, capability.success_checkpoint, outputs):
                return Success(
                    run_id=run_id,
                    capability=capability.id,
                    version=capability.version,
                    outputs=outputs,
                    model_calls=0,
                )
            return Failure(
                run_id=run_id,
                code=FailureCode.CHECKPOINT_FAILED,
                step_id=None,
                expected=repr(capability.success_checkpoint),
                observed=await _observe(active, routes),
                retryable=False,
                model_calls=0,
            )
        except SurfaceError as error:
            # start / goto / success-check errors not tied to a specific step.
            return _surface_failure(run_id, error, None)
    finally:
        if owns_surface:
            try:
                await active.close()
            except SurfaceError:
                pass  # a close error must never mask the RunResult
