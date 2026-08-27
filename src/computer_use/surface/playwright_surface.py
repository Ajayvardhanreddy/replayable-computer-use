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
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
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
    BlockerObservation,
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

# A blocking modal is identified by ARIA semantics, not by matching any specific
# copy: a visible element with the dialog role marked aria-modal. This is a generic
# structural signal for "an interactive state the deterministic flow does not model",
# never a check for particular dialog text.
_BLOCKING_DIALOG_JS = """
() => {
  const dialogs = [...document.querySelectorAll('[role=dialog][aria-modal=true]')];
  return dialogs.some((d) => {
    if (d.hasAttribute('hidden')) return false;
    const style = window.getComputedStyle(d);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = d.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
}
"""

# Structural description of the first visible modal dialog: its accessible name, the notice
# body text, and the interactable controls it contains. Structural only (no pixels), scoped to
# the dialog subtree so an intervention focuses on what is blocking rather than the whole page.
_BLOCKER_JS = """
() => {
  const dialogs = [...document.querySelectorAll('[role=dialog][aria-modal=true]')];
  const vis = dialogs.find((d) => {
    if (d.hasAttribute('hidden')) return false;
    const s = window.getComputedStyle(d);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = d.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!vis) return null;
  let name = (vis.getAttribute('aria-label') || '').trim();
  if (!name) {
    const lb = vis.getAttribute('aria-labelledby');
    const t = lb ? document.getElementById(lb) : null;
    name = t ? (t.innerText || '').trim() : '';
  }
  if (!name) {
    const h = vis.querySelector('h1,h2,h3');
    name = h ? (h.innerText || '').trim() : '';
  }
  const p = vis.querySelector('p');
  const text = ((p ? p.innerText : vis.innerText) || '').trim();
  const controls = [];
  vis.querySelectorAll('input[type=text], input:not([type])').forEach((el) => {
    const l = (el.labels && el.labels.length) ? (el.labels[0].innerText || '').trim()
      : (el.getAttribute('aria-label') || '').trim();
    controls.push({ role: 'textbox', name: l });
  });
  vis.querySelectorAll('button').forEach((el) => {
    controls.push({ role: 'button', name: (el.innerText || '').trim() });
  });
  vis.querySelectorAll('a[href]').forEach((el) => {
    controls.push({ role: 'link', name: (el.innerText || '').trim() });
  });
  return { name, text, controls };
}
"""

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
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # A stable identifier for this session, independent of any driver internals.
        # It is assigned once at construction and never changes, so a same-session
        # handoff can be asserted without depending on private Playwright handles.
        self._session_id = uuid4().hex

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_live(self) -> bool:
        return self._page is not None

    @property
    def context(self) -> BrowserContext:
        """The live browser context (same object across a same-session handoff)."""
        if self._context is None:
            raise SurfaceError("surface not started")
        return self._context

    @property
    def page(self) -> Page:
        """The live page (same object across a same-session handoff)."""
        return self._pg()

    async def start(self) -> None:
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
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

    async def click(self, target: TargetDescriptor, *, timeout_ms: int | None = None) -> None:
        locator = await self._unique_locator(target)
        await self._act(locator.click())
        # For a consequential commit, wait for the resulting completion within a bounded
        # budget: if the server withholds normal completion the wait raises, converting a
        # hang into an uncertain dispatch (the effect may already have committed).
        if timeout_ms is not None:
            try:
                await self._pg().wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightError as error:
                raise _translate(error) from error

    async def type_text(
        self, target: TargetDescriptor, text: str, *, submit: bool = False
    ) -> None:
        locator = await self._unique_locator(target)
        await self._act(locator.fill(text))
        if submit:
            # Submit the field's form (implicit submission on Enter). Used when a
            # control has no separate submit button.
            await self._act(locator.press("Enter"))

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

    async def has_blocking_dialog(self) -> bool:
        for frame in self._pg().frames:
            if cast(bool, await self._safe_eval(frame, _BLOCKING_DIALOG_JS)):
                return True
        return False

    async def observe_blocker(self) -> BlockerObservation | None:
        """Structural description of the current blocking modal, or None. Concrete-surface
        only (not part of the Surface protocol): the operator console uses it to scope an
        intervention to the blocker's own controls and text, never the whole page."""
        for frame in self._pg().frames:
            raw = cast("dict[str, Any] | None", await self._safe_eval(frame, _BLOCKER_JS))
            if not raw:
                continue
            controls = [
                Candidate(id=f"b{i}", role=str(item["role"]), name=(item.get("name") or None))
                for i, item in enumerate(raw.get("controls", []), start=1)
            ]
            # Minimize the notice text: collapse whitespace and cap length (structural aid,
            # not a place to surface long or sensitive copy).
            text = " ".join((raw.get("text") or "").split())[:200] or None
            return BlockerObservation(
                role="dialog", name=(raw.get("name") or None), text=text, controls=controls
            )
        return None

    async def current_route(self) -> str:
        return urlparse(self._pg().url).path

    async def current_url(self) -> str:
        return self._pg().url

    async def scope_urls(self) -> list[str]:
        """Every real document URL in the session: the top page and each subframe.

        The meaningful workspace runs inside an iframe, so an in-scope top page can host
        an out-of-scope subframe; navigation scope is judged against all of them. Blank or
        unnavigated frames (``about:blank``, ``about:srcdoc``) carry no origin to judge and
        are skipped. The top page is always included, even if every frame is blank.
        """
        urls: list[str] = []
        for frame in self._pg().frames:
            url = frame.url
            if not url or url.startswith("about:"):
                continue
            urls.append(url)
        if not urls:
            urls.append(self._pg().url)
        return urls

    async def wait_settled(self) -> None:
        try:
            await self._pg().wait_for_load_state("networkidle")
            # A subframe redirect (POST -> 303 -> GET inside an iframe) can still be in
            # flight when the page reports networkidle. Wait until frame URLs stop
            # changing so an action does not land on a document about to be replaced.
            await self._wait_frames_stable()
        except PlaywrightError:
            pass

    async def _wait_frames_stable(self, *, poll_ms: int = 50, budget_ms: int = 2000) -> None:
        previous = tuple(frame.url for frame in self._pg().frames)
        waited = 0
        while waited < budget_ms:
            await self._pg().wait_for_timeout(poll_ms)
            current = tuple(frame.url for frame in self._pg().frames)
            if current == previous:
                return  # no navigation in flight
            previous = current
            waited += poll_ms

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
            self._context = None
