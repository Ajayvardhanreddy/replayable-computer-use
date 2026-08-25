"""Confirmation/approval policy for consequential actions.

Approval is bound to a specific trusted operation — a compiled step id — never to a
human-readable control name, so two same-named controls cannot share an approval.
An empty policy approves nothing: every consequential action stays blocked. Live
human approval is a separate concern (the human-takeover path), not this policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConfirmationPolicy:
    approved: frozenset[str] = field(default_factory=frozenset)

    def is_approved(self, operation_id: str | None) -> bool:
        return operation_id is not None and operation_id in self.approved
