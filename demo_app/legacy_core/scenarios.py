"""Deterministic runtime scenarios that can be injected into LegacyCore.

``member_not_found`` is intentionally NOT a scenario here: it is data-driven (an
unknown member number naturally has no record). Only genuine runtime conditions
that are independent of the data are modeled as injectable scenarios.
"""

from __future__ import annotations

from enum import StrEnum

SCENARIO_COOKIE = "lc_scenario"
SLOW_DELAY_SECONDS = 1.0


class Scenario(StrEnum):
    NORMAL = "normal"
    SLOW = "slow"
    UNEXPECTED_DIALOG = "unexpected_dialog"


def resolve_scenario(query_value: str | None, cookie_value: str | None) -> Scenario:
    """Resolve the active scenario: query param wins, then cookie, else normal.

    An unrecognized value falls back to ``NORMAL`` (fail closed to the safe default).
    """
    raw = query_value or cookie_value
    if raw is None:
        return Scenario.NORMAL
    try:
        return Scenario(raw)
    except ValueError:
        return Scenario.NORMAL
