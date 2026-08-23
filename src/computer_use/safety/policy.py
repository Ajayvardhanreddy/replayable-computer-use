"""Capability-scope policy: an allowlist of permitted action types.

This enforces the capability's permitted action vocabulary. Domain/route scoping is the
responsibility of the broader safety layer, not this check.
"""

from __future__ import annotations

from dataclasses import dataclass

from computer_use.model import (
    PolicyDecision,
    PolicyEffect,
    ProposedActionType,
    TargetDescriptor,
)


@dataclass(frozen=True)
class Policy:
    allowed_actions: frozenset[ProposedActionType]

    def check(self, action: ProposedActionType, target: TargetDescriptor) -> PolicyDecision:
        if action not in self.allowed_actions:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=f"action '{action.value}' is not permitted for this capability",
                rule="action_allowlist",
            )
        return PolicyDecision(
            effect=PolicyEffect.ALLOW, reason="action permitted", rule="action_allowlist"
        )
