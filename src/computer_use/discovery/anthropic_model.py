"""Anthropic-backed DiscoveryModel adapter.

This is ONLY a model adapter: it maps (goal, observation) -> ProposedAction and
holds no browser or kernel authority. Model output is parsed into the untrusted
ProposedAction boundary and validated before the agent hands it to the kernel.
The raw invocation value is never sent — the model reasons about symbolic refs.
"""

from __future__ import annotations

import json

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from pydantic import ValidationError

from computer_use.model import ProposedAction

from .model import GoalContext, ModelObservation, ModelOutputError

DEFAULT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """\
You operate a bank employee workstation to accomplish a goal. You do NOT control the
browser directly and you never see raw member data. Each turn you receive JSON with the
goal, the typed inputs available (by name only), the candidate controls (with ids), the
current route, and "actions_taken" (what you have ALREADY done this run).

Propose exactly ONE next action as ONLY a JSON object (no prose):
  {"action": "click|type|extract|declare_success|request_human",
   "candidate_id": "<id from candidates>",              // click/type/extract
   "value": {"source":"parameter","name":"<input>"},    // type only
   "output": "<output name>",                            // extract only
   "reason": "<why>"}                                    // request_human only

Rules:
- Do NOT repeat an action that already appears in actions_taken.
- To type an input, use its symbolic ref {"source":"parameter","name":...}; never a raw value.
- Choose candidate_id only from the provided candidates.
- If "last_error" is set, your previous reply was rejected — return ONLY the JSON object.

Typical flow for a member lookup:
  1. type the member number into the "Member Number" field,
  2. click "Search",
  3. on the member profile, extract the requested output from the accounts table by
     choosing the cell whose row matches the account and whose column is the field
     (e.g. row "Share Savings", column "Current Balance"),
  4. once the output has been extracted, reply {"action":"declare_success"}.
"""


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]  # drop the opening ``` / ```json line
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced {...} object in the text, ignoring surrounding prose."""
    cleaned = _strip_fences(text)
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]
    return None


def parse_proposal(text: str) -> ProposedAction:
    """Parse model text into the untrusted, validated ProposedAction boundary.

    Tolerant of surrounding prose/fences. Raises ModelOutputError (not a raw JSON or
    validation error) so the discovery agent can recover and re-prompt.
    """
    candidate = _extract_json_object(text)
    if candidate is None:
        raise ModelOutputError(f"no JSON object in model output: {text[:120]!r}")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ModelOutputError(f"invalid JSON: {error}") from error
    try:
        return ProposedAction.model_validate(data)
    except ValidationError as error:
        raise ModelOutputError(f"invalid action: {error}") from error


def _render_user(goal: GoalContext, observation: ModelObservation) -> str:
    return json.dumps(
        {"goal": goal.model_dump(), "observation": observation.model_dump()},
        separators=(",", ":"),
    )


class AnthropicDiscoveryModel:
    provider = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        self.model_id = model
        self._api_key = api_key
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def decide(self, goal: GoalContext, observation: ModelObservation) -> ProposedAction:
        messages: list[MessageParam] = [
            {"role": "user", "content": _render_user(goal, observation)}
        ]
        response = await self._get_client().messages.create(
            model=self.model_id,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=messages,
        )
        text = "".join(getattr(block, "text", "") for block in response.content)
        return parse_proposal(text)
