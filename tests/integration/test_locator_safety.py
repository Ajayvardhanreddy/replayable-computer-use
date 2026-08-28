"""Role/name resolution goes through Playwright's typed locator API, so a page-derived
accessible name is never interpolated into selector syntax.

These drive a real browser against inline pages (no LegacyCore) to prove that a name
containing quotes/brackets resolves, that exact matching is preserved, and that duplicate
names still fail closed as ambiguous.
"""

import urllib.parse

import pytest

from computer_use.model import TargetDescriptor
from computer_use.surface import PlaywrightSurface
from computer_use.surface.base import TargetAmbiguousError

_HOSTILE = 'Say "hi" [ok]'


def _data_url(body: str) -> str:
    html = f"<!doctype html><meta charset=utf-8><body>{body}</body>"
    return "data:text/html," + urllib.parse.quote(html)


async def _surface(body: str) -> PlaywrightSurface:
    surface = PlaywrightSurface()
    await surface.start()
    await surface.goto(_data_url(body))
    return surface


async def test_accessible_name_with_quotes_and_brackets_resolves() -> None:
    # The old string-selector form (`role=button[name="..."]`) would break on these characters;
    # the typed API resolves and acts on it.
    surface = await _surface(f"<button>{_HOSTILE}</button>")
    try:
        target = TargetDescriptor(role="button", name=_HOSTILE)
        assert await surface.count(target) == 1
        await surface.click(target)  # resolves + clicks without a selector-syntax error
    finally:
        await surface.close()


async def test_duplicate_name_fails_closed_as_ambiguous() -> None:
    surface = await _surface("<button>Dup</button><button>Dup</button>")
    try:
        target = TargetDescriptor(role="button", name="Dup")
        assert await surface.count(target) == 2
        with pytest.raises(TargetAmbiguousError):
            await surface.click(target)
    finally:
        await surface.close()


async def test_exact_match_does_not_match_a_superstring() -> None:
    # Exact resolution: "Save" must not match "Save As" (precise targeting, no accidental hit).
    surface = await _surface("<button>Save As</button>")
    try:
        assert await surface.count(TargetDescriptor(role="button", name="Save")) == 0
    finally:
        await surface.close()


async def test_wrong_name_resolves_to_zero() -> None:
    surface = await _surface("<button>Real</button>")
    try:
        assert await surface.count(TargetDescriptor(role="button", name="Nope")) == 0
    finally:
        await surface.close()
