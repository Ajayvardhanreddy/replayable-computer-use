"""The Cap-B replay CLI wiring: operator-scoped approval and the read/write profiles.

Replaying a write is sanctioned by the operator running the command; the confirmation is
scoped to exactly that capability's own consequential operation (nothing else), and the
write profile supplies an authoritative read source. The model can never produce either.
"""

from pathlib import Path

import pytest
import typer

from computer_use.cli import _capability_b_spec, _replay_confirmation, _replay_profile
from computer_use.model import (
    Capability,
    CapabilityTarget,
    ClickAction,
    Condition,
    Heading,
    InputSpec,
    MutationVerification,
    OutcomeClass,
    ParamType,
    ProposedActionType,
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


def test_capability_b_spec_declares_the_account_already_exists_outcome() -> None:
    # The real discovery spec (not the handcrafted mutation fixture) binds the app's known
    # duplicate-sub-account rejection to the Create Account commit, so genuine discovery
    # compiles ACCOUNT_ALREADY_EXISTS onto the write step. The compiler's binding mechanism
    # itself is proven in test_compiler; this pins the app-specific declaration.
    spec = _capability_b_spec("Open a share savings sub-account for this member")
    matches = [b for b in spec.business_outcomes if b.outcome.code == "ACCOUNT_ALREADY_EXISTS"]
    assert len(matches) == 1
    binding = matches[0]
    assert binding.action is ProposedActionType.CLICK
    assert binding.target is not None and binding.target.name == "Create Account"
    assert binding.outcome.outcome_class is OutcomeClass.BUSINESS_OUTCOME
    assert binding.outcome.detector.text_present == "A sub-account of this type already exists."


def test_canonical_capability_b_artifact_carries_the_business_outcome() -> None:
    # End-to-end proof on the committed, genuinely-discovered artifact (not a fixture): the
    # spec's declaration was compiled onto the single consequential write step.
    artifact = Path(__file__).resolve().parents[2] / "evidence/capability/open_sub_account.v1.json"
    capability = Capability.model_validate_json(artifact.read_text(encoding="utf-8"))
    writes = [step for step in capability.steps if step.risk is RiskClass.CONSEQUENTIAL_WRITE]
    assert len(writes) == 1
    assert "ACCOUNT_ALREADY_EXISTS" in [outcome.code for outcome in writes[0].outcomes]


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(typer.BadParameter):
        _replay_profile("nope")
