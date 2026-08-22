"""Synthetic member/account records for LegacyCore. No real PII.

Balances are pre-formatted display strings. One row (Share Draft) has Available <
Current to reflect a hold, matching how credit-union cores compute available balance
(current minus secured funds).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    type_description: str
    suffix: str
    current_balance: str
    available_balance: str
    status: str


@dataclass(frozen=True)
class Member:
    member_number: str
    name: str
    status: str
    member_since: str
    branch: str
    relationship: str
    warning: str
    accounts: tuple[Account, ...]


_MEMBERS: dict[str, Member] = {
    "12345": Member(
        member_number="12345",
        name="ALICE EXAMPLE",
        status="ACTIVE",
        member_since="03/12/2009",
        branch="014",
        relationship="PRIMARY",
        warning="none",
        accounts=(
            Account("Share Savings", "00", "8,421.31", "8,421.31", "OPEN"),
            Account("Share Draft", "10", "2,104.82", "1,984.82", "OPEN"),
            Account("Certificate", "20", "15,000.00", "15,000.00", "OPEN"),
        ),
    ),
    "54321": Member(
        member_number="54321",
        name="BOB SAMPLE",
        status="ACTIVE",
        member_since="07/22/2015",
        branch="014",
        relationship="PRIMARY",
        warning="none",
        accounts=(
            Account("Share Savings", "00", "312.45", "312.45", "OPEN"),
            Account("Share Draft", "10", "1,050.00", "1,050.00", "OPEN"),
        ),
    ),
}


def find_member(member_number: str) -> Member | None:
    """Return the member for a number, or None if no such record exists."""
    return _MEMBERS.get(member_number.strip())
