"""``observe_blocker`` describes only the blocking modal, scoped to its own controls.

This is what lets a replay intervention present Observed state focused on what is actually
blocking (a dialog and its Acknowledge control), rather than dumping every background control.
"""

from urllib.parse import quote

from computer_use.surface import PlaywrightSurface

_PAGE = (
    "<h1>Member Profile</h1>"
    "<a href='#'>Background Link</a>"
    "<div role='dialog' aria-modal='true' aria-labelledby='h'>"
    "  <h2 id='h'>System Notice</h2>"
    "  <p>Please acknowledge the account notice before continuing.</p>"
    "  <a href='#ack'>Acknowledge</a>"
    "</div>"
)


async def test_observe_blocker_scopes_to_dialog_controls() -> None:
    surface = PlaywrightSurface(headless=True)
    await surface.start()
    try:
        await surface.goto("data:text/html," + quote(_PAGE))
        blocker = await surface.observe_blocker()
        assert blocker is not None
        assert blocker.role == "dialog"
        assert blocker.name == "System Notice"
        assert blocker.text == "Please acknowledge the account notice before continuing."
        # Only the dialog's own control — the background link is excluded.
        assert [(c.role, c.name) for c in blocker.controls] == [("link", "Acknowledge")]
    finally:
        await surface.close()


async def test_observe_blocker_returns_none_without_modal() -> None:
    surface = PlaywrightSurface(headless=True)
    await surface.start()
    try:
        page = "<h1>Member Profile</h1><button>Search</button>"
        await surface.goto("data:text/html," + quote(page))
        assert await surface.observe_blocker() is None
    finally:
        await surface.close()
