"""Minimal capability-scope policy: an action-type allowlist.

The domain/route allowlist and richer scope rules are a later safety phase; this
proves that an action outside the capability's permitted vocabulary is denied.
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
