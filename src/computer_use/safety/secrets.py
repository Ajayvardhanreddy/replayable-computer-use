"""Secret resolution seam.

The artifact stores only a symbolic ``SecretRef(name)``. A ``SecretProvider``
resolves it to a concrete value inside the trusted runtime, immediately before the
value is used, and the resolved value is never sent to the model, written to the
artifact, or persisted in evidence/logs. A missing secret raises with the name
only — never a value. Environment-backed resolution is sufficient here; a
vault/KMS integration is out of scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class MissingSecret(Exception):
    """A required secret was unavailable. Carries the reference name, never a value."""


class SecretProvider(Protocol):
    def resolve(self, name: str) -> str: ...


@dataclass(frozen=True)
class EnvSecretProvider:
    """Resolve ``SecretRef(name)`` from ``<prefix><NAME>`` in the environment."""

    prefix: str = "LC_SECRET_"

    def resolve(self, name: str) -> str:
        value = os.environ.get(f"{self.prefix}{name.upper()}")
        if value is None:
            raise MissingSecret(name)
        return value
