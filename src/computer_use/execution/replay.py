"""Deterministic replay: execute a compiled Capability with no model in the loop.

Replay drives the same TrustedKernel as discovery. It never constructs or calls a
discovery model, so the reported model_calls is always 0. After a step it races the
step's declared business outcomes against its checkpoint: a matching outcome detector
returns a BusinessOutcome (a legitimate domain answer), a satisfied checkpoint
continues, and neither within the timeout is a hard CHECKPOINT_FAILED.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from computer_use.model import (
    BusinessOutcome,
    Capability,
    Condition,
    Failure,
    OutcomeClass,
    OutputSpec,
    ParamType,
    ProposedActionType,
    RunResult,
    Step,
    Success,
)
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface, Surface

from .kernel import KernelRejection, TrustedKernel, ValueResolver

_REPLAY_ACTIONS = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
_RESOLVE_TIMEOUT_MS = 5000


def _coerce_output(value: str, spec: OutputSpec | None) -> str:
    if spec is not None and spec.type is ParamType.DECIMAL:
        return value.replace("$", "").replace(",", "").strip()
    return value


async def _matches(surface: Surface, condition: Condition) -> bool:
    if condition.text_present is not None:
        return await surface.has_text(condition.text_present)
    if condition.heading is not None:
        return await surface.has_text(condition.heading.name)
    if condition.any_of is not None:
        for sub_condition in condition.any_of:
            if await _matches(surface, sub_condition):
                return True
    return False


async def _resolve_step(surface: Surface, step: Step) -> tuple[str, str | None]:
    """Race the step's business outcomes against its checkpoint after execution."""
    if not step.outcomes and step.postcondition is None:
        return ("continue", None)
    waited = 0
    while True:
        for outcome in step.outcomes:
            if outcome.outcome_class is OutcomeClass.BUSINESS_OUTCOME and await _matches(
                surface, outcome.detector
            ):
                return ("outcome", outcome.code)
        if step.postcondition is None or await _matches(surface, step.postcondition):
            return ("continue", None)
        if waited >= _RESOLVE_TIMEOUT_MS:
            return ("failed", None)
        await asyncio.sleep(0.1)
        waited += 100


async def replay(
    capability: Capability,
    inputs: dict[str, str],
    target_url: str,
    safe_clicks: frozenset[str] = frozenset(),
) -> RunResult:
    run_id = f"run_{uuid4().hex[:8]}"
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(target_url.rstrip("/") + "/")
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_REPLAY_ACTIONS),
            RiskClassifier(safe_click_names=safe_clicks),
            ValueResolver(inputs),
        )
        outputs: dict[str, str] = {}
        for step in capability.steps:
            await surface.wait_settled()
            try:
                execution = await kernel.execute_step(step)
            except KernelRejection as rejection:
                return Failure(
                    run_id=run_id,
                    code=rejection.code.value,
                    step_id=step.id,
                    retryable=False,
                    model_calls=0,
                )
            if step.output is not None and execution.extracted is not None:
                outputs[step.output] = _coerce_output(
                    execution.extracted, capability.outputs.get(step.output)
                )
            kind, code = await _resolve_step(surface, step)
            if kind == "outcome":
                return BusinessOutcome(
                    run_id=run_id, capability=capability.id, code=code or "", model_calls=0
                )
            if kind == "failed":
                return Failure(
                    run_id=run_id,
                    code="CHECKPOINT_FAILED",
                    step_id=step.id,
                    expected=repr(step.postcondition),
                    retryable=False,
                    model_calls=0,
                )
        expected_output = capability.success_checkpoint.output_present
        if expected_output is not None and expected_output in outputs:
            return Success(
                run_id=run_id,
                capability=capability.id,
                version=capability.version,
                outputs=outputs,
                model_calls=0,
            )
        return Failure(
            run_id=run_id,
            code="CHECKPOINT_FAILED",
            step_id=None,
            expected=f"output '{expected_output}' present",
            retryable=False,
            model_calls=0,
        )
    finally:
        await surface.close()
