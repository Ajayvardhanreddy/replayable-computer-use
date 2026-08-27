"""The compiler derives a consequential write's verification recipe generically.

It works on any trace shaped as "one consequential write, then a read-only re-derivation
ending in the declared success extract" — with no knowledge of the capability, its labels,
or the target app. The post-write segment is lifted into a single embedded recipe on the
write step (no duplication), and non-read-only or non-unique verifications fail closed.
"""

import pytest

from computer_use.discovery.compiler import CapabilityValidationError, GoalSpec, compile_capability
from computer_use.discovery.trace import DiscoveryTrace, TraceStep
from computer_use.model import (
    CapabilityTarget,
    InputSpec,
    OutputSpec,
    ParameterRef,
    ParamType,
    ProposedActionType,
    RiskClass,
    TableCellTarget,
    TargetDescriptor,
)

# Deliberately unrelated to LegacyCore / open_sub_account: proves genericity.
_SPEC = GoalSpec(
    capability_id="demo.finalize_request",
    goal="Finalize the request and confirm it in the records.",
    target=CapabilityTarget(vendor="acme", application_family="generic"),
    inputs={"request_id": InputSpec(type=ParamType.STRING)},
    outputs={"request_state": OutputSpec(type=ParamType.STRING)},
    success_output="request_state",
)


def _t(action, *, target=None, risk=RiskClass.READ_ONLY, value=None, output=None, landmark=None,
       before=None) -> TraceStep:
    return TraceStep(
        action=action, target=target or TargetDescriptor(role="button", name="x"), risk=risk,
        value=value, output=output, observed_landmark=landmark, heading_before=before,
        route="/records",
    )


def _finalize_trace() -> DiscoveryTrace:
    param = ParameterRef(name="request_id")
    tb = TargetDescriptor(role="textbox", name="Request Id")
    return DiscoveryTrace(steps=[
        _t(ProposedActionType.TYPE, target=tb, value=param),
        _t(ProposedActionType.CLICK, target=TargetDescriptor(role="button", name="Open Record"),
           landmark="Record Detail", before="Search"),
        # the single consequential write
        _t(ProposedActionType.CLICK,
           target=TargetDescriptor(role="button", name="Finalize Request"),
           risk=RiskClass.CONSEQUENTIAL_WRITE, landmark="Finalized", before="Record Detail"),
        # read-only independent re-derivation
        _t(ProposedActionType.CLICK, target=TargetDescriptor(role="link", name="Records"),
           landmark="Records List", before="Finalized"),
        _t(ProposedActionType.TYPE, target=tb, value=param),
        _t(ProposedActionType.CLICK, target=TargetDescriptor(role="button", name="Open Record"),
           landmark="Record Detail", before="Records List"),
        _t(ProposedActionType.EXTRACT,
           target=TargetDescriptor(table_cell=TableCellTarget(
               row_contains="Special Product X", column_header="State")),
           output="request_state"),
    ])


def test_verification_is_derived_generically() -> None:
    cap = compile_capability(_finalize_trace(), _SPEC)
    # Post-write steps are lifted out: top-level ends at the write.
    assert len(cap.steps) == 3
    write = cap.steps[2]
    assert write.risk is RiskClass.CONSEQUENTIAL_WRITE
    assert write.verification is not None
    v = write.verification
    # Navigate is the discovered read-only re-derivation (3 steps), extract is terminal.
    assert len(v.navigate) == 3
    assert all(s.risk is RiskClass.READ_ONLY for s in v.navigate)
    assert v.extract is not None and v.extract.output == "request_state"
    # Effect identity + view are captured from the discovered target/landmark, not invented.
    assert v.effect_present.text_present == "Special Product X"
    assert v.page.heading is not None and v.page.heading.name == "Record Detail"
    # Parameter provenance survives into the verification's re-query.
    type_step = next(s for s in v.navigate if s.action.type == "type")
    assert isinstance(type_step.action.value, ParameterRef)
    assert type_step.action.value.name == "request_id"


def test_read_only_capability_compiles_flat() -> None:
    # No consequential write -> every step stays top-level, no verification.
    trace = DiscoveryTrace(steps=[
        _t(ProposedActionType.TYPE, target=TargetDescriptor(role="textbox", name="Id"),
           value=ParameterRef(name="request_id")),
        _t(ProposedActionType.EXTRACT,
           target=TargetDescriptor(table_cell=TableCellTarget(
               row_contains="Special Product X", column_header="State")),
           output="request_state"),
    ])
    cap = compile_capability(trace, _SPEC)
    assert len(cap.steps) == 2
    assert all(s.verification is None for s in cap.steps)


def test_non_read_only_verification_step_is_rejected() -> None:
    trace = _finalize_trace()
    # Corrupt a post-write step to be consequential.
    trace.steps[3] = _t(
        ProposedActionType.CLICK, target=TargetDescriptor(role="button", name="Delete"),
        risk=RiskClass.CONSEQUENTIAL_WRITE,
    )
    with pytest.raises(CapabilityValidationError):
        compile_capability(trace, _SPEC)


def test_two_consequential_writes_are_rejected() -> None:
    trace = _finalize_trace()
    trace.steps[1] = _t(
        ProposedActionType.CLICK, target=TargetDescriptor(role="button", name="Approve"),
        risk=RiskClass.CONSEQUENTIAL_WRITE,
    )
    with pytest.raises(CapabilityValidationError):
        compile_capability(trace, _SPEC)


def test_missing_terminal_extract_is_rejected() -> None:
    trace = _finalize_trace()
    trace.steps = trace.steps[:-1]  # drop the extract of the success output
    with pytest.raises(CapabilityValidationError):
        compile_capability(trace, _SPEC)


def test_write_before_effect_view_is_rejected() -> None:
    # The write is reached before any step visits the effect view, so no pre-dispatch
    # baseline (effect-absent) can be established -> the mutation would be unattributable.
    param = ParameterRef(name="request_id")
    tb = TargetDescriptor(role="textbox", name="Request Id")
    trace = DiscoveryTrace(steps=[
        _t(ProposedActionType.TYPE, target=tb, value=param),
        # write immediately — nothing before it reaches the effect view ("Record Detail")
        _t(ProposedActionType.CLICK,
           target=TargetDescriptor(role="button", name="Finalize Request"),
           risk=RiskClass.CONSEQUENTIAL_WRITE, landmark="Finalized", before="Search"),
        _t(ProposedActionType.CLICK, target=TargetDescriptor(role="link", name="Records"),
           landmark="Records List", before="Finalized"),
        _t(ProposedActionType.TYPE, target=tb, value=param),
        _t(ProposedActionType.CLICK, target=TargetDescriptor(role="button", name="Open Record"),
           landmark="Record Detail", before="Records List"),
        _t(ProposedActionType.EXTRACT,
           target=TargetDescriptor(table_cell=TableCellTarget(
               row_contains="Special Product X", column_header="State")),
           output="request_state"),
    ])
    with pytest.raises(CapabilityValidationError):
        compile_capability(trace, _SPEC)
