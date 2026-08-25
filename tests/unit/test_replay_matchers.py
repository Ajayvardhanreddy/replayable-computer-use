"""Checkpoint matcher semantics: every allowed Condition matcher is evaluated,
none is silently ignored, and route matching is a narrow deterministic form."""

import pytest

from computer_use.execution.replay import _matches, _success_satisfied
from computer_use.model import Condition, Heading
from computer_use.safety import route_matches


class _MatchSurface:
    """Minimal surface exposing only what the matchers evaluate."""

    def __init__(
        self, *, texts: set[str] | None = None, headings: set[str] | None = None, route: str = "/"
    ) -> None:
        self._texts = texts or set()
        self._headings = headings or set()
        self._route = route

    async def has_text(self, text: str) -> bool:
        return text in self._texts

    async def has_heading(self, name: str) -> bool:
        return name in self._headings

    async def current_route(self) -> str:
        return self._route


def test_route_matches_param_is_one_segment_and_anchored() -> None:
    assert route_matches("/workspace/member/:id", "/workspace/member/12345")
    assert route_matches("/workspace/member/:id", "/workspace/member/54321")
    # a :param matches exactly one segment, so a trailing segment must not match
    assert not route_matches("/workspace/member/:id", "/workspace/member/12345/detail")
    assert not route_matches("/workspace/member/:id", "/workspace/inquiry")


def test_route_matches_escapes_literals() -> None:
    # the '.' is a literal, not a regex wildcard
    assert route_matches("/a.b", "/a.b")
    assert not route_matches("/a.b", "/axb")


async def test_heading_matcher_uses_heading_identity() -> None:
    heading = Condition(heading=Heading(role="heading", name="Member Profile"))
    surface = _MatchSurface(headings={"Member Profile"}, texts={"Member Profile is loading"})
    assert await _matches(surface, heading)
    # text presence of the same string must NOT satisfy a heading checkpoint
    plain = _MatchSurface(headings=set(), texts={"Member Profile"})
    assert not await _matches(plain, heading)


async def test_text_present_and_recursive_any_of() -> None:
    surface = _MatchSurface(texts={"Member record not found"})
    assert await _matches(surface, Condition(text_present="Member record not found"))
    any_of = Condition(
        any_of=[
            Condition(text_present="nope"),
            Condition(any_of=[Condition(text_present="Member record not found")]),
        ]
    )
    assert await _matches(surface, any_of)
    assert not await _matches(_MatchSurface(), any_of)


async def test_route_matcher_via_condition() -> None:
    surface = _MatchSurface(route="/workspace/member/12345")
    assert await _matches(surface, Condition(route_pattern="/workspace/member/:id"))
    assert not await _matches(surface, Condition(route_pattern="/workspace/inquiry"))


async def test_output_present_is_not_a_live_condition() -> None:
    with pytest.raises(ValueError, match="success-checkpoint matcher"):
        await _matches(_MatchSurface(), Condition(output_present="savings_balance"))


async def test_success_satisfied_checks_outputs_and_live_matchers() -> None:
    surface = _MatchSurface(headings={"Member Profile"})
    output_only = Condition(output_present="savings_balance")
    assert await _success_satisfied(surface, output_only, {"savings_balance": "1.00"})
    assert not await _success_satisfied(surface, output_only, {})
    # a success checkpoint may also require a live matcher
    combined = Condition(
        output_present="savings_balance",
        heading=Heading(role="heading", name="Member Profile"),
    )
    assert await _success_satisfied(surface, combined, {"savings_balance": "1.00"})
    assert not await _success_satisfied(_MatchSurface(), combined, {"savings_balance": "1.00"})
