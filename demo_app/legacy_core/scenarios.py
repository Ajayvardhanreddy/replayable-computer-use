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
    # A test-world switch that forces the lookup to return no record regardless of the
    # supplied id. Ordinary product behaviour stays data-driven (an unknown member number
    # naturally has no record); this only gives the eval runner one consistent interface.
    NOT_FOUND = "not_found"
    # The live session is no longer valid mid-flow: the workspace returns a recognizable
    # expired-session state instead of the expected page.
    SESSION_EXPIRED = "session_expired"
    # The session/operator is not authorized to read this record: an access-denied state.
    # Distinct from VERIFICATION_REQUIRED, which is resolvable by supplying a credential.
    PERMISSION_DENIED = "permission_denied"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    VERIFICATION_REQUIRED = "verification_required"
    # Consequential-mutation scenarios. Each concerns the commit endpoint's completion:
    # COMMIT_THEN_TIMEOUT genuinely commits then withholds the response (a real client
    # timeout); COMMIT_AMBIGUOUS commits then returns an ambiguous page immediately;
    # COMMIT_DROPPED returns the same ambiguous page but does not commit; and
    # COMMIT_UNVERIFIABLE commits but leaves the read-back page unrenderable.
    COMMIT_THEN_TIMEOUT = "commit_then_timeout"
    COMMIT_AMBIGUOUS = "commit_ambiguous"
    COMMIT_DROPPED = "commit_dropped"
    COMMIT_UNVERIFIABLE = "commit_unverifiable"
    # The commit succeeds, but the independent read-back is blocked by an unexpected
    # dialog a human must clear. Unlike COMMIT_UNVERIFIABLE (permanently unreadable),
    # this ambiguity is *recoverable*: once acknowledged, the read-back can proceed.
    VERIFICATION_DIALOG = "verification_dialog"


# The commit endpoint holds the response this long under COMMIT_THEN_TIMEOUT — well beyond a
# consequential click's bounded timeout (e.g. 300ms) — so the client experiences a real timeout
# after the server has already committed. Held generously so the demonstrated
# "uncertain dispatch -> read-back recovers" path never races, even on a loaded or headed machine.
COMMIT_HOLD_SECONDS = 3.0


# The synthetic employee verification code that releases a flagged account. It is a
# credential only an authorized employee would hold; the automated lookup is not
# given it, which is what makes the flagged state one the capability cannot resolve.
EMPLOYEE_VERIFICATION_CODE = "4729"
VERIFIED_COOKIE = "lc_verified"


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
