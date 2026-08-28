"""Presentation layer for the operator console — the human-handoff terminal.

Renders the control-transfer state machine (automation ⇄ human, lease epochs,
reconciliation, result) so a human can *read* the handoff without reading source.

This is presentation only. It depends on the public contract types
(``InterventionRequest``, ``RunResult``) and never the other way round: execution,
session, and kernel code know nothing about this module or about terminal styling.
Output degrades gracefully — ``rich`` emits plain text when stdout is not a terminal
(pipes, CI, captured evidence), so no styling ever leaks into machine-read output.

Semantic palette, deliberately small:
    automation → cyan   human → yellow   success → green
    intervention / ambiguity → amber   denial / hard failure → red   metadata → dim
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.box import ROUNDED
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from computer_use.model import BusinessOutcome, Escalated, RunResult, Success

from .intervention import InterventionRequest


@dataclass(frozen=True)
class Blocker:
    """A structural blocking region and its own controls (id, role, name)."""

    role: str
    name: str | None
    text: str | None
    controls: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class InterventionFacts:
    """Deterministic Expected-vs-Observed context for a paused replay — built from the compiled
    artifact and a fresh structural observation of the live surface, never from a model. The
    operator UI presents these facts; it does not reason or explain in prose."""

    last_action: str | None = None
    expected_heading: str | None = None
    expected_output: str | None = None
    route: str | None = None
    observed_heading: str | None = None
    blocker: Blocker | None = None

# Semantic style names (one place; callers name meaning, not colour codes).
AUTOMATION = "cyan"
HUMAN = "yellow"
OK = "green"
WARN = "dark_orange"
FAIL = "red"
META = "dim"
CMD = "bold"

_console = Console()


def _owner_style(owner: str) -> str:
    return AUTOMATION if owner.upper().startswith("AUTO") else HUMAN


def _kv(rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(style=META, justify="left")
    table.add_column()
    for label, value in rows:
        table.add_row(label, value)
    return table


def _commands(commands: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(style=CMD, justify="left")
    table.add_column(style=META)
    for name, desc in commands:
        table.add_row(name, desc)
    return table


def prompt_text(owner: str, epoch: int) -> Text:
    """The ownership-visible operator prompt, e.g. ``[AUTOMATION · epoch 0] operator ❯``.

    Makes lease epochs a first-class, visible part of every interaction rather than an
    obscure implementation detail.
    """
    text = Text()
    text.append(f"[{owner.upper()} · epoch {epoch}] ", style=f"bold {_owner_style(owner)}")
    text.append("operator ❯ ", style=CMD)
    return text


def input_line(owner: str, epoch: int, *, console: Console | None = None) -> str:
    """Read one operator command line behind the ownership-visible prompt (blocking).

    Wrap in ``asyncio.to_thread`` at the call site; the prompt itself is styled here so
    the presentation stays out of the console-control loop.
    """
    return (console or _console).input(prompt_text(owner, epoch))


def _blocker_controls_table(controls: list[tuple[str, str, str]]) -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(style=f"bold {HUMAN}", justify="left")
    table.add_column(style=META)
    table.add_column(style=HUMAN)
    for candidate_id, role, name in controls:
        table.add_row(f"      {candidate_id}", role, name)
    return table


def _facts_body(facts: InterventionFacts) -> list[RenderableType]:
    """The deterministic Expected-vs-Observed block (facts, not prose)."""
    observed = facts.observed_heading or "—"
    if facts.blocker is not None:
        blocked = f'blocked by {facts.blocker.role} "{facts.blocker.name or "?"}"'
        observed = f"{observed}  ·  {blocked}"
    expected = " · ".join(p for p in (facts.expected_heading, facts.expected_output) if p)
    rows: list[tuple[str, str]] = []
    if facts.last_action:
        rows.append(("Last action", facts.last_action))
    if expected:
        rows.append(("Expected", expected))
    rows.append(("Observed", observed))
    if facts.blocker is not None and facts.blocker.text:
        # Labelled so its source is unmistakable: the dialog's own copy, not an automation note.
        rows.append(("Dialog text", f'"{facts.blocker.text}"'))
    body: list[RenderableType] = [_kv(rows)]
    if facts.blocker is not None and facts.blocker.controls:
        body.append(Text("  controls in blocker:", style=META))
        body.append(_blocker_controls_table(facts.blocker.controls))
    return body


def render_intervention(
    request: InterventionRequest,
    *,
    title: str,
    commands: list[tuple[str, str]],
    facts: InterventionFacts | None = None,
    agent_note: str | None = None,
    session_id: str | None = None,
    console: Console | None = None,
) -> None:
    """The framed ``INTERVENTION REQUIRED`` panel: identity (capability/step/reason/ids/session/
    owner/epoch/route) plus, for replay, a deterministic Expected-vs-Observed ``facts`` block —
    never a model-written explanation. ``session_id`` is our own stable session identifier (not a
    driver internal): the same value shown here and after takeover proves the *same live session*.
    ``agent_note`` is only for discovery, where a running model may state its own escalation
    reason (clearly labelled as the agent's words)."""
    rows: list[tuple[str, str]] = [
        ("Capability", f"{request.capability} v{request.version}"),
        ("Step", request.step_id or "—"),
        ("Reason", request.reason.value),
        ("Run", request.run_id),
        ("Case id", request.intervention_id),
    ]
    if session_id is not None:
        rows.append(("Session", session_id))
    rows.extend([
        ("Control", request.control_owner.value.upper()),
        ("Epoch", str(request.control_epoch)),
        ("Route", request.route),
    ])
    body: list[RenderableType] = [_kv(rows)]
    if agent_note is not None:
        body.extend([Text(""), Text(f"Agent request: {agent_note}", style=WARN)])
    if facts is not None:
        body.append(Text(""))
        body.extend(_facts_body(facts))
    body.extend([Text(""), _commands(commands)])
    (console or _console).print(
        Panel(
            Group(*body),
            title=Text(title, style=f"bold {WARN}"),
            border_style=WARN,
            box=ROUNDED,
            expand=False,
            padding=(1, 2),
        )
    )


def render_approval_request(
    *,
    action: str,
    target: str,
    risk: str,
    context: str | None = None,
    console: Console | None = None,
) -> None:
    """The consequential-write authorization panel shown during discovery.

    The model has proposed a write it cannot authorize; a human must approve this exact
    action. Structural fields only — the action, the resolved target, the software-derived
    risk, and the current landmark — never a model explanation and never a raw value. Same
    framed, amber presentation as the intervention panel so the risk gate reads consistently.
    """
    rows = [("Action", action), ("Target", target), ("Risk", risk)]
    if context:
        rows.append(("Context", context))
    body: list[RenderableType] = [
        _kv(rows),
        Text(""),
        Text(
            "The model cannot authorize this — a human must approve this exact action.",
            style=WARN,
        ),
    ]
    (console or _console).print(
        Panel(
            Group(*body),
            title=Text("AUTHORIZATION REQUIRED  ·  consequential write", style=f"bold {WARN}"),
            border_style=WARN,
            box=ROUNDED,
            expand=False,
            padding=(1, 2),
        )
    )


def render_observed(facts: InterventionFacts, *, console: Console | None = None) -> None:
    """``inspect``: a fresh structural read of the current live state (route, heading, blocker,
    blocker controls). No model, no screenshot — just the live Surface described structurally."""
    rows = [("Route", facts.route or "—"), ("Heading", facts.observed_heading or "—")]
    if facts.blocker is not None:
        rows.append(("Blocker", f'{facts.blocker.role} "{facts.blocker.name or "?"}"'))
    body: list[RenderableType] = [Text("  Current state", style=f"bold {HUMAN}"), _kv(rows)]
    if facts.blocker is not None and facts.blocker.text:
        body.append(Text(f'    "{facts.blocker.text}"', style=META))
    if facts.blocker is not None and facts.blocker.controls:
        body.append(Text("  controls in blocker:", style=META))
        body.append(_blocker_controls_table(facts.blocker.controls))
    (console or _console).print(Group(*body))


def render_controls(
    candidates: list[tuple[str, str, str]],
    *,
    blocker: tuple[str, str | None] | None = None,
    console: Console | None = None,
) -> None:
    """Show the controls to act on — scoped to the blocking region when one is present, so the
    operator sees what is blocking rather than the whole background page. The candidate list
    only; the generic command vocabulary is shown separately (once) to keep each step readable.
    Nothing here prescribes a fix; the operator reads the live page and chooses."""
    lines: list[RenderableType] = []
    if blocker is not None:
        role, name = blocker
        lines.append(Text(f'  blocked by {role} "{name or "?"}" — its controls:', style=WARN))
    elif candidates:
        lines.append(Text("  current actionable controls:", style=META))
    if candidates:
        lines.append(_blocker_controls_table(candidates))
    else:
        lines.append(Text("  (no interactable controls detected)", style=META))
    (console or _console).print(Group(*lines))


_COMMAND_GRID = (
    ("click <control>", "type <control>=<value>", "submit <control>=<value>"),
    ("controls [--all]", "inspect", "resume"),
    ("status", "help", "quit"),
)


def render_commands(*, console: Console | None = None) -> None:
    """The generic action vocabulary as a small aligned grid (primary actions on the first row)
    — shown once on takeover, so the flow stays readable. ``help`` shows the fuller descriptions."""
    table = Table.grid(padding=(0, 4))
    for _ in range(3):
        table.add_column(style=CMD, justify="left")
    for left, middle, right in _COMMAND_GRID:
        table.add_row(f"    {left}", middle, right)
    (console or _console).print(table)


_HELP = (
    ("click <control>", "click a control  (e.g. click c1)"),
    ("type <control>=<value>", "enter a value into a field"),
    ("submit <control>=<value>", "enter a value and submit the form (Enter)"),
    ("controls [--all]", "list the blocker's controls  (--all for the whole page)"),
    ("inspect", "re-read the current live state (route/heading/blocker)"),
    ("resume", "hand control back to automation and continue"),
    ("status", "show the current owner and lease epoch"),
    ("help", "show this list"),
    ("quit", "abort without resolving"),
)


def render_help(*, console: Console | None = None) -> None:
    """The full command list with a one-line description of each — what ``help`` prints."""
    table = Table.grid(padding=(0, 3))
    table.add_column(style=CMD, justify="left")
    table.add_column(style=META)
    for name, description in _HELP:
        table.add_row(f"    {name}", description)
    (console or _console).print(Group(Text("  commands:", style=META), table))


def render_turn_separator(*, console: Console | None = None) -> None:
    """A subtle full-width divider printed between operator turns, so each take / act / resume
    step reads as its own block instead of one continuous wall of text."""
    (console or _console).print(Rule(style=META))


def render_control_transfer(
    frm: str, to: str, epoch: int, *, session_id: str | None = None,
    console: Console | None = None,
) -> None:
    """The ``AUTOMATION → HUMAN`` (or reverse) transfer with the new lease epoch. When a
    ``session_id`` is given it is shown as *preserved* — the same session id as before the
    transfer, making the same-live-session invariant visible."""
    arrow = Text("    ")
    arrow.append(frm.upper(), style=f"bold {_owner_style(frm)}")
    arrow.append("  →  ", style=META)
    arrow.append(to.upper(), style=f"bold {_owner_style(to)}")
    body: list[RenderableType] = [Text("  ● Control transferred", style=f"bold {OK}"), arrow]
    if session_id is not None:
        body.append(Text(f"    session {session_id} preserved", style=META))
    body.append(Text(f"    epoch → {epoch}", style=META))
    (console or _console).print(Group(*body))


def render_human_mode(*, console: Console | None = None) -> None:
    """Panel making explicit that the human now drives the same session and automation
    is fenced (cannot act) until control is handed back."""
    (console or _console).print(
        Panel(
            Text(
                "You now control the same live session.\n"
                "Automation is fenced and cannot issue actions until you hand control back.",
                style=HUMAN,
            ),
            title=Text("HUMAN CONTROL", style=f"bold {HUMAN}"),
            border_style=HUMAN,
            box=ROUNDED,
            expand=False,
            padding=(0, 2),
        )
    )


def render_human_action(
    action: str, target: str, epoch: int, *, value: bool = False, console: Console | None = None
) -> None:
    """A recorded human action on the live session, as one line (a typed value is audited
    redacted)."""
    line = Text("  ✓ recorded: ", style=f"bold {OK}")
    line.append(f'{action} "{target}"', style=HUMAN)
    suffix = f"   (epoch {epoch}{' · value redacted' if value else ''})"
    line.append(suffix, style=META)
    (console or _console).print(line)


def render_reconciliation(
    lines: list[str], *, console: Console | None = None
) -> None:
    """The reconcile-then-resume checkpoints. The first line is in-progress (◌); the rest
    are completed checkpoints (✓)."""
    body: list[RenderableType] = [Text(f"  ◌ {lines[0]}", style=META)]
    body.extend(Text(f"  ✓ {line}", style=OK) for line in lines[1:])
    (console or _console).print(Group(*body))


def render_result(result: RunResult, *, console: Console | None = None) -> None:
    """The terminal ``RUN COMPLETE`` panel. Shows the caller's own deliverable outputs
    (the same values already returned on stdout); persisted evidence remains masked
    independently of this display."""
    if isinstance(result, Success):
        title, style, rows = "SUCCESS", OK, list(result.outputs.items())
    elif isinstance(result, BusinessOutcome):
        title, style = "BUSINESS OUTCOME", AUTOMATION
        rows = [("code", result.code), *result.outputs.items()]
    elif isinstance(result, Escalated):
        title, style = "ESCALATED", WARN
        rows = [("code", result.code), ("step", result.step_id or "—")]
    else:  # Failure
        title, style = "FAILURE", FAIL
        rows = [("code", result.code.value), ("step", result.step_id or "—")]
    rows.append(("model_calls", str(result.model_calls)))
    (console or _console).print(
        Panel(
            _kv([(k, str(v)) for k, v in rows]),
            title=Text(title, style=f"bold {style}"),
            border_style=style,
            box=ROUNDED,
            expand=False,
            padding=(0, 2),
        )
    )


def render_handoff_case(
    request: InterventionRequest, effect_reason: str, *, console: Console | None = None
) -> None:
    """An *unattended* runner's paused run, rendered as a human-actionable handoff case:
    what was attempted, where it stopped, why, what automation did, and what to do next —
    with only structural state (no raw member id or financial value)."""
    is_mutation = request.reason.value == "MUTATION_AMBIGUOUS"
    rows: list[tuple[str, str]] = [
        ("Capability", f"{request.capability} v{request.version}"),
        ("Stopped at", request.step_id or "—"),
        ("Why", request.reason.value),
    ]
    if effect_reason:
        rows.append(("What happened", effect_reason))
    rows.append(("Last state", f"route={request.route}  landmarks={request.evidence.landmarks}"))
    rows.append(("Case id", request.intervention_id))
    if is_mutation:
        did = "dispatched the write exactly once; did NOT retry it and did NOT assume success."
        do = "confirm in the system of record whether the change took effect, then close this case."
    else:
        did = "paused before acting further; no state was changed."
        do = "resolve the blocking state, then resume the run."
    group = Group(
        _kv(rows),
        Text(""),
        Text(f"What I did:  {did}", style=HUMAN),
        Text(f"Please:      {do}", style=f"bold {HUMAN}"),
    )
    (console or _console).print(
        Panel(
            group,
            title=Text("HANDOFF CASE — a human needs to look into this", style=f"bold {WARN}"),
            border_style=WARN,
            box=ROUNDED,
            expand=False,
            padding=(1, 2),
        )
    )


def note(message: str, *, style: str = META, console: Console | None = None) -> None:
    """A single indented status/feedback line."""
    (console or _console).print(Text(f"  {message}", style=style))
