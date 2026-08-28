"""Deterministic replay: execute a compiled Capability with no model in the loop.

Replay drives the same TrustedKernel as discovery through a ``ReplaySession``. It
never constructs or calls a discovery model, so the reported model_calls is always
0. This module is the run-to-completion convenience over a session; the resumable
same-session handoff path drives the session directly.
"""

from __future__ import annotations

from computer_use.model import Capability, RunResult
from computer_use.safety import (
    AuthorityPolicy,
    ConfirmationPolicy,
    NavigationPolicy,
    SecretProvider,
)
from computer_use.surface import Surface

from .session import _RESOLVE_TIMEOUT_MS, ReplaySession
from .trace import ReplayEventSink


async def replay(
    capability: Capability,
    inputs: dict[str, str],
    target_url: str,
    *,
    nav_policy: NavigationPolicy,
    safe_clicks: frozenset[str] = frozenset(),
    surface: Surface | None = None,
    resolve_timeout_ms: int = _RESOLVE_TIMEOUT_MS,
    secrets: SecretProvider | None = None,
    confirmation: ConfirmationPolicy | None = None,
    commit_timeout_ms: int | None = None,
    authority: AuthorityPolicy | None = None,
    on_event: ReplayEventSink | None = None,
) -> RunResult:
    session = ReplaySession(
        capability,
        inputs,
        target_url,
        nav_policy=nav_policy,
        safe_clicks=safe_clicks,
        surface=surface,
        resolve_timeout_ms=resolve_timeout_ms,
        secrets=secrets,
        confirmation=confirmation,
        commit_timeout_ms=commit_timeout_ms,
        authority=authority,
        on_event=on_event,
    )
    try:
        return await session.run_to_completion()
    finally:
        await session.close()
