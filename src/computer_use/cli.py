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
from computer_use.execution import ControlLease, ReplaySession, TrustedKernel, ValueResolver, replay
from computer_use.handoff import (
    ClickControl,
    InterventionRequest,
    OperatorController,
    OperatorError,
    OperatorScopeError,
    TypeControl,
)
from computer_use.model import (
    Capability,
    CapabilityTarget,
    Condition,
    Escalated,
    Failure,
    InputSpec,
    Outcome,
    OutcomeClass,
    OutputSpec,
    ParamType,
    ProposedActionType,
    RunResult,
    Sensitivity,
    Success,
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
from computer_use.surface import PlaywrightSurface, SurfaceError

app = typer.Typer(add_completion=False, help="Computer-use discovery and replay.")


def _load_dotenv(path: str = ".env") -> None:
    """Load simple KEY=VALUE lines from a local .env into the environment.

    Dependency-free and non-overriding: an already-set variable wins, and only the
    model key belongs here. The file is git-ignored; secrets never enter the repo.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)

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


async def _discovery_operator_console(operator: OperatorController, evidence_dir: Path) -> bool:
    """Block for real operator input during discovery; return True once resolved.

    The human takes exclusive control of the same live session, performs bounded
    actions to clear the block (e.g. entering an employee verification code), then
    resumes — after which discovery re-observes and the model continues.
    """
    request = await operator.raise_intervention()
    (evidence_dir / "intervention.json").write_text(
        request.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    typer.echo("")
    typer.echo("=== Intervention required (discovery) ===")
    typer.echo(f"  id:         {request.intervention_id}")
    typer.echo(f"  capability: {request.capability}")
    typer.echo(f"  reason:     {request.reason.value}")
    typer.echo(f"  control:    {request.control_owner.value} (epoch {request.control_epoch})")
    typer.echo(f"  landmarks:  {request.evidence.landmarks}")
    controls = await operator.visible_controls()
    typer.echo(f"  controls:   {controls}")
    typer.echo("")
    typer.echo("You now hold nothing yet. To resolve this on the SAME live session:")
    typer.echo("  1) 'take'  — grab exclusive control")
    typer.echo("  2) resolve it, either way:")
    typer.echo("       • directly in the browser window (type the code, press Enter), or")
    typer.echo("       • 'submit <field>=<value>' to do it through the audited console")
    typer.echo("         e.g.  submit Employee Verification Code=4729")
    typer.echo("  3) 'resume' — hand control back so the model continues")
    typer.echo("commands: take | submit <field>=<value> | type <field>=<value> | "
               "click <name> | resume | status | help | quit")
    while True:
        try:
            line = (await asyncio.to_thread(input, "operator> ")).strip()
        except EOFError:
            return False
        if not line:
            continue
        command, _, rest = line.partition(" ")
        rest = rest.strip()
        try:
            if command == "take":
                epoch = operator.take_control()
                typer.echo(f"control -> HUMAN (epoch {epoch}); automation is blocked")
            elif command in ("type", "submit") and "=" in rest:
                field, _, value = rest.partition("=")
                await operator.perform(
                    TypeControl(
                        TargetDescriptor(role="textbox", name=field.strip()),
                        value,
                        submit=(command == "submit"),
                    )
                )
                verb = "submitted" if command == "submit" else "typed into"
                typer.echo(f"{verb} {field.strip()!r} (value recorded as redacted)")
            elif command == "click" and rest:
                await operator.perform(ClickControl(TargetDescriptor(role="button", name=rest)))
                typer.echo(f"clicked {rest!r} on the live session")
            elif command == "resume":
                operator.release_to_automation()
                typer.echo("control -> AUTOMATION; discovery will re-observe and continue")
                return True
            elif command == "status":
                typer.echo("(operator holds control)" if rest == "" else "")
            elif command in ("help", "?"):
                typer.echo("commands: take | type <field>=<value> | click <name> | resume | quit")
            elif command in ("quit", "abort"):
                return False
            else:
                typer.echo(f"unknown or malformed command: {line!r}")
        except (OperatorError, OperatorScopeError) as error:
            typer.echo(f"error: {error}")
        except SurfaceError:
            # A mistyped field/control name resolves to nothing; do not crash the run.
            controls = await operator.visible_controls()
            typer.echo(f"error: no such control on the page. available: {controls}")


async def _run_discover(
    goal: str, inputs: dict[str, str], target: str, model_id: str, out: str, evidence: str,
    scenario: str, headed: bool,
) -> int:
    spec = _capability_a_spec(goal)
    store = EvidenceStore(evidence)
    evidence_dir = Path(evidence).parent
    surface = PlaywrightSurface(headless=not headed)
    lease = ControlLease()

    async def on_human_request(operator: OperatorController) -> bool:
        return await _discovery_operator_console(operator, evidence_dir)

    await surface.start()
    try:
        # Arm a runtime scenario for the whole discovery flow (sets the cookie).
        if scenario and scenario != "normal":
            await surface.goto(f"{target.rstrip('/')}/?scenario={scenario}")
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=_SAFE_CLICKS),
            ValueResolver(inputs, EnvSecretProvider()),
            lease=lease,
        )
        outcome = await discover(
            AnthropicDiscoveryModel(model=model_id),
            surface,
            kernel,
            spec,
            target,
            evidence=store,
            nav_policy=_nav_policy(),
            lease=lease,
            on_human_request=on_human_request,
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
    scenario: str = typer.Option("normal", "--scenario"),
    headed: bool = typer.Option(False, "--headed/--headless"),
) -> None:
    _load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        typer.echo("ANTHROPIC_API_KEY is not set; discovery requires a model key.")
        raise typer.Exit(code=1)
    code = asyncio.run(
        _run_discover(goal, _parse_params(param), target, model, out, evidence, scenario, headed)
    )
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


_HANDOFF_HELP = (
    "commands: take (take control) | ack (acknowledge the notice) | "
    "resume (hand back to automation) | status | help | quit"
)


def _print_intervention(request: InterventionRequest) -> None:
    typer.echo("")
    typer.echo("=== Intervention required ===")
    typer.echo(f"  id:         {request.intervention_id}")
    typer.echo(f"  capability: {request.capability} v{request.version}")
    typer.echo(f"  step:       {request.step_id}")
    typer.echo(f"  reason:     {request.reason.value}")
    typer.echo(f"  control:    {request.control_owner.value} (epoch {request.control_epoch})")
    typer.echo(f"  route:      {request.route}")
    typer.echo(f"  landmarks:  {request.evidence.landmarks}")
    typer.echo(_HANDOFF_HELP)


async def _operator_console(
    operator: OperatorController, session: ReplaySession
) -> RunResult | None:
    """Block for real operator input; drive the same live session; return the result."""
    while True:
        try:
            line = (await asyncio.to_thread(input, "operator> ")).strip()
        except EOFError:
            return None
        if not line:
            continue
        command, *_ = line.split()
        if command == "take":
            try:
                epoch = operator.take_control()
                typer.echo(f"control -> HUMAN (epoch {epoch}); automation is now blocked")
            except OperatorError as error:
                typer.echo(f"error: {error}")
        elif command == "ack":
            try:
                await operator.perform(
                    ClickControl(TargetDescriptor(role="link", name="Acknowledge"))
                )
                typer.echo("acknowledged on the live session (human action recorded)")
            except (OperatorError, OperatorScopeError, SurfaceError) as error:
                typer.echo(f"error: {error}")
        elif command == "resume":
            try:
                result = await operator.resume()
            except OperatorError as error:
                typer.echo(f"error: {error}")
                continue
            if isinstance(result, Escalated):
                typer.echo("still blocked; resolve the notice ('ack') then 'resume' again")
                continue
            return result
        elif command == "status":
            typer.echo(
                f"owner={session.lease.owner.value} epoch={session.lease.epoch}"
            )
        elif command in ("help", "?"):
            typer.echo(_HANDOFF_HELP)
        elif command in ("quit", "abort"):
            return None
        else:
            typer.echo(f"unknown command: {command!r}")
            typer.echo(_HANDOFF_HELP)


async def _run_handoff_demo(
    artifact: str, params: dict[str, str], target: str, scenario: str, evidence_dir: str,
    headed: bool,
) -> int:
    capability = Capability.model_validate_json(Path(artifact).read_text(encoding="utf-8"))
    out = Path(evidence_dir)
    out.mkdir(parents=True, exist_ok=True)
    actions = EvidenceStore(out / "actions.jsonl")
    surface = PlaywrightSurface(headless=not headed)
    await surface.start()
    try:
        # Arm the runtime scenario for the whole flow, then drive the capability until
        # it either completes or pauses for a human.
        await surface.goto(f"{target.rstrip('/')}/?scenario={scenario}")
        session = ReplaySession(
            capability, params, target,
            nav_policy=_nav_policy(), safe_clicks=_SAFE_CLICKS, surface=surface,
            secrets=EnvSecretProvider(),
        )
        opened = await session.start()
        if opened is not None:
            typer.echo(opened.model_dump_json())
            return 1
        result = await session.advance()
        if not isinstance(result, Escalated):
            typer.echo("no intervention was required for this run")
            typer.echo(result.model_dump_json())
            return 0 if isinstance(result, Success) else 1
        operator = OperatorController(session, evidence=actions)
        request = await operator.raise_intervention()
        (out / "intervention.json").write_text(
            request.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _print_intervention(request)
        final = await _operator_console(operator, session)
        if final is None:
            typer.echo("aborted before completion")
            return 1
        typer.echo(final.model_dump_json())
        (out / "result.json").write_text(
            json.dumps({"result": persistable_result(final, capability)}, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0 if isinstance(final, Success) else 1
    finally:
        await surface.close()


@app.command("handoff-demo")
def handoff_demo_command(
    artifact: str = typer.Argument("artifacts/member_lookup.v1.json"),
    param: list[str] = typer.Option(["member_number=12345"], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
    scenario: str = typer.Option("unexpected_dialog", "--scenario"),
    evidence_out: str = typer.Option("evidence/replay_handoff", "--evidence-out"),
    headed: bool = typer.Option(True, "--headed/--headless"),
) -> None:
    """Replay a capability, pause on an unhandled dialog, and hand the live session
    to a human operator who resolves it and resumes automation."""
    code = asyncio.run(
        _run_handoff_demo(artifact, _parse_params(param), target, scenario, evidence_out, headed)
    )
    raise typer.Exit(code=code)
