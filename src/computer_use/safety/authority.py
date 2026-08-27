"""Trusted authority over a verification read source.

The artifact says *where and how* to verify a mutation. Whether an *absent* effect on
that source is an authoritative non-commit — versus a source that may lag the write and
whose absence is therefore ambiguous — is a property of the target environment, decided
here by trusted configuration. It is never serialized in the artifact, so a capability
can never escalate itself to authoritative.

The default is conservative: absence is not authoritative, so an absent effect stays
ambiguous (routes to a human) rather than being reported as a definite non-commit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorityPolicy:
    authoritative_absence: bool = False

    def absence_is_authoritative(self) -> bool:
        """True only when a loaded verification view with the effect absent is an
        immediately-consistent, authoritative non-commit for this environment."""
        return self.authoritative_absence
