"""The Cap-B replay CLI wiring: operator-scoped approval and the read/write profiles.

Replaying a write is sanctioned by the operator running the command; the confirmation is
scoped to exactly that capability's own consequential operation (nothing else), and the
write profile supplies an authoritative read source. The model can never produce either.
"""

import pytest
import typer

from computer_use.cli import _replay_confirmation, _replay_profile
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    Heading,
    InputSpec,
    MutationVerification,
    ParamType,
    RiskClass,
    Step,
    TargetDescriptor,
)


def _write_capability() -> Capability:
    return Capability(
        id="x.write_thing",
        version=1,
        target=CapabilityTarget(vendor="v", application_family="f"),
        inputs={"id": InputSpec(type=ParamType.STRING)},
        outputs={},
        steps=[
            Step(
                id="s1_commit",
                action=ClickAction(),
                target=TargetDescriptor(role="button", name="Commit"),
                risk=RiskClass.CONSEQUENTIAL_WRITE,
                postcondition=Condition(text_present="done"),
                verification=MutationVerification(
                    navigate=[
                        Step(
                            id="v1",
                            action=ClickAction(),
                            target=TargetDescriptor(role="link", name="Records"),
                            risk=RiskClass.READ_ONLY,
                        )
                    ],
                    page=Condition(heading=Heading(role="heading", name="Records")),
                    effect_present=Condition(text_present="Thing"),
                ),
            )
        ],
        success_checkpoint=Condition(text_present="done"),
    )


def test_confirmation_approves_only_the_capabilitys_own_write() -> None:
    policy = _replay_confirmation(_write_capability())
    assert policy.is_approved("x.write_thing:v1:s1_commit")
    assert not policy.is_approved("x.write_thing:v1:s_other")
    assert not policy.is_approved("other.capability:v1:s1_commit")


def test_write_profile_supplies_authoritative_read_source() -> None:
    profile = _replay_profile("open_sub_account")
    assert profile.authority is not None
    assert profile.authority.absence_is_authoritative() is True
    assert "Member Inquiry" in profile.safe_clicks


def test_read_profile_has_no_write_authority() -> None:
    profile = _replay_profile("member_lookup")
    assert profile.authority is None


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(typer.BadParameter):
        _replay_profile("nope")
