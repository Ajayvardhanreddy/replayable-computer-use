"""Enumerations shared across the contract layer."""

from __future__ import annotations

from enum import StrEnum


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    CONSEQUENTIAL_WRITE = "consequential_write"
    IRREVERSIBLE = "irreversible"


class OutcomeClass(StrEnum):
    """Families a declared step outcome can belong to.

    Escalation is intentionally NOT a member: escalation is represented at the
    run level by ``RunResult.Escalated`` / the human-in-the-loop path, not as a
    per-step business outcome class.
    """

    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class Sensitivity(StrEnum):
    NONE = "none"
    PII = "pii"
    FINANCIAL = "financial"
    SECRET = "secret"


class ParamType(StrEnum):
    STRING = "string"
    DECIMAL = "decimal"
    INT = "int"
    BOOL = "bool"
    DATE = "date"


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ControlOwner(StrEnum):
    """Who currently holds exclusive authority to act on a live session.

    At any instant exactly one owner may drive the session. Automation and a
    human never act simultaneously; ownership transfers are explicit and audited.
    """

    AUTOMATION = "automation"
    HUMAN = "human"


class EffectState(StrEnum):
    """Certainty about whether a consequential mutation reached the application.

    A consequential write is dispatched exactly once. The moment the dispatch call
    is invoked, the runtime can no longer assume the effect did not happen; from
    there only observable read-back moves the state forward. This is the smallest
    representation needed to decide whether retry is safe (only before dispatch).
    """

    NOT_DISPATCHED = "not_dispatched"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    AMBIGUOUS = "ambiguous"
