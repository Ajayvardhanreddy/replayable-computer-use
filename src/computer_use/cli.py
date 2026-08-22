"""The `cua` CLI: `discover` (genuine model run) and `replay` (no model)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from computer_use.discovery import GoalSpec, compile_capability, discover
from computer_use.discovery.anthropic_model import DEFAULT_MODEL, AnthropicDiscoveryModel
from computer_use.execution import TrustedKernel, ValueResolver, replay
from computer_use.model import (
    Capability,
    CapabilityTarget,
    Condition,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParamType,
    ProposedActionType,
    Sensitivity,
)
from computer_use.observability import EvidenceStore
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface

app = typer.Typer(add_completion=False, help="Computer-use discovery and replay.")

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
# Explicit known-safe read-only click for this capability (a member lookup).
_SAFE_CLICKS = frozenset({"Search"})


def _capability_a_spec(goal: str) -> GoalSpec:
    return GoalSpec(
        capability_id="member.lookup_savings_balance",
        goal=goal,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, sensitivity=Sensitivity.FINANCIAL, currency="USD"
            )
        },
        success_output="savings_balance",
        business_outcomes=[
            Outcome(
                code="MEMBER_NOT_FOUND",
                outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                detector=Condition(text_present="Member record not found"),
            )
        ],
    )


def _parse_params(params: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for item in params:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got {item!r}")
        key, value = item.split("=", 1)
        inputs[key] = value
    return inputs


async def _run_discover(
    goal: str, inputs: dict[str, str], target: str, model_id: str, out: str, evidence: str
) -> int:
    spec = _capability_a_spec(goal)
    store = EvidenceStore(evidence)
    surface = PlaywrightSurface()
    await surface.start()
    try:
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver(inputs),
        )
        outcome = await discover(
            AnthropicDiscoveryModel(model=model_id), surface, kernel, spec, target, evidence=store
        )
    finally:
        await surface.close()
    if outcome.stop_reason != "GOAL_REACHED":
        typer.echo(f"discovery did not reach the goal: {outcome.stop_reason}")
        return 1
    capability = compile_capability(outcome.trace, spec)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(capability.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(
        json.dumps(
            {
                "artifact": out,
                "model": model_id,
                "model_calls": outcome.model_calls,
                "stop_reason": outcome.stop_reason,
            }
        )
    )
    return 0


@app.command("discover")
def discover_command(
    goal: str = typer.Option(..., "--goal"),
    param: list[str] = typer.Option([], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    out: str = typer.Option("artifacts/member_lookup.v1.json", "--out"),
    evidence: str = typer.Option("evidence/discovery/trace.jsonl", "--evidence"),
) -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        typer.echo("ANTHROPIC_API_KEY is not set; discovery requires a model key.")
        raise typer.Exit(code=1)
    code = asyncio.run(_run_discover(goal, _parse_params(param), target, model, out, evidence))
    raise typer.Exit(code=code)


@app.command("replay")
def replay_command(
    artifact: str = typer.Argument(...),
    param: list[str] = typer.Option([], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
) -> None:
    capability = Capability.model_validate_json(Path(artifact).read_text(encoding="utf-8"))
    result = asyncio.run(
        replay(capability, _parse_params(param), target, safe_clicks=_SAFE_CLICKS)
    )
    typer.echo(result.model_dump_json())
