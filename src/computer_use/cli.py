"""The `cua` CLI: `discover` (genuine model run) and `replay` (no model)."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import httpx
import typer
from pydantic import ValidationError

from computer_use.discovery import (
    DiscoveryTrace,
    GoalSpec,
    OutcomeBinding,
    compile_capability,
    discover,
    verification_provenance,
)
from computer_use.discovery.anthropic_model import DEFAULT_MODEL, AnthropicDiscoveryModel
from computer_use.discovery.compiler import CapabilityValidationError
from computer_use.execution import (
    ApprovalGrant,
    ApprovalRequest,
    ControlLease,
    ReplaySession,
    TrustedKernel,
    ValueResolver,
)
from computer_use.handoff import (
    ClickControl,
    InterventionRequest,
    OperatorController,
    OperatorError,
    OperatorScopeError,
    TypeControl,
    operator_ui,
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
    ParameterRef,
    ParamType,
    ProposedActionType,
    RunResult,
    SafeLiteral,
    SecretRef,
    Sensitivity,
    Step,
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
from computer_use.safety import (
    AuthorityPolicy,
    ConfirmationPolicy,
    EnvSecretProvider,
    NavigationPolicy,
    Policy,
    RiskClassifier,
)
from computer_use.surface import BlockerObservation, PlaywrightSurface, SurfaceError

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
# Explicit known-safe read-only click for this capability (a member lookup). Only the
# search action; re-navigating is not part of the happy path, so a click elsewhere is a
# genuine block that steers a stuck model to request_human rather than letting it wander.
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


# Capability B (open a sub-account): navigation scope and known-safe read-only clicks.
# "Create Account" is deliberately absent — it is the consequential commit and must be
# authorized, never classified safe. "Member Inquiry" is the persistent shell nav the
# model reuses to independently re-derive the member's accounts after the commit.
_ALLOWED_ROUTES_B = _ALLOWED_ROUTES | frozenset(
    {"/workspace/member/:member_number/sub-account"}
)
_SAFE_CLICKS_B = frozenset({"Search", "Open Sub-Account", "Member Inquiry"})


def _capability_b_spec(goal: str) -> GoalSpec:
    # A write capability. The declared output is the new sub-account's status, which the
    # model can obtain only by independently re-deriving the member's accounts after the
    # commit — so software ratifies success on an independent read, not the commit echo.
    return GoalSpec(
        capability_id="member.open_sub_account",
        goal=goal,
        target=CapabilityTarget(vendor="legacy_core", application_family="core_banking"),
        inputs={"member_number": InputSpec(type=ParamType.STRING, sensitivity=Sensitivity.PII)},
        outputs={"sub_account_status": OutputSpec(type=ParamType.STRING)},
        success_output="sub_account_status",
    )


@dataclass(frozen=True)
class _CapabilityProfile:
    """The application-specific inputs to a discovery run. Kept out of the generic
    compiler and kernel: the demo/target knowledge lives only at this composition root."""

    spec: GoalSpec
    safe_clicks: frozenset[str]
    nav_policy: NavigationPolicy
    # A read-only capability has no legitimate consequential action, so a click the
    # classifier flags is a genuine block -> the model is steered to request_human. Only
    # a capability that really performs a write asks a human to authorize that one action.
    has_write: bool


def _capability_profile(name: str, goal: str) -> _CapabilityProfile:
    if name == "member_lookup":
        return _CapabilityProfile(
            _capability_a_spec(goal), _SAFE_CLICKS, _nav_policy(), has_write=False
        )
    if name == "open_sub_account":
        return _CapabilityProfile(
            _capability_b_spec(goal),
            _SAFE_CLICKS_B,
            NavigationPolicy(allowed_origins=_ALLOWED_ORIGINS, allowed_routes=_ALLOWED_ROUTES_B),
            has_write=True,
        )
    raise typer.BadParameter(
        f"unknown capability {name!r}; expected member_lookup|open_sub_account"
    )


async def _discovery_approval_console(request: ApprovalRequest) -> ApprovalGrant | None:
    """Ask the operator to authorize one consequential action on the live session.

    The trusted kernel raised this requirement; it never prompts. A 'yes' returns a
    one-time grant bound to this operation's fingerprint, which the kernel revalidates
    immediately before dispatch. The model can never reach or satisfy this itself.
    """
    fp = request.fingerprint
    if fp.row_contains is not None:
        target = f"cell[{fp.row_contains}/{fp.column_header}]"
    elif fp.target_name:
        target = f"{fp.target_role or 'element'}:{fp.target_name}"
    else:
        target = fp.target_role or "element"
    typer.echo("")
    typer.echo("=== Consequential action requires authorization ===")
    typer.echo(f"  action:  {fp.action}")
    typer.echo(f"  target:  {target}")
    typer.echo(f"  risk:    {request.risk.value}")
    if fp.landmark:
        typer.echo(f"  context: {fp.landmark}")
    typer.echo("The model cannot authorize this itself.")
    while True:
        try:
            answer = (await asyncio.to_thread(input, "approve this action? [y/N] ")).strip().lower()
        except EOFError:
            return None
        if answer in ("y", "yes"):
            return ApprovalGrant(proposal_nonce=request.proposal_nonce, fingerprint=fp)
        if answer in ("", "n", "no"):
            return None
        typer.echo("please answer y or n")


def _target_fingerprint(target: TargetDescriptor | None) -> str:
    if target is None:
        return "element"
    if target.table_cell is not None:
        return f"cell[{target.table_cell.row_contains}/{target.table_cell.column_header}]"
    if target.name:
        return f"{target.role or 'element'}:{target.name}"
    if target.text:
        return f"{target.role or 'element'}:text"
    return target.role or "element"


def _value_fingerprint(step: Step) -> str | None:
    value = getattr(step.action, "value", None)
    if value is None:
        return None
    if isinstance(value, ParameterRef):
        return f"<param:{value.name}>"
    if isinstance(value, SafeLiteral):
        return "<const>"
    if isinstance(value, SecretRef):
        return "<secret>"
    return "<derived>"


def _write_verification_provenance(
    artifact: Capability, trace: DiscoveryTrace, spec: GoalSpec, evidence_dir: Path
) -> None:
    """Record which discovery steps became the embedded verification (structural only),
    so a reviewer can see the model discovered the re-derivation, not the compiler."""
    prov = verification_provenance(trace, spec)
    write_step = next((s for s in artifact.steps if s.verification is not None), None)
    if prov is None or write_step is None or write_step.verification is None:
        return
    v = write_step.verification
    entries: list[dict[str, object]] = []
    for idx, vstep in zip(prov.navigate_step_indices, v.navigate, strict=True):
        entries.append({
            "from_discovery_step": idx, "action": vstep.action.type,
            "target": _target_fingerprint(vstep.target), "value": _value_fingerprint(vstep),
        })
    if v.extract is not None and prov.extract_step_index is not None:
        entries.append({
            "from_discovery_step": prov.extract_step_index, "action": "extract",
            "target": _target_fingerprint(v.extract.target), "output": v.extract.output,
        })
    payload = {
        "capability": artifact.id,
        "write_step": write_step.id,
        "write_from_discovery_step": prov.write_step_index,
        "verification": entries,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "verification_provenance.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _parse_params(params: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for item in params:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got {item!r}")
        key, value = item.split("=", 1)
        inputs[key] = value
    return inputs


async def _discovery_operator_console(
    operator: OperatorController,
    surface: PlaywrightSurface,
    evidence_dir: Path,
    agent_reason: str | None = None,
) -> bool:
    """Block for real operator input during discovery; return True once resolved.

    Same operator surface as replay. Discovery differs in one way: a model is actually running,
    so the panel may show the *agent's own* reason for escalating (clearly labelled), paired
    with a trusted structural observation of the live state. The human's visual context is the
    live (headed) browser; the terminal presents controls scoped to the blocker and generic
    actions. Nothing prescribes the fix; on hand-back discovery re-observes and the model
    continues.
    """
    request = await operator.raise_intervention()
    (evidence_dir / "intervention.json").write_text(
        request.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    operator_ui.render_intervention(
        request,
        title="INTERVENTION REQUIRED  ·  discovery",
        commands=[
            ("take", "take exclusive control of this same live session"),
            ("inspect", "re-read the current live state (route/heading/blocker)"),
            ("controls", "list controls to act on ('controls --all' for the whole page)"),
            ("status · help · quit", ""),
        ],
        agent_note=(
            agent_reason or "the running agent could not safely proceed and asked for a human"
        ),
        facts=await _observe_state(surface),
        session_id=_session_label(surface),
    )
    displayed: list[_Candidate] = []
    owner, epoch = request.control_owner.value, request.control_epoch
    first = True
    while True:
        if not first:
            operator_ui.render_turn_separator()
        first = False
        try:
            line = (await asyncio.to_thread(operator_ui.input_line, owner, epoch)).strip()
        except EOFError:
            return False
        if not line:
            continue
        command, _, rest = line.partition(" ")
        rest = rest.strip()
        if command == "take":
            before, owner = owner, "HUMAN"
            epoch = operator.take_control()
            operator_ui.render_control_transfer(
                before, owner, epoch, session_id=_session_label(surface)
            )
            operator_ui.render_human_mode()
            displayed = await _show_controls(operator, surface)
            operator_ui.render_commands()
        elif command in ("controls", "view"):
            displayed = await _show_controls(
                operator, surface, full_page=rest in ("--all", "all")
            )
        elif command == "inspect":
            operator_ui.render_observed(await _observe_state(surface))
        elif command in ("click", "ack"):
            token = rest if command == "click" else "Acknowledge"
            if await _operator_click(operator, displayed, token, epoch):
                displayed = await _after_action(operator, surface)
        elif command in ("type", "submit"):
            displayed = await _handle_type_submit(
                command, rest, operator, surface, displayed, epoch
            )
        elif command == "resume":
            before, owner = owner, "AUTOMATION"
            epoch = operator.release_to_automation()
            operator_ui.render_control_transfer(
                before, owner, epoch, session_id=_session_label(surface)
            )
            operator_ui.note("discovery will re-observe and continue")
            return True
        elif command == "status":
            operator_ui.note(f"owner={owner} epoch={epoch}")
        elif command in ("help", "?"):
            operator_ui.render_help()
        elif command in ("quit", "abort"):
            return False
        else:
            operator_ui.note(f"unknown or malformed command: {line!r}", style=operator_ui.FAIL)


async def _run_discover(
    goal: str, inputs: dict[str, str], target: str, model_id: str, out: str, evidence: str,
    scenario: str, headed: bool, capability: str = "member_lookup",
) -> int:
    profile = _capability_profile(capability, goal)
    spec = profile.spec
    store = EvidenceStore(evidence)
    evidence_dir = Path(evidence).parent
    surface = PlaywrightSurface(headless=not headed)
    lease = ControlLease()

    async def on_human_request(operator: OperatorController, reason: str | None) -> bool:
        return await _discovery_operator_console(operator, surface, evidence_dir, reason)

    await surface.start()
    try:
        # Arm a runtime scenario for the whole discovery flow (sets the cookie).
        if scenario and scenario != "normal":
            await surface.goto(f"{target.rstrip('/')}/?scenario={scenario}")
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=profile.safe_clicks),
            ValueResolver(inputs, EnvSecretProvider()),
            lease=lease,
            # Only a write capability authors a consequential action: it pauses for
            # one-time human authorization. A read-only capability never does, so a
            # flagged click there is a genuine block that steers to request_human.
            interactive_approval=profile.has_write,
        )
        outcome = await discover(
            AnthropicDiscoveryModel(model=model_id),
            surface,
            kernel,
            spec,
            target,
            evidence=store,
            nav_policy=profile.nav_policy,
            lease=lease,
            on_human_request=on_human_request,
            on_consequential_approval=_discovery_approval_console,
        )
    finally:
        await surface.close()
    if outcome.stop_reason != "GOAL_REACHED":
        typer.echo(f"discovery did not reach the goal: {outcome.stop_reason}")
        return 1
    try:
        artifact = compile_capability(outcome.trace, spec)
    except (CapabilityValidationError, ValidationError, ValueError) as error:
        # The genuine trace is already persisted; a compile gap is something to inspect,
        # not a reason to lose the run. (Read-back compilation is a later concern.)
        typer.echo(f"discovery reached the goal but compilation failed: {error}")
        typer.echo(f"raw discovery trace: {evidence}")
        return 2
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    _write_verification_provenance(artifact, outcome.trace, spec, evidence_dir)
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
    out: str = typer.Option("evidence/capability/member_lookup.v1.json", "--out"),
    evidence: str = typer.Option("evidence/discovery/trace.jsonl", "--evidence"),
    scenario: str = typer.Option("normal", "--scenario"),
    capability: str = typer.Option("member_lookup", "--capability"),
    headed: bool = typer.Option(False, "--headed/--headless"),
) -> None:
    _load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        typer.echo("ANTHROPIC_API_KEY is not set; discovery requires a model key.")
        raise typer.Exit(code=1)
    code = asyncio.run(
        _run_discover(
            goal, _parse_params(param), target, model, out, evidence, scenario, headed, capability
        )
    )
    raise typer.Exit(code=code)


@dataclass(frozen=True)
class _ReplayProfile:
    safe_clicks: frozenset[str]
    nav_policy: NavigationPolicy
    authority: AuthorityPolicy | None


def _replay_profile(name: str) -> _ReplayProfile:
    if name == "member_lookup":
        return _ReplayProfile(_SAFE_CLICKS, _nav_policy(), None)
    if name == "open_sub_account":
        return _ReplayProfile(
            _SAFE_CLICKS_B,
            NavigationPolicy(allowed_origins=_ALLOWED_ORIGINS, allowed_routes=_ALLOWED_ROUTES_B),
            AuthorityPolicy(authoritative_absence=True),
        )
    raise typer.BadParameter(
        f"unknown capability {name!r}; expected member_lookup|open_sub_account"
    )


def _replay_confirmation(capability: Capability) -> ConfirmationPolicy:
    """Operator sanction for a write replay: approve exactly the capability's own
    consequential operation. The model can never produce this; the operator does, by
    running the command against this reviewed artifact."""
    approved = frozenset(
        f"{capability.id}:v{capability.version}:{step.id}"
        for step in capability.steps
        if step.verification is not None
    )
    return ConfirmationPolicy(approved=approved)


@dataclass(frozen=True)
class _ReplayReport:
    result: RunResult
    failure_evidence: FailureEvidence | None
    # A paused run produces a full, context-carrying handoff request for a human.
    intervention: InterventionRequest | None
    effect_reason: str


async def _run_replay(
    capability: Capability, params: dict[str, str], target: str, *,
    capability_name: str = "member_lookup", scenario: str = "normal",
    commit_timeout_ms: int | None = None, headed: bool = False,
) -> _ReplayReport:
    # The CLI drives the session directly (owning the Playwright seam) so that, on a
    # paused run, it can build a context-rich intervention while the surface is open.
    profile = _replay_profile(capability_name)
    surface = PlaywrightSurface(headless=not headed)
    await surface.start()
    try:
        # Arm a runtime scenario for the whole flow (sets a server cookie); the model
        # never sees it — replay has no model, and the arming query is not egressed.
        if scenario and scenario != "normal":
            await surface.goto(f"{target.rstrip('/')}/?scenario={scenario}")
        session = ReplaySession(
            capability, params, target,
            nav_policy=profile.nav_policy, safe_clicks=profile.safe_clicks, surface=surface,
            secrets=EnvSecretProvider(), confirmation=_replay_confirmation(capability),
            authority=profile.authority, commit_timeout_ms=commit_timeout_ms,
        )
        opened = await session.start()
        if opened is not None:
            return _ReplayReport(opened, None, None, "")
        result = await session.advance()
        failure_evidence: FailureEvidence | None = None
        intervention: InterventionRequest | None = None
        if isinstance(result, Escalated):
            # A paused run is a handoff case: build the full, sanitized intervention
            # request (capability, step, why, last state) so a human knows exactly what
            # was attempted and what remains to do.
            try:
                intervention = await OperatorController(session).raise_intervention()
            except OperatorError:
                intervention = None
        elif isinstance(result, Failure):
            collector = EvidenceCollector(EvidencePolicy(), profile.nav_policy.allowed_routes)
            failure_evidence = await collector.collect_failure_evidence(
                surface, await surface.current_route()
            )
        return _ReplayReport(result, failure_evidence, intervention, session.last_effect_reason)
    finally:
        await surface.close()


def _print_handoff_case(request: InterventionRequest, effect_reason: str) -> None:
    """Render a paused run as a human-actionable handoff case (structural state only:
    no raw member id or financial value). Presentation lives in the operator UI layer."""
    operator_ui.render_handoff_case(request, effect_reason)


@app.command("replay")
def replay_command(
    artifact: str = typer.Argument(...),
    param: list[str] = typer.Option([], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
    evidence_out: str | None = typer.Option(None, "--evidence-out"),
    capability: str = typer.Option("member_lookup", "--capability"),
    scenario: str = typer.Option("normal", "--scenario"),
    commit_timeout_ms: int | None = typer.Option(None, "--commit-timeout-ms"),
    headed: bool = typer.Option(False, "--headed/--headless"),
) -> None:
    try:
        loaded = Capability.model_validate_json(Path(artifact).read_text(encoding="utf-8"))
    except ValidationError as error:
        # Static validation runs on load, so an invalid or hand-edited artifact is
        # rejected before it can drive the browser.
        typer.echo(f"invalid capability artifact: {error}")
        raise typer.Exit(code=1) from error
    report = asyncio.run(
        _run_replay(
            loaded, _parse_params(param), target, capability_name=capability,
            scenario=scenario, commit_timeout_ms=commit_timeout_ms, headed=headed,
        )
    )
    result = report.result
    # stdout is the caller's deliverable: the raw typed result.
    typer.echo(result.model_dump_json())
    # A paused run is not a dead end: present it as a handoff case a human can act on.
    if report.intervention is not None:
        _print_handoff_case(report.intervention, report.effect_reason)
    # persisted evidence is masked: sensitive outputs and route params are redacted.
    if evidence_out is not None:
        payload: dict[str, object] = {"result": persistable_result(result, loaded)}
        if report.failure_evidence is not None:
            payload["failure_evidence"] = report.failure_evidence.model_dump(mode="json")
        if report.intervention is not None:
            payload["intervention"] = report.intervention.model_dump(mode="json")
        Path(evidence_out).parent.mkdir(parents=True, exist_ok=True)
        Path(evidence_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class _Candidate(NamedTuple):
    """A page control offered to the operator, with a stable per-observation id (``c1``…)."""

    id: str
    role: str
    name: str


async def _observe_candidates(operator: OperatorController) -> list[_Candidate]:
    """Enumerate the current live controls as numbered operator candidates."""
    candidates: list[_Candidate] = []
    for index, control in enumerate(await operator.visible_controls(), start=1):
        role, _, name = control.partition(":")
        candidates.append(_Candidate(f"c{index}", role.strip(), name.strip()))
    return candidates


def _select_candidate(token: str, displayed: list[_Candidate]) -> _Candidate | str:
    """Pick the control the operator named — by id (``c17``) or by unique label. Returns the
    candidate, or an error string; a label shared by several controls fails closed (the human
    is told to use the id) rather than silently selecting the first."""
    for candidate in displayed:
        if candidate.id == token:
            return candidate
    named = [c for c in displayed if c.name.lower() == token.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        return (
            f"{token!r} matches {len(named)} controls — use its id "
            f"(e.g. {', '.join(c.id for c in named)})"
        )
    return f"no control {token!r} on this page — type 'view' to list controls"


async def _resolve_live_target(
    operator: OperatorController, chosen: _Candidate
) -> TargetDescriptor | str:
    """Re-resolve a chosen candidate against the *current* live state before acting, failing
    closed on staleness (the control is gone) or ambiguity (now duplicated) instead of acting
    on the wrong element."""
    matches = [
        c
        for c in await _observe_candidates(operator)
        if c.role == chosen.role and c.name.lower() == chosen.name.lower()
    ]
    if len(matches) == 1:
        return TargetDescriptor(role=chosen.role, name=chosen.name)
    if not matches:
        return f"{chosen.name!r} is no longer on the page — 'controls' to refresh, then retry"
    return (
        f"{chosen.name!r} now matches {len(matches)} controls — 'controls' to refresh, then retry"
    )


def _step_index(capability: Capability, step_id: str | None) -> int | None:
    for index, step in enumerate(capability.steps):
        if step.id == step_id:
            return index
    return None


def _describe_action(step: Step) -> str:
    verb = type(step.action).__name__.replace("Action", "").lower()
    target = step.target
    if target is None:
        return verb
    cell = target.table_cell
    name = target.name or (cell.row_contains if cell else None) or target.role
    return f"{verb} {name}"


def _expected_heading(capability: Capability, index: int | None) -> str | None:
    steps = capability.steps if index is None else capability.steps[: index + 1]
    for step in reversed(steps):
        if step.postcondition is not None and step.postcondition.heading is not None:
            return step.postcondition.heading.name
    return None


def _blocker_candidates(blocker: BlockerObservation) -> list[_Candidate]:
    return [
        _Candidate(f"c{i}", control.role, control.name or "")
        for i, control in enumerate(blocker.controls, start=1)
    ]


def _ui_blocker(blocker: BlockerObservation | None) -> operator_ui.Blocker | None:
    if blocker is None:
        return None
    return operator_ui.Blocker(
        blocker.role,
        blocker.name,
        blocker.text,
        [(c.id, c.role, c.name) for c in _blocker_candidates(blocker)],
    )


async def _observe_state(surface: PlaywrightSurface) -> operator_ui.InterventionFacts:
    """Observed-only facts (route/heading/blocker) from a fresh read of the live surface."""
    return operator_ui.InterventionFacts(
        route=await surface.current_route(),
        observed_heading=await surface.primary_heading(),
        blocker=_ui_blocker(await surface.observe_blocker()),
    )


async def _build_facts(
    capability: Capability, step_id: str | None, surface: PlaywrightSurface
) -> operator_ui.InterventionFacts:
    """Deterministic Expected-vs-Observed facts: Expected from the compiled artifact, Observed
    from a fresh structural read of the live surface. No model, no prose."""
    index = _step_index(capability, step_id)
    step = capability.steps[index] if index is not None else None
    prior = capability.steps[index - 1] if index is not None and index > 0 else None
    last_action = _describe_action(prior) if prior else (_describe_action(step) if step else None)
    expected_output = (
        step.output
        if step is not None and step.output
        else capability.success_checkpoint.output_present
    )
    observed = await _observe_state(surface)
    return operator_ui.InterventionFacts(
        last_action=last_action,
        expected_heading=_expected_heading(capability, index),
        expected_output=expected_output,
        route=observed.route,
        observed_heading=observed.observed_heading,
        blocker=observed.blocker,
    )


async def _show_controls(
    operator: OperatorController, surface: PlaywrightSurface, *, full_page: bool = False
) -> list[_Candidate]:
    """Observe and display the controls to act on, returning them as candidates. Scoped to the
    blocking region unless ``full_page``. Table only — the command vocabulary is shown once, on
    takeover and ``help``. The canonical handoff is headed: the visual surface is the browser."""
    blocker = None if full_page else await surface.observe_blocker()
    if blocker is not None and blocker.controls:
        candidates = _blocker_candidates(blocker)
        operator_ui.render_controls(
            [(c.id, c.role, c.name) for c in candidates], blocker=(blocker.role, blocker.name)
        )
    else:
        candidates = await _observe_candidates(operator)
        operator_ui.render_controls([(c.id, c.role, c.name) for c in candidates])
    return candidates


async def _after_action(
    operator: OperatorController, surface: PlaywrightSurface
) -> list[_Candidate]:
    """Refresh compactly after a human action: re-list controls only if a blocker remains;
    otherwise report that the blocker cleared and stop (the operator resumes from here) rather
    than dumping the whole page again."""
    blocker = await surface.observe_blocker()
    if blocker is not None and blocker.controls:
        candidates = _blocker_candidates(blocker)
        operator_ui.render_controls(
            [(c.id, c.role, c.name) for c in candidates], blocker=(blocker.role, blocker.name)
        )
        return candidates
    heading = await surface.primary_heading()
    where = f' — now at "{heading}"' if heading else ""
    operator_ui.note(
        f"blocker cleared{where}. Type 'resume' to hand back (or 'controls' to inspect the page).",
        style=operator_ui.OK,
    )
    return []


_SENSITIVE_KEYWORDS = (
    "password", "passcode", "verification", "code", "pin", "otp", "cvv", "ssn", "secret",
)


def _is_sensitive(name: str) -> bool:
    """Heuristic: a field whose label implies a credential or one-time value should never have
    its value typed inline (it would land in the terminal transcript). Errs toward masking."""
    lowered = name.lower()
    return any(keyword in lowered for keyword in _SENSITIVE_KEYWORDS)


def _parse_field_value(rest: str) -> tuple[str, str | None] | None:
    """Parse a ``type``/``submit`` argument into ``(field, value | None)``. A multi-word control
    label needs ``=`` to separate name from value (``Employee Verification Code=4729``); a
    candidate id can be followed by a space (``c1 4729``). A field with no value (``submit c2``)
    returns value ``None``, which triggers a prompt — masked for a sensitive field — so a secret
    is never typed inline. Returns ``None`` only when no field is given."""
    rest = rest.strip()
    if not rest:
        return None
    if "=" in rest:
        field, _, value = rest.partition("=")
    else:
        field, _, value = rest.partition(" ")
    field, value = field.strip(), value.strip()
    if not field:
        return None
    return (field, value or None)


async def _handle_type_submit(
    command: str,
    rest: str,
    operator: OperatorController,
    surface: PlaywrightSurface,
    displayed: list[_Candidate],
    epoch: int,
) -> list[_Candidate]:
    """Perform a ``type``/``submit`` against the displayed candidates, refreshing state on
    success. A sensitive field's value is never accepted inline (it would leak to the terminal
    transcript); it is read from a no-echo prompt so it appears in neither the screen nor
    evidence."""
    parsed = _parse_field_value(rest)
    if parsed is None:
        operator_ui.note(
            f"usage: {command} <control>=<value>   e.g.  {command} c1=4729   "
            f"(or  {command} c1 4729)",
            style=operator_ui.FAIL,
        )
        return displayed
    field, value = parsed
    chosen = _select_candidate(field, displayed)
    name = chosen.name if isinstance(chosen, _Candidate) else field
    if value is not None and _is_sensitive(name) and sys.stdin.isatty():
        operator_ui.note(
            f"{name!r} is a sensitive field — don't type the value inline. "
            f"Use '{command} {field}' and enter it at the masked prompt.",
            style=operator_ui.WARN,
        )
        return displayed
    if value is None:
        reader = getpass.getpass if _is_sensitive(name) else input
        value = (await asyncio.to_thread(reader, f"  {name}: ")).strip()
        if not value:
            operator_ui.note("cancelled (no value entered)")
            return displayed
    if await _operator_type(operator, displayed, field, value, epoch, submit=command == "submit"):
        return await _after_action(operator, surface)
    return displayed


async def _operator_click(
    operator: OperatorController, displayed: list[_Candidate], token: str, epoch: int
) -> bool:
    """Resolve and click a human-chosen control on the same live session; True if it acted."""
    chosen = _select_candidate(token, displayed)
    if isinstance(chosen, str):
        operator_ui.note(chosen, style=operator_ui.FAIL)
        return False
    target = await _resolve_live_target(operator, chosen)
    if isinstance(target, str):
        operator_ui.note(target, style=operator_ui.FAIL)
        return False
    try:
        await operator.perform(ClickControl(target))
    except (OperatorError, OperatorScopeError, SurfaceError) as error:
        operator_ui.note(f"error: {error}", style=operator_ui.FAIL)
        return False
    operator_ui.render_human_action("click", target.name or token, epoch)
    return True


async def _operator_type(
    operator: OperatorController, displayed: list[_Candidate], token: str, value: str,
    epoch: int, *, submit: bool,
) -> bool:
    """Resolve a human-chosen field and enter a value (optionally submitting via Enter) on the
    same live session; True if it acted. The value is used transiently and never persisted."""
    chosen = _select_candidate(token, displayed)
    if isinstance(chosen, str):
        operator_ui.note(chosen, style=operator_ui.FAIL)
        return False
    target = await _resolve_live_target(operator, chosen)
    if isinstance(target, str):
        operator_ui.note(target, style=operator_ui.FAIL)
        return False
    try:
        await operator.perform(TypeControl(target, value, submit=submit))
    except (OperatorError, OperatorScopeError, SurfaceError) as error:
        operator_ui.note(f"error: {error}", style=operator_ui.FAIL)
        return False
    operator_ui.render_human_action(
        "submit" if submit else "type", target.name or token, epoch, value=True
    )
    return True


def _session_label(surface: PlaywrightSurface) -> str:
    """Our own stable session id (not a driver internal), shown so the reviewer can see the same
    live session persist across the human takeover."""
    return f"sess_{surface.session_id[:8]}"


def _print_intervention(
    request: InterventionRequest, facts: operator_ui.InterventionFacts, session_id: str
) -> None:
    """Present the paused replay as deterministic Expected-vs-Observed facts (no prose, no
    model): what the artifact expected here vs what the live surface actually shows."""
    operator_ui.render_intervention(
        request,
        title="INTERVENTION REQUIRED",
        commands=[
            ("take", "take exclusive control of this same live session"),
            ("inspect", "re-read the current live state (route/heading/blocker)"),
            ("controls", "list controls to act on ('controls --all' for the whole page)"),
            ("status · help · quit", ""),
        ],
        facts=facts,
        session_id=session_id,
    )


async def _operator_console(
    operator: OperatorController, session: ReplaySession, surface: PlaywrightSurface,
    *, is_mutation: bool,
) -> RunResult | None:
    """Block for real operator input; drive the same live session; return the result.

    The canonical handoff is headed: the human's visual surface is the live browser window.
    The terminal presents the controls scoped to the blocking region and offers the generic
    computer-use vocabulary; how to resolve the block is the operator's decision, not a scripted
    step (``ack`` remains only as a convenience alias for clicking an acknowledgement control).
    """
    displayed: list[_Candidate] = []
    first = True
    while True:
        if not first:
            operator_ui.render_turn_separator()
        first = False
        try:
            line = (
                await asyncio.to_thread(
                    operator_ui.input_line, session.lease.owner.value, session.lease.epoch
                )
            ).strip()
        except EOFError:
            return None
        if not line:
            continue
        command, _, rest = line.partition(" ")
        rest = rest.strip()
        if command == "take":
            try:
                before = session.lease.owner.value
                epoch = operator.take_control()
                operator_ui.render_control_transfer(
                    before, "HUMAN", epoch, session_id=_session_label(surface)
                )
                operator_ui.render_human_mode()
                displayed = await _show_controls(operator, surface)
                operator_ui.render_commands()
            except OperatorError as error:
                operator_ui.note(f"error: {error}", style=operator_ui.FAIL)
        elif command in ("controls", "view"):
            displayed = await _show_controls(
                operator, surface, full_page=rest in ("--all", "all")
            )
        elif command == "inspect":
            operator_ui.render_observed(await _observe_state(surface))
        elif command in ("click", "ack"):
            token = rest if command == "click" else "Acknowledge"
            if await _operator_click(operator, displayed, token, session.lease.epoch):
                displayed = await _after_action(operator, surface)
        elif command in ("type", "submit"):
            displayed = await _handle_type_submit(
                command, rest, operator, surface, displayed, session.lease.epoch
            )
        elif command == "resume":
            try:
                result = await operator.resume()
            except OperatorError as error:
                operator_ui.note(f"error: {error}", style=operator_ui.FAIL)
                continue
            if isinstance(result, Escalated):
                operator_ui.note(
                    "still blocked; look at the browser, 'controls' to re-list, resolve it "
                    "(click/type), then 'resume' again",
                    style=operator_ui.WARN,
                )
                continue
            handback = (
                f"Control returned HUMAN → AUTOMATION "
                f"(session {_session_label(surface)} preserved, epoch {session.lease.epoch})"
            )
            if is_mutation:
                lines = [
                    "Reconciling — re-establishing the effect by independent read-only "
                    "re-derivation…",
                    handback,
                    "Re-ran READ-ONLY verification — the consequential write was NOT re-dispatched",
                    f"model_calls: {result.model_calls}",
                ]
            else:
                lines = [
                    "Reconciling current application state…",
                    handback,
                    "Resumed automation from the capability checkpoint",
                    f"model_calls: {result.model_calls}",
                ]
            operator_ui.render_reconciliation(lines)
            return result
        elif command == "status":
            operator_ui.note(
                f"owner={session.lease.owner.value} epoch={session.lease.epoch}"
            )
        elif command in ("help", "?"):
            operator_ui.render_help()
        elif command in ("quit", "abort"):
            return None
        else:
            operator_ui.note(f"unknown command: {command!r}", style=operator_ui.FAIL)


async def _run_handoff_demo(
    artifact: str, params: dict[str, str], target: str, scenario: str, evidence_dir: str,
    headed: bool, capability_name: str = "member_lookup",
) -> int:
    capability = Capability.model_validate_json(Path(artifact).read_text(encoding="utf-8"))
    profile = _replay_profile(capability_name)
    out = Path(evidence_dir)
    out.mkdir(parents=True, exist_ok=True)
    actions = EvidenceStore(out / "actions.jsonl")
    surface = PlaywrightSurface(headless=not headed)
    await surface.start()
    try:
        # Arm the runtime scenario for the whole flow, then drive the capability until
        # it either completes or pauses for a human. The same operator loop serves a
        # read capability's unexpected dialog and a write's ambiguous verification.
        await surface.goto(f"{target.rstrip('/')}/?scenario={scenario}")
        session = ReplaySession(
            capability, params, target,
            nav_policy=profile.nav_policy, safe_clicks=profile.safe_clicks, surface=surface,
            secrets=EnvSecretProvider(),
            confirmation=_replay_confirmation(capability),
            authority=profile.authority,
        )
        opened = await session.start()
        if opened is not None:
            typer.echo(opened.model_dump_json())
            return 1
        result = await session.advance()
        if not isinstance(result, Escalated):
            operator_ui.note("no intervention was required for this run")
            operator_ui.render_result(result)
            return 0 if isinstance(result, Success) else 1
        operator = OperatorController(session, evidence=actions)
        request = await operator.raise_intervention()
        (out / "intervention.json").write_text(
            request.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _print_intervention(
            request, await _build_facts(capability, request.step_id, surface),
            _session_label(surface),
        )
        final = await _operator_console(
            operator, session, surface,
            is_mutation=request.reason.value == "MUTATION_AMBIGUOUS",
        )
        if final is None:
            operator_ui.note("aborted before completion", style=operator_ui.FAIL)
            return 1
        operator_ui.render_result(final)
        (out / "result.json").write_text(
            json.dumps({"result": persistable_result(final, capability)}, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0 if isinstance(final, Success) else 1
    finally:
        await surface.close()


@app.command("handoff-demo")
def handoff_demo_command(
    artifact: str = typer.Argument("evidence/capability/member_lookup.v1.json"),
    param: list[str] = typer.Option(["member_number=12345"], "--param", "-p"),
    target: str = typer.Option("http://localhost:8000", "--target"),
    scenario: str = typer.Option("unexpected_dialog", "--scenario"),
    evidence_out: str = typer.Option("evidence/replay_handoff", "--evidence-out"),
    capability: str = typer.Option("member_lookup", "--capability"),
    headed: bool = typer.Option(True, "--headed/--headless"),
) -> None:
    """Replay a capability, pause on an unhandled state, and hand the live session to a
    human operator who resolves it and resumes automation. Serves both a read's
    unexpected dialog and a write's ambiguous verification on the same operator loop."""
    code = asyncio.run(
        _run_handoff_demo(
            artifact, _parse_params(param), target, scenario, evidence_out, headed, capability
        )
    )
    raise typer.Exit(code=code)


@app.command("reset-demo")
def reset_demo_command(
    target: str = typer.Option("http://localhost:8000", "--target"),
) -> None:
    """Reset LegacyCore's in-memory demo state so the write demos can be re-run without
    restarting the server (clears created sub-accounts, acknowledgements, dispatch count)."""
    try:
        response = httpx.post(f"{target.rstrip('/')}/reset", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as error:
        typer.echo(f"reset failed (is `legacy-core` running?): {error}")
        raise typer.Exit(code=1) from error
    typer.echo("demo state reset")
