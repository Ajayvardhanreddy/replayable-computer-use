"""Model egress (C22-C24): the observation sent to the model carries only structural
information — no resolved values, secrets, or cell text — and is bounded, not a full
DOM dump."""

from computer_use.discovery.agent import _minimize
from computer_use.discovery.model import ModelObservation
from computer_use.surface import Candidate


def test_minimize_drops_cell_value_from_egress() -> None:
    candidate = Candidate(
        id="c1", role="cell", text="$8,421.31", row="Share Savings", column="Current Balance"
    )
    minimized = _minimize(candidate)
    dumped = minimized.model_dump()
    assert "text" not in dumped  # ModelCandidate has no text field at all
    assert "8,421.31" not in minimized.model_dump_json()  # the financial value is not egressed
    assert minimized.row == "Share Savings"  # structural coordinates are retained


def test_model_observation_is_bounded_structural() -> None:
    obs = ModelObservation(
        route="/workspace/inquiry",
        candidates=[_minimize(Candidate(id="c1", role="textbox", name="Member Number"))],
        steps_remaining=5,
    )
    # only candidate structure + minimal run state; no raw value fields
    assert set(obs.model_dump()) == {
        "route",
        "candidates",
        "actions_taken",
        "obtained_outputs",
        "last_error",
        "steps_remaining",
    }
    assert obs.candidates[0].model_dump().get("text") is None
