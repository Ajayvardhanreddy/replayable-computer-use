"""Software-owned risk classification. The model never sets risk.

Fail closed: an unknown or unrecognized clickable action is NOT read-only. Only
clicks explicitly declared safe (an allowlist of known read-only controls, e.g.
a lookup's "Search") classify as READ_ONLY; every other click is treated as
consequential and must be confirmed. Reads (observe/extract/type/scroll) have no
persistent side effect and are read-only. The authority principle is fixed: risk
is derived here, not accepted from the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from computer_use.model import ProposedActionType, RiskClass, TargetDescriptor

_READ_ONLY_ACTIONS = frozenset(
    {
        ProposedActionType.OBSERVE,
        ProposedActionType.EXTRACT,
        ProposedActionType.TYPE,
        ProposedActionType.SCROLL,
    }
)


@dataclass(frozen=True)
class RiskClassifier:
    # Explicit allowlist of control names that are known-safe read-only clicks.
    safe_click_names: frozenset[str] = frozenset()

    def classify(self, action: ProposedActionType, target: TargetDescriptor) -> RiskClass:
        if action in _READ_ONLY_ACTIONS:
            return RiskClass.READ_ONLY
        if action is ProposedActionType.CLICK:
            name = (target.name or "").strip().lower()
            safe = {n.strip().lower() for n in self.safe_click_names}
            if name and name in safe:
                return RiskClass.READ_ONLY
            # Unknown/unrecognized click: fail closed, never silently read-only.
            return RiskClass.CONSEQUENTIAL_WRITE
        return RiskClass.CONSEQUENTIAL_WRITE
