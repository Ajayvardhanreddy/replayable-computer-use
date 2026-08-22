import pytest

from computer_use.model import TableCellTarget, TargetDescriptor
from computer_use.surface import PlaywrightSurface, TargetNotFoundError


async def test_observe_harvests_across_iframe(legacy_core_url: str) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{legacy_core_url}/")
        obs = await surface.observe()
        pairs = {(c.role, c.name) for c in obs.candidates}
        assert ("textbox", "Member Number") in pairs
        assert ("button", "Search") in pairs
        # candidates were harvested from inside the workspace iframe
        assert any(c.frame == "lc-workspace" for c in obs.candidates)
    finally:
        await surface.close()


async def test_type_click_navigate_and_extract_balance(legacy_core_url: str) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{legacy_core_url}/")
        member_box = TargetDescriptor(role="textbox", name="Member Number", frame="lc-workspace")
        search = TargetDescriptor(role="button", name="Search", frame="lc-workspace")
        await surface.type_text(member_box, "12345")
        await surface.click(search)
        await surface.wait_for_frame_url("/workspace/member/12345")
        balance = TargetDescriptor(
            frame="lc-workspace",
            table_cell=TableCellTarget(
                row_contains="Share Savings", column_header="Current Balance"
            ),
        )
        assert await surface.extract(balance) == "$8,421.31"
    finally:
        await surface.close()


async def test_missing_target_fails_closed(legacy_core_url: str) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{legacy_core_url}/")
        with pytest.raises(TargetNotFoundError):
            await surface.click(TargetDescriptor(role="button", name="Nonexistent"))
    finally:
        await surface.close()
