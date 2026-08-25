"""The `cua` CLI: `discover` (genuine model run) and `replay` (no model)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer
from pydantic import ValidationError

from computer_use.discovery import GoalSpec, OutcomeBinding, compile_capability, discover
from computer_use.discovery.anthropic_model import DEFAULT_MODEL, AnthropicDiscoveryModel
from computer_use.execution import TrustedKernel, ValueResolver, replay
from computer_use.model import (
    Capability,
    CapabilityTarget,
    Condition,
    Failure,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParamType,
    ProposedActionType,
    RunResult,
    Sensitivity,
    TargetDescriptor,
)
from computer_use.observability import (
    EvidenceCollector,
    EvidencePolicy,
    EvidenceStore,
    FailureEvidence,
    persistable_result,
)
from computer_use.safety import EnvSecretProvider, NavigationPolicy, Policy, RiskClassifier
from computer_use.surface import PlaywrightSurface

app = typer.Typer(add_completion=False, help="Computer-use discovery and replay.")

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)
# Explicit known-safe read-only click for this capability (a member lookup).
_SAFE_CLICKS = frozenset({"Search"})
# Trusted navigation scope: the origins the agent may operate on. This is operator
# configuration, deliberately NOT derived from the caller-supplied --target, so a
# target cannot define its own allowed scope. An off-scope target is refused by the
# runtime before any action.
_ALLOWED_ORIGINS = frozenset({"http://localhost:8000", "http://127.0.0.1:8000"})
# The routes this capability is scoped to on the target host.
_ALLOWED_ROUTES = frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"})


def _nav_policy() -> NavigationPolicy:
    return NavigationPolicy(allowed_origins=_ALLOWED_ORIGINS, allowed_routes=_ALLOWED_ROUTES)


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
            OutcomeBinding(
                action=ProposedActionType.CLICK,
                target=TargetDescriptor(role="button", name="Search"),
                outcome=Outcome(
                    code="MEMBER_NOT_FOUND",
                    outcome_class=OutcomeClass.BUSINESS_OUTCOME,
                    detector=Condition(text_present="Member record not found"),
                ),
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
            ValueResolver(inputs, EnvSecretProvider()),
        )
        outcome = await discover(
            AnthropicDiscoveryModel(model=model_id),
            surface,
            kernel,
            spec,
            target,
            evidence=store,
            nav_policy=_nav_policy(),
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


async def _run_replay(
    capability: Capability, params: dict[str, str], target: str
) -> tuple[RunResult, FailureEvidence | None]:
    # The CLI owns the session (Playwright injection seam) so, on failure, it can
    # collect sanitized structural evidence from the still-open surface.
    surface = PlaywrightSurface()
    await surface.start()
    try:
        result = await replay(
            capability,
            params,
            target,
            safe_clicks=_SAFE_CLICKS,
            surface=surface,
            nav_policy=_nav_policy(),
            secrets=EnvSecretProvider(),
        )
        failure_evidence: FailureEvidence | None = None
        if isinstance(result, Failure):
            collector = EvidenceCollector(EvidencePolicy(), _ALLOWED_ROUTES)
            failure_evidence = await collector.collect_failure_evidence(
                surface, await surface.current_route()
            )
        return result, failure_evidence
    finally:
        await surface.close()


@app.command("replay")
def replay_command(
    artifact: str = typer.Argument(...),
    param: list[str] = typer.Option([], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
    evidence_out: str | None = typer.Option(None, "--evidence-out"),
) -> None:
    try:
        capability = Capability.model_validate_json(Path(artifact).read_text(encoding="utf-8"))
    except ValidationError as error:
        # Static validation runs on load, so an invalid or hand-edited artifact is
        # rejected before it can drive the browser.
        typer.echo(f"invalid capability artifact: {error}")
        raise typer.Exit(code=1) from error
    result, failure_evidence = asyncio.run(_run_replay(capability, _parse_params(param), target))
    # stdout is the caller's deliverable: the raw typed result.
    typer.echo(result.model_dump_json())
    # persisted evidence is masked: sensitive outputs and route params are redacted.
    if evidence_out is not None:
        payload: dict[str, object] = {"result": persistable_result(result, capability)}
        if failure_evidence is not None:
            payload["failure_evidence"] = failure_evidence.model_dump(mode="json")
        Path(evidence_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
