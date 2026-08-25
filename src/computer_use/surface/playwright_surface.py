"""Playwright-backed Surface implementation (async, single browser session).

Perception uses controlled, trusted JavaScript to harvest an accessibility-
oriented candidate inventory (roles, accessible names, and table row/column
context) across all frames — this is surface code, never model-proposed script.
Resolution prefers semantic role/name locators over CSS.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, TypeVar, cast
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    Frame,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from computer_use.model import TargetDescriptor

from .base import (
    Candidate,
    Observation,
    StructuralSnapshot,
    SurfaceDriverError,
    SurfaceError,
    SurfaceTransientError,
    TargetAmbiguousError,
    TargetNotFoundError,
)

_HARVEST_JS = """
() => {
  const out = [];
  const nm = (el) => {
    if (el.labels && el.labels.length) {
      return (el.labels[0].innerText || '').trim();
    }
    const al = el.getAttribute('aria-label');
    if (al) return al.trim();
    return (el.innerText || el.value || '').trim();
  };
  const inputs = 'input[type=text], input:not([type])';
  document.querySelectorAll(inputs).forEach((el) => {
    out.push({ role: 'textbox', name: nm(el), filled: !!(el.value && el.value.trim()) });
  });
  document.querySelectorAll('button').forEach((el) => {
    out.push({ role: 'button', name: (el.innerText || '').trim() });
  });
  document.querySelectorAll('a[href]').forEach((el) => {
    out.push({ role: 'link', name: (el.innerText || '').trim() });
  });
  document.querySelectorAll('table').forEach((tbl) => {
    const ths = [...tbl.querySelectorAll('thead th')];
    if (!ths.length) return;
    const hdrs = ths.map((th) => (th.innerText || '').trim());
    tbl.querySelectorAll('tbody tr').forEach((tr) => {
      const cells = [...tr.children];
      const rl = cells.length ? (cells[0].innerText || '').trim() : '';
      cells.forEach((td, i) => {
        out.push({
          role: 'cell',
          text: (td.innerText || '').trim(),
          row: rl,
          column: hdrs[i] || '',
        });
      });
    });
  });
  return out;
}
"""

_TABLE_CELL_JS = """
(args) => {
  const rc = args.rowContains;
  const ch = args.columnHeader;
  const results = [];
  document.querySelectorAll('table').forEach((tbl) => {
    const ths = [...tbl.querySelectorAll('thead th')];
    if (!ths.length) return;
    const ci = ths.findIndex((th) => (th.innerText || '').trim() === ch);
    if (ci < 0) return;
    tbl.querySelectorAll('tbody tr').forEach((tr) => {
      const cells = [...tr.children];
      const rl = cells.length ? (cells[0].innerText || '').trim() : '';
      if (rl.includes(rc) && cells[ci]) {
        results.push((cells[ci].innerText || '').trim());
      }
    });
  });
  return results;
}
"""

_HEADINGS_JS = """
() => {
  const hs = [...document.querySelectorAll('h1,h2')];
  return hs.map((h) => (h.innerText || '').trim());
}
"""

_BODY_TEXT_JS = "() => (document.body ? document.body.innerText : '')"

# Retries for transient "execution context destroyed" errors during navigation.
_EVAL_RETRIES = 40
# How long to wait for a table cell to appear (e.g. while a page finishes loading).
_CELL_POLL_MS = 5000

_PRIMARY_HEADING_JS = """
() => {
  const h = document.querySelector('h1');
  if (!h) return null;
  const t = h.childNodes[0];
  return t ? (t.textContent || '').trim() : (h.innerText || '').trim();
}
"""

# Heading identity (h1/h2 primary text node, excluding child spans like a member id).
_HEADING_TEXTS_JS = """
() => {
  const hs = [...document.querySelectorAll('h1,h2')];
  return hs.map((h) => {
    const t = h.childNodes[0];
    return t ? (t.textContent || '').trim() : (h.innerText || '').trim();
  });
}
"""

# Narrow set of genuinely transient conditions worth a bounded retry (navigation races).
_TRANSIENT_SIGNATURES = (
    "execution context was destroyed",
    "frame was detached",
    "cannot find context with specified id",
)

_T = TypeVar("_T")


def _is_transient(error: PlaywrightError) -> bool:
    message = str(error).lower()
    return any(signature in message for signature in _TRANSIENT_SIGNATURES)


def _translate(error: PlaywrightError) -> SurfaceError:
    """Map a provider error to a Surface-level error; nothing else crosses the seam."""
    if _is_transient(error):
        return SurfaceTransientError(str(error))
    return SurfaceDriverError(str(error))


class PlaywrightSurface:
    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            context = await self._browser.new_context()
            self._page = await context.new_page()
        except PlaywrightError as error:
            raise SurfaceDriverError(str(error)) from error

    def _pg(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface not started")
        return self._page

    async def _act(self, awaitable: Awaitable[_T]) -> _T:
        """Await a driver call, translating any provider error to a SurfaceError."""
        try:
            return await awaitable
        except PlaywrightError as error:
            raise _translate(error) from error

    async def goto(self, url: str) -> None:
        await self._act(self._pg().goto(url, wait_until="networkidle"))

    async def _safe_eval(self, frame: Frame, script: str, arg: object = None) -> object:
        # Retry a genuinely transient navigation race under a bounded budget; a
        # non-transient driver error is translated and raised immediately (no retry).
        for attempt in range(_EVAL_RETRIES):
            try:
                return await frame.evaluate(script, arg)
            except PlaywrightError as error:
                if not _is_transient(error):
                    raise SurfaceDriverError(str(error)) from error
                if attempt == _EVAL_RETRIES - 1:
                    raise SurfaceTransientError(str(error)) from error
                await self._pg().wait_for_timeout(50)
        return None

    def _frames(self, target: TargetDescriptor) -> list[Frame]:
        frames = list(self._pg().frames)
        if target.frame:
            frames = [f for f in frames if f.name == target.frame]
        return frames

    def _locator(self, frame: Frame, target: TargetDescriptor) -> Locator:
        if target.role and target.name:
            return frame.locator(f'role={target.role}[name="{target.name}"]')
        if target.label:
            return frame.get_by_label(target.label, exact=True)
        if target.text:
            return frame.get_by_text(target.text, exact=True)
        raise TargetNotFoundError("unsupported target descriptor for this surface")

    async def observe(self) -> Observation:
        candidates: list[Candidate] = []
        counter = 0
        for frame in self._pg().frames:
            raw = cast(list[dict[str, Any]], await self._safe_eval(frame, _HARVEST_JS))
            for item in raw:
                counter += 1
                candidates.append(
                    Candidate(
                        id=f"c{counter}",
                        frame=frame.name or None,
                        role=str(item["role"]),
                        name=item.get("name"),
                        text=item.get("text"),
                        row=item.get("row"),
                        column=item.get("column"),
                        filled=item.get("filled"),
                    )
                )
        return Observation(route=urlparse(self._pg().url).path, candidates=candidates)

    async def _table_cell_values(self, target: TargetDescriptor) -> list[str]:
        cell = target.table_cell
        if cell is None:
            return []
        args = {"rowContains": cell.row_contains, "columnHeader": cell.column_header}
        waited = 0
        while True:
            values: list[str] = []
            for frame in self._frames(target):
                values.extend(cast(list[str], await self._safe_eval(frame, _TABLE_CELL_JS, args)))
            if values or waited >= _CELL_POLL_MS:
                return values
            await self._pg().wait_for_timeout(100)
            waited += 100

    async def count(self, target: TargetDescriptor) -> int:
        if target.table_cell is not None:
            return len(await self._table_cell_values(target))
        total = 0
        for frame in self._frames(target):
            total += await self._act(self._locator(frame, target).count())
        return total

    async def _unique_locator(self, target: TargetDescriptor) -> Locator:
        matches: list[Locator] = []
        for frame in self._frames(target):
            locator = self._locator(frame, target)
            found = await self._act(locator.count())
            if found > 1:
                raise TargetAmbiguousError(f"{found} matches in frame {frame.name!r}")
            if found == 1:
                matches.append(locator)
        if not matches:
            raise TargetNotFoundError("no element matched target")
        if len(matches) > 1:
            raise TargetAmbiguousError("target matched in multiple frames")
        return matches[0]

    async def click(self, target: TargetDescriptor) -> None:
        locator = await self._unique_locator(target)
        await self._act(locator.click())

    async def type_text(self, target: TargetDescriptor, text: str) -> None:
        locator = await self._unique_locator(target)
        await self._act(locator.fill(text))

    async def extract(self, target: TargetDescriptor) -> str:
        if target.table_cell is not None:
            values = await self._table_cell_values(target)
            if not values:
                raise TargetNotFoundError("no table cell matched")
            if len(values) > 1:
                raise TargetAmbiguousError("multiple table cells matched")
            return values[0]
        locator = await self._unique_locator(target)
        return (await self._act(locator.inner_text())).strip()

    async def capture(self) -> StructuralSnapshot:
        frames: list[str] = []
        landmarks: list[str] = []
        for frame in self._pg().frames:
            frames.append(frame.name or "main")
            # Heading primary text only (excludes child spans like a member id), so a
            # structural snapshot carries landmarks, never embedded record values.
            landmarks.extend(cast(list[str], await self._safe_eval(frame, _HEADING_TEXTS_JS)))
        return StructuralSnapshot(
            route=urlparse(self._pg().url).path, frames=frames, landmarks=landmarks
        )

    async def wait_for_frame_url(self, fragment: str, timeout_ms: int = 5000) -> None:
        page = self._pg()
        waited = 0
        while waited < timeout_ms:
            if any(fragment in frame.url for frame in page.frames):
                return
            await page.wait_for_timeout(100)
            waited += 100
        raise SurfaceError(f"timeout waiting for frame url containing {fragment!r}")

    async def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool:
        page = self._pg()
        waited = 0
        while waited < timeout_ms:
            for frame in page.frames:
                try:
                    body = cast(str, await frame.evaluate(_BODY_TEXT_JS))
                except PlaywrightError:
                    continue  # frame is mid-navigation/detached; retry next poll
                if text in body:
                    return True
            await page.wait_for_timeout(100)
            waited += 100
        return False

    async def has_text(self, text: str) -> bool:
        for frame in self._pg().frames:
            body = cast(str, await self._safe_eval(frame, _BODY_TEXT_JS))
            if text in body:
                return True
        return False

    async def has_heading(self, name: str) -> bool:
        for frame in self._pg().frames:
            headings = cast(list[str], await self._safe_eval(frame, _HEADING_TEXTS_JS))
            if name in headings:
                return True
        return False

    async def current_route(self) -> str:
        return urlparse(self._pg().url).path

    async def current_url(self) -> str:
        return self._pg().url

    async def wait_settled(self) -> None:
        try:
            await self._pg().wait_for_load_state("networkidle")
        except PlaywrightError:
            pass

    async def primary_heading(self) -> str | None:
        for frame in self._pg().frames:
            heading = cast(str | None, await self._safe_eval(frame, _PRIMARY_HEADING_JS))
            if heading:
                return heading
        return None

    async def wait_for_heading_change(
        self, previous: str | None, timeout_ms: int = 5000
    ) -> str | None:
        # Return the new heading only if it actually changed; on timeout return
        # None (no change) rather than the stale heading, so callers never treat
        # an unchanged page as a transition.
        waited = 0
        while waited < timeout_ms:
            current = await self.primary_heading()
            if current is not None and current != previous:
                return current
            await self._pg().wait_for_timeout(100)
            waited += 100
        return None

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except PlaywrightError as error:
            raise SurfaceDriverError(str(error)) from error
        finally:
            self._browser = None
            self._pw = None
            self._page = None
