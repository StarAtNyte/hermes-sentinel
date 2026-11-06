"""The structured-output schema and instructions for the rule-compiling LLM call."""

from __future__ import annotations

from typing import Any

from ..constants import ACTIONS, MODES

COMPILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "mode": {"type": "string", "enum": list(MODES)},
        "hold_seconds": {"type": ["number", "null"]},
        "cooldown_seconds": {"type": ["number", "null"]},
        "roi": {
            "anyOf": [
                {"type": "null"},
                {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            ]
        },
        "schedule": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "days": {"type": ["array", "null"]},
                    },
                },
            ]
        },
        "action": {"type": "string", "enum": list(ACTIONS)},
        "action_prompt": {"type": ["string", "null"]},
    },
    "required": ["question", "mode"],
}

COMPILE_INSTRUCTIONS = """\
You convert a user's plain-English request to watch a camera or screen into a JSON rule.
A vision model will later be shown single frames and asked `question`, so:
- `question` is ONE short yes/no question answerable from a single still image,
  e.g. "Is there a package on the doorstep?". No time words in the question.
- `mode`:
  * "trigger": alert when the answer becomes yes ("tell me if someone takes my mug" ->
    question "Is the coffee mug missing from the desk?", mode trigger).
  * "condition": alert when the answer stays yes for hold_seconds ("stove on and nobody in
    the kitchen for 5 minutes" -> hold_seconds 300).
  * "absence": something EXPECTED is missing for hold_seconds. Phrase the question
    POSITIVELY about the expected thing ("dog not on its bed for 10 min" -> question
    "Is the dog on its bed?", mode absence, hold_seconds 600).
- `hold_seconds`: required duration for condition/absence, else 0.
- `cooldown_seconds`: minimum gap between alerts if the user states one, else null.
- `roi`: [x, y, w, h] as fractions 0..1 only if the user names a clear region of the frame
  ("top-left corner", "left half"), else null.
- `schedule`: {"start": "HH:MM", "end": "HH:MM", "days": [0..6 with 0=Monday] or null} only
  if the user limits when to watch ("at night" -> 22:00-06:00), else null.
- `action`: "agent" only if the user wants something DONE beyond being told (then put the
  instruction in `action_prompt`), else "notify" with action_prompt null.
Return only the JSON object."""
