"""Operator control resolution fails closed.

A human names a control by candidate id or label; the console resolves it against the *current*
live page. Duplicate labels and controls that have moved/vanished since the snapshot must be
refused with a clear message rather than acting on the wrong element — the same
ambiguity-fails-closed discipline the automation path uses.
"""

from computer_use.cli import (
    _Candidate,
    _is_sensitive,
    _observe_candidates,
    _parse_field_value,
    _resolve_live_target,
    _select_candidate,
)
from computer_use.model import TargetDescriptor


def test_parse_field_value_accepts_equals_and_id_space_forms() -> None:
    # A multi-word control name needs '=' to separate name from value.
    assert _parse_field_value("Employee Verification Code=4729") == (
        "Employee Verification Code",
        "4729",
    )
    # A candidate id can be followed by a space instead.
    assert _parse_field_value("c1 4729") == ("c1", "4729")
    assert _parse_field_value("c1=4729") == ("c1", "4729")
    # A value may contain spaces; only the first space splits the id form.
    assert _parse_field_value("c1 hello world") == ("c1", "hello world")
    # A field with no value returns value None -> the caller prompts (masked if sensitive).
    assert _parse_field_value("c1") == ("c1", None)
    # No field at all is unparseable (caller shows a usage hint, not a crash).
    assert _parse_field_value("") is None


def test_sensitive_field_detection() -> None:
    # Credential/one-time-value fields must never be typed inline.
    assert _is_sensitive("Employee Verification Code")
    assert _is_sensitive("Password") and _is_sensitive("One-Time PIN")
    # Ordinary fields are not masked.
    assert not _is_sensitive("Member Name")
    assert not _is_sensitive("Amount")


class _FakeOperator:
    """Minimal stand-in exposing only what candidate resolution needs."""

    def __init__(self, controls: list[str]) -> None:
        self._controls = controls

    async def visible_controls(self) -> list[str]:
        return self._controls


async def test_observe_numbers_controls() -> None:
    operator = _FakeOperator(["link:Acknowledge", "button:Transfer"])
    candidates = await _observe_candidates(operator)  # type: ignore[arg-type]
    assert candidates == [
        _Candidate("c1", "link", "Acknowledge"),
        _Candidate("c2", "button", "Transfer"),
    ]


def test_select_by_id_and_unique_label() -> None:
    displayed = [_Candidate("c1", "link", "Acknowledge"), _Candidate("c2", "button", "Cancel")]
    assert _select_candidate("c2", displayed) == displayed[1]
    assert _select_candidate("acknowledge", displayed) == displayed[0]


def test_select_ambiguous_label_fails_closed_with_ids() -> None:
    displayed = [_Candidate("c1", "button", "Continue"), _Candidate("c2", "button", "Continue")]
    result = _select_candidate("Continue", displayed)
    assert isinstance(result, str)
    assert "c1" in result and "c2" in result  # tells the human to disambiguate by id


def test_select_absent_control_fails_closed() -> None:
    result = _select_candidate("Nope", [_Candidate("c1", "link", "Acknowledge")])
    assert isinstance(result, str) and "no control" in result


async def test_resolve_live_unique_returns_target() -> None:
    operator = _FakeOperator(["link:Acknowledge", "button:Transfer"])
    target = await _resolve_live_target(operator, _Candidate("c1", "link", "Acknowledge"))  # type: ignore[arg-type]
    assert isinstance(target, TargetDescriptor)
    assert target.role == "link" and target.name == "Acknowledge"


async def test_resolve_live_stale_control_fails_closed() -> None:
    operator = _FakeOperator(["button:Transfer"])  # Acknowledge is gone
    result = await _resolve_live_target(operator, _Candidate("c1", "link", "Acknowledge"))  # type: ignore[arg-type]
    assert isinstance(result, str) and "no longer on the page" in result


async def test_resolve_live_now_ambiguous_fails_closed() -> None:
    operator = _FakeOperator(["button:Continue", "button:Continue"])
    result = await _resolve_live_target(operator, _Candidate("c1", "button", "Continue"))  # type: ignore[arg-type]
    assert isinstance(result, str) and "now matches 2 controls" in result
