"""Navigation scope policy: the configured origin + route allowlist.

Enforced in the trusted runtime (discovery/replay orchestration), not in the
kernel: the kernel decides whether an *action* may execute; this decides whether
the *session* is allowed to be at a URL. A caller-supplied target, a redirect, or
a model-driven navigation that lands out of scope is refused before any further
action, so an out-of-scope origin can never lead to another automated step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from computer_use.model import PolicyDecision, PolicyEffect


def route_matches(pattern: str, path: str) -> bool:
    """Match a URL path against a narrow deterministic pattern.

    Literal segments are escaped; a ``:param`` placeholder matches exactly one path
    segment. The whole pattern is anchored. No caller-supplied regex is honored.
    """
    parts: list[str] = []
    for segment in pattern.split("/"):
        if segment.startswith(":") and len(segment) > 1:
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    return re.fullmatch("/".join(parts), path) is not None


def route_label(path: str, patterns: frozenset[str]) -> str:
    """A safe, structural label for a URL path for persistence.

    Returns the matching allowed-route pattern (e.g. ``/workspace/member/:member_id``)
    rather than the concrete path, so a path parameter that is sensitive (a member id)
    is never persisted. An unmatched path yields a masked token, never the raw path.
    """
    for pattern in sorted(patterns):
        if route_matches(pattern, path):
            return pattern
    return "<off-scope>"


@dataclass(frozen=True)
class NavigationPolicy:
    """Allowlist of permitted origins and route patterns for a run.

    ``check`` validates the *full* URL — scheme, host, port, and path — so an
    out-of-scope redirect that keeps a valid-looking path is still refused.
    """

    allowed_origins: frozenset[str]
    allowed_routes: frozenset[str]

    def check(self, url: str) -> PolicyDecision:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=f"origin {origin!r} is not in the allowed scope",
                rule="navigation_origin",
            )
        if not any(route_matches(pattern, parts.path) for pattern in self.allowed_routes):
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=f"route {parts.path!r} is not in the allowed scope",
                rule="navigation_route",
            )
        return PolicyDecision(
            effect=PolicyEffect.ALLOW, reason="in navigation scope", rule="navigation"
        )

    def check_all(self, urls: Iterable[str]) -> PolicyDecision:
        """Fail-closed across every frame URL the session occupies.

        Denies on the first out-of-scope document, so an in-scope top page can never
        mask an out-of-scope subframe. With no URLs to judge the session is not at any
        out-of-scope origin, so it allows.
        """
        decision = PolicyDecision(
            effect=PolicyEffect.ALLOW, reason="all frames in navigation scope", rule="navigation"
        )
        for url in urls:
            decision = self.check(url)
            if decision.effect is PolicyEffect.DENY:
                return decision
        return decision
