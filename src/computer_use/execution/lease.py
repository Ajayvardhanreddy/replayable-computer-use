"""Control lease: the single source of truth for who may act on a live session.

Only one owner (automation or a human) holds authority at any instant. Every
transfer advances a monotonic epoch, so work prepared under an older epoch is
rejected after ownership changes. This prevents split-brain control of the same
browser session: the lease is checked at the trusted authority path before any
side effect, not merely reflected in an operator UI.
"""

from __future__ import annotations

from computer_use.model import ControlOwner


class ControlLeaseError(Exception):
    """Raised when an actor attempts to act without current ownership."""


class ControlLease:
    """Mutable ownership token guarding a single live session.

    The lease starts owned by automation at epoch 0. ``to_human`` / ``to_automation``
    are the only ways to change ownership, and each advances the epoch. Only a genuine
    ownership change is legal: a same-owner transition (a double take or double release)
    is rejected rather than silently re-advancing the epoch, since it is not a real
    hand-off and would corrupt the epoch fencing the lease exists to provide.
    """

    def __init__(self) -> None:
        self._owner = ControlOwner.AUTOMATION
        self._epoch = 0

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def epoch(self) -> int:
        return self._epoch

    def to_human(self) -> int:
        """Transfer authority to the human operator; returns the new epoch."""
        if self._owner is ControlOwner.HUMAN:
            raise ControlLeaseError("control is already held by the human operator")
        self._owner = ControlOwner.HUMAN
        self._epoch += 1
        return self._epoch

    def to_automation(self) -> int:
        """Return authority to automation; returns the new epoch."""
        if self._owner is ControlOwner.AUTOMATION:
            raise ControlLeaseError("control is already held by automation")
        self._owner = ControlOwner.AUTOMATION
        self._epoch += 1
        return self._epoch

    def assert_automation_may_act(self, captured_epoch: int | None = None) -> None:
        """Fail closed unless automation currently owns the lease.

        When ``captured_epoch`` is supplied it must equal the current epoch: work
        scheduled under a superseded epoch (e.g. before a human took over) is
        rejected even if automation has since regained ownership.
        """
        if self._owner is not ControlOwner.AUTOMATION:
            raise ControlLeaseError(
                f"automation may not act while owner is {self._owner.value!r}"
            )
        if captured_epoch is not None and captured_epoch != self._epoch:
            raise ControlLeaseError(
                f"stale automation work at epoch {captured_epoch}; current epoch is {self._epoch}"
            )
