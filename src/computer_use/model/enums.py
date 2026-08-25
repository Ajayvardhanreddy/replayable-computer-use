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
