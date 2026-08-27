"""The model never sees which runtime scenario is armed.

Scenario switches (a slow page, an interrupted commit, a verification gate) are a
property of the target environment, not information the agent may use to decide. This
proves the two egress transforms that build what the model receives — the minimized
candidate and the structural route label — cannot carry a scenario identifier, a
scenario-bearing URL, or a raw path parameter, regardless of what the live page holds.
"""

import json

from computer_use.discovery.agent import _minimize
from computer_use.discovery.model import ModelCandidate, ModelObservation
from computer_use.safety import route_label
from computer_use.surface import Candidate

_SCENARIO_TOKENS = (
    "commit_then_timeout",
    "commit_dropped",
    "commit_ambiguous",
    "commit_unverifiable",
    "verification_required",
    "unexpected_dialog",
    "scenario=",
    "scenario",
)
_ROUTES = frozenset(
    {"/", "/workspace/inquiry", "/workspace/member/:member_number",
     "/workspace/member/:member_number/sub-account"}
)


def test_egressed_candidate_has_no_field_that_could_carry_a_url_or_scenario() -> None:
    # Structural identity only: no `text`, `href`, or `url` field exists on the model
    # candidate, so a scenario-bearing attribute cannot be egressed by construction.
    fields = set(ModelCandidate.model_fields)
    assert fields == {"id", "role", "name", "frame", "row", "column", "filled"}
    assert "text" not in fields and "href" not in fields and "url" not in fields


def test_minimize_drops_scenario_bearing_text() -> None:
    # Even if a harvested element's text carried the armed scenario, minimization drops
    # text entirely — the model chooses by structure, never by a value it can read.
    harvested = Candidate(
        id="c1", role="link", name="Open Sub-Account",
        text="Open Sub-Account ?scenario=commit_then_timeout", frame="lc-workspace",
    )
    egressed = _minimize(harvested)
    dumped = egressed.model_dump_json()
    for token in _SCENARIO_TOKENS:
        assert token not in dumped


def test_route_label_masks_scenario_and_member_id() -> None:
    # A concrete, scenario-armed path egresses only as its structural pattern.
    labelled = route_label("/workspace/member/54321", _ROUTES)
    assert labelled == "/workspace/member/:member_number"
    assert "54321" not in labelled


def test_full_model_observation_is_clean() -> None:
    obs = ModelObservation(
        route=route_label("/workspace/member/54321", _ROUTES),
        candidates=[
            _minimize(
                Candidate(id="c1", role="link", name="Member Inquiry", frame=None)
            ),
            _minimize(
                Candidate(id="c2", role="button", name="Create Account", frame="lc-workspace")
            ),
        ],
        actions_taken=["click link:Open Sub-Account"],
        obtained_outputs=[],
        steps_remaining=6,
    )
    dumped = json.dumps(json.loads(obs.model_dump_json()))
    for token in _SCENARIO_TOKENS:
        assert token not in dumped
    assert "54321" not in dumped
