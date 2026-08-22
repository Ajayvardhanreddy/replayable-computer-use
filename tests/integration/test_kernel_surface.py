from computer_use.execution import TrustedKernel, ValueResolver
from computer_use.model import ParameterRef, ProposedAction, ProposedActionType
from computer_use.safety import Policy, RiskClassifier
from computer_use.surface import Candidate, PlaywrightSurface

_ALLOWED = frozenset(
    {ProposedActionType.CLICK, ProposedActionType.TYPE, ProposedActionType.EXTRACT}
)


def _find(candidates: list[Candidate], **attrs: str) -> Candidate:
    for candidate in candidates:
        if all(getattr(candidate, key) == value for key, value in attrs.items()):
            return candidate
    raise AssertionError(f"no candidate matching {attrs}")


async def test_kernel_drives_real_surface_with_provenance(legacy_core_url: str) -> None:
    surface = PlaywrightSurface()
    await surface.start()
    try:
        await surface.goto(f"{legacy_core_url}/")
        kernel = TrustedKernel(
            surface,
            Policy(allowed_actions=_ALLOWED),
            RiskClassifier(safe_click_names=frozenset({"Search"})),
            ValueResolver({"member_number": "12345"}),
        )

        obs = await surface.observe()
        by_id = {c.id: c for c in obs.candidates}
        member = _find(obs.candidates, role="textbox", name="Member Number")
        search = _find(obs.candidates, role="button", name="Search")

        # The model proposes a symbolic ParameterRef; the kernel substitutes 12345.
        await kernel.execute(
            ProposedAction(
                action=ProposedActionType.TYPE,
                candidate_id=member.id,
                value=ParameterRef(name="member_number"),
            ),
            by_id,
        )
        await kernel.execute(
            ProposedAction(action=ProposedActionType.CLICK, candidate_id=search.id), by_id
        )
        await surface.wait_for_frame_url("/workspace/member/12345")

        obs2 = await surface.observe()
        balance = _find(
            obs2.candidates, role="cell", row="Share Savings", column="Current Balance"
        )
        result = await kernel.execute(
            ProposedAction(
                action=ProposedActionType.EXTRACT,
                candidate_id=balance.id,
                output="savings_balance",
            ),
            {c.id: c for c in obs2.candidates},
        )
        assert result.extracted == "$8,421.31"
    finally:
        await surface.close()
