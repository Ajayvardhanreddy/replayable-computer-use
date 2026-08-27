"""Surface layer: the async perceive/act seam over a live UI."""

from __future__ import annotations

from .base import (
    BlockerObservation,
    Candidate,
    Observation,
    StructuralSnapshot,
    Surface,
    SurfaceDriverError,
    SurfaceError,
    SurfaceTransientError,
    TargetAmbiguousError,
    TargetNotFoundError,
)
from .playwright_surface import PlaywrightSurface

__all__ = [
    "BlockerObservation",
    "Candidate",
    "Observation",
    "PlaywrightSurface",
    "StructuralSnapshot",
    "Surface",
    "SurfaceDriverError",
    "SurfaceError",
    "SurfaceTransientError",
    "TargetAmbiguousError",
    "TargetNotFoundError",
]
