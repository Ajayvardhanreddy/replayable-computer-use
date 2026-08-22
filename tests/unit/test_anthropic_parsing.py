import pytest

from computer_use.discovery import ModelOutputError
from computer_use.discovery.anthropic_model import parse_proposal
from computer_use.model import ParameterRef, ProposedActionType


def test_parse_plain_json() -> None:
    proposal = parse_proposal('{"action":"click","candidate_id":"c1"}')
    assert proposal.action is ProposedActionType.CLICK
    assert proposal.candidate_id == "c1"


def test_parse_fenced_json_with_parameter_value() -> None:
    text = (
        "```json\n"
        '{"action":"type","candidate_id":"c2",'
        '"value":{"source":"parameter","name":"member_number"}}\n'
        "```"
    )
    proposal = parse_proposal(text)
    assert proposal.action is ProposedActionType.TYPE
    assert isinstance(proposal.value, ParameterRef)
    assert proposal.value.name == "member_number"


def test_parse_extracts_json_from_surrounding_prose() -> None:
    text = 'Sure, I will click Search.\n{"action":"click","candidate_id":"c3"} Done.'
    proposal = parse_proposal(text)
    assert proposal.action is ProposedActionType.CLICK
    assert proposal.candidate_id == "c3"


def test_parse_rejects_raw_scalar_value() -> None:
    # A raw scalar can never cross the ProposedAction boundary, even from the model.
    with pytest.raises(ModelOutputError):
        parse_proposal('{"action":"type","candidate_id":"c2","value":"12345"}')


def test_parse_raises_model_output_error_when_no_json() -> None:
    with pytest.raises(ModelOutputError):
        parse_proposal("I'm not sure which control to use.")
