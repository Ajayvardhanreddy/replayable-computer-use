"""In-memory record of sub-accounts created through the mutation flow.

Synthetic and process-local. It exists so a consequential write has a real, readable
effect: the commit endpoint records a created sub-account, and the member profile (the
independent read path used for read-back) reflects it. A dispatch counter records how
many times the commit endpoint was invoked, so a test can prove an uncertain mutation is
never re-dispatched.
"""

from __future__ import annotations

# The label of the sub-account the demo capability opens. A single fixed label keeps the
# flow minimal (the point is commit semantics, not a form builder).
SUB_ACCOUNT_LABEL = "Share Savings Sub"

_created: dict[str, list[str]] = {}
_acknowledged: set[str] = set()
_dispatch_count = 0


def record_commit_dispatch() -> None:
    """Count a commit endpoint invocation (whether or not it creates anything)."""
    global _dispatch_count
    _dispatch_count += 1


def create_sub_account(member_number: str) -> None:
    _created.setdefault(member_number, []).append(SUB_ACCOUNT_LABEL)


def has_sub_account(member_number: str) -> bool:
    return bool(_created.get(member_number))


def created_sub_accounts(member_number: str) -> list[str]:
    return list(_created.get(member_number, []))


def commit_dispatch_count() -> int:
    return _dispatch_count


def acknowledge_verification(member_number: str) -> None:
    """Record that an operator acknowledged the post-commit verification dialog."""
    _acknowledged.add(member_number)


def is_acknowledged(member_number: str) -> bool:
    return member_number in _acknowledged


def reset() -> None:
    """Clear all created sub-accounts, acknowledgements, and the counter (test isolation)."""
    global _dispatch_count
    _created.clear()
    _acknowledged.clear()
    _dispatch_count = 0
