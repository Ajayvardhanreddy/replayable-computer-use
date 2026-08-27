"""The surface seam: how the system perceives and acts on a live UI.

The capability artifact is expressed in surface-agnostic semantic descriptors; a
Surface implementation translates them onto a concrete UI. This keeps the
recorded flow independent of any one automation technology, and it is the seam a
future legacy-web or desktop surface would swap without touching the artifact.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from computer_use.model import TargetDescriptor


class Candidate(BaseModel):
    """An interactable element (or table cell) discovered on the current surface."""

    model_config = ConfigDict(extra="forbid")
    id: str
    role: str
    name: str | None = None
    text: str | None = None
    frame: str | None = None
    row: str | None = None
    column: str | None = None
    filled: bool | None = None


class BlockerObservation(BaseModel):
    """A structural description of a blocking region (e.g. a modal dialog) currently on the
    surface: its role, accessible name, minimized visible text, and the controls it contains.

    This is structural only — no pixels — so it is safe to show an authorized operator and to
    reason about deterministically. It scopes an intervention to what is actually blocking the
    flow, rather than the whole background page.
    """

    model_config = ConfigDict(extra="forbid")
    role: str
    name: str | None = None
    text: str | None = None
    controls: list[Candidate] = Field(default_factory=list)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str
    candidates: list[Candidate]


class StructuralSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str
    frames: list[str]
    landmarks: list[str]


class SurfaceError(Exception):
    """Base class for surface failures.

    A Surface implementation raises only SurfaceError subtypes; provider-specific
    driver exceptions never cross this boundary.
    """


class TargetNotFoundError(SurfaceError):
    """No element matched the target descriptor."""


class TargetAmbiguousError(SurfaceError):
    """More than one element matched the target descriptor (fail closed)."""


class SurfaceTransientError(SurfaceError):
    """A transient runtime condition (e.g. a mid-navigation execution-context race)
    that a bounded retry may recover from. Distinct from a hard driver failure."""


class SurfaceDriverError(SurfaceError):
    """An underlying automation-driver failure. Implementations translate
    provider-specific exceptions into this rather than leaking them upward."""


class Surface(Protocol):
    """Async perceive/act contract. Sequential control flow, async I/O."""

    async def start(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def observe(self) -> Observation: ...
    async def count(self, target: TargetDescriptor) -> int: ...
    async def click(self, target: TargetDescriptor, *, timeout_ms: int | None = None) -> None: ...
    async def type_text(
        self, target: TargetDescriptor, text: str, *, submit: bool = False
    ) -> None: ...
    async def extract(self, target: TargetDescriptor) -> str: ...
    async def capture(self) -> StructuralSnapshot: ...
    async def wait_for_frame_url(self, fragment: str, timeout_ms: int = 5000) -> None: ...
    async def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool: ...
    async def has_text(self, text: str) -> bool: ...
    async def has_heading(self, name: str) -> bool: ...
    async def has_blocking_dialog(self) -> bool: ...
    async def current_route(self) -> str: ...
    async def current_url(self) -> str: ...
    # Every real document URL the session currently occupies (top page + subframes).
    # Navigation scope is enforced across all of them, so an out-of-scope redirect
    # inside an embedded workspace frame is caught even when the top page stays in scope.
    async def scope_urls(self) -> list[str]: ...
    async def wait_settled(self) -> None: ...
    async def primary_heading(self) -> str | None: ...
    async def wait_for_heading_change(
        self, previous: str | None, timeout_ms: int = 5000
    ) -> str | None: ...
    async def close(self) -> None: ...
