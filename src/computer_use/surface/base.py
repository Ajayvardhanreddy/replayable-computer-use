"""The surface seam: how the system perceives and acts on a live UI.

The capability artifact is expressed in surface-agnostic semantic descriptors; a
Surface implementation translates them onto a concrete UI. This keeps the
recorded flow independent of any one automation technology, and it is the seam a
future legacy-web or desktop surface would swap without touching the artifact.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

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
    async def click(self, target: TargetDescriptor) -> None: ...
    async def type_text(self, target: TargetDescriptor, text: str) -> None: ...
    async def extract(self, target: TargetDescriptor) -> str: ...
    async def capture(self) -> StructuralSnapshot: ...
    async def wait_for_frame_url(self, fragment: str, timeout_ms: int = 5000) -> None: ...
    async def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool: ...
    async def has_text(self, text: str) -> bool: ...
    async def has_heading(self, name: str) -> bool: ...
    async def current_route(self) -> str: ...
    async def wait_settled(self) -> None: ...
    async def primary_heading(self) -> str | None: ...
    async def wait_for_heading_change(
        self, previous: str | None, timeout_ms: int = 5000
    ) -> str | None: ...
    async def close(self) -> None: ...
