"""Fail-closed evidence policy: what may be persisted for a given page.

A raw screenshot or DOM snapshot can contain the same regulated data as pixels, so
evidence is allowlisted, not scrubbed after the fact: when a page's screenshot
safety cannot be established, the collector persists sanitized structural evidence
instead of a screenshot. Deterministic pixel masking (MASK_KNOWN) is a policy the
contract allows but no current page needs — every LegacyCore page shows member data
or is a form, so routes resolve to STRUCTURAL_ONLY (also the fail-closed default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from computer_use.safety import route_label, route_matches
from computer_use.surface import Surface


class ScreenshotPolicy(StrEnum):
    SAFE = "safe"
    MASK_KNOWN = "mask_known"
    STRUCTURAL_ONLY = "structural_only"


@dataclass(frozen=True)
class EvidencePolicy:
    """Maps a route to its screenshot policy; an unknown route fails closed."""

    safe_routes: frozenset[str] = field(default_factory=frozenset)
    mask_known_routes: frozenset[str] = field(default_factory=frozenset)

    def for_route(self, route: str) -> ScreenshotPolicy:
        if any(route_matches(pattern, route) for pattern in self.safe_routes):
            return ScreenshotPolicy.SAFE
        if any(route_matches(pattern, route) for pattern in self.mask_known_routes):
            return ScreenshotPolicy.MASK_KNOWN
        return ScreenshotPolicy.STRUCTURAL_ONLY  # fail closed


class FailureEvidence(BaseModel):
    """A sanitized richer signal on failure: structural by default, never raw values."""

    model_config = ConfigDict(extra="forbid")
    policy: ScreenshotPolicy
    route: str
    frames: list[str] = Field(default_factory=list)
    landmarks: list[str] = Field(default_factory=list)
    screenshot: str | None = None


class EvidenceCollector:
    def __init__(
        self,
        policy: EvidencePolicy | None = None,
        route_patterns: frozenset[str] = frozenset(),
    ) -> None:
        self._policy = policy if policy is not None else EvidencePolicy()
        self._routes = route_patterns

    async def collect_failure_evidence(self, surface: Surface, route: str) -> FailureEvidence:
        decision = self._policy.for_route(route)
        # A structural snapshot is always safe (landmarks/headings, no record values),
        # and the route is recorded as its pattern, not the concrete PII path.
        snapshot = await surface.capture()
        # Screenshot persistence is only permitted for a SAFE route; STRUCTURAL_ONLY
        # (and the unknown/fail-closed default) explicitly refuse it. No LegacyCore
        # route is SAFE, so this stays None — uncertainty yields less evidence.
        return FailureEvidence(
            policy=decision,
            route=route_label(snapshot.route, self._routes),
            frames=snapshot.frames,
            landmarks=snapshot.landmarks,
            screenshot=None,
        )
