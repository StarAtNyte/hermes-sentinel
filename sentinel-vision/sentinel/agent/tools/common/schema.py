"""The JSON-schema builder and the property shapes several tools share."""

from __future__ import annotations

from typing import Any

__all__ = ["ROI_PROPERTY", "SCHEDULE_PROPERTY", "SOURCE_PROPERTY", "schema"]


def schema(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """A Hermes tool schema with strict (``additionalProperties: false``) parameters."""
    params: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


ROI_PROPERTY = {
    "type": "array",
    "items": {"type": "number", "minimum": 0, "maximum": 1},
    "minItems": 4,
    "maxItems": 4,
    "description": "Region of interest as fractions of the frame: [x, y, width, height], "
    "e.g. [0.5, 0.5, 0.5, 0.5] is the bottom-right quarter. Only changes inside it count.",
}
SOURCE_PROPERTY = {
    "type": "string",
    "description": "Where to look: 'camera:0' (webcam index), 'screen' or 'screen:1' "
    "(monitor), 'screen:1@x,y,w,h' (screen region), 'stream:<name>' (a network camera "
    "the user configured; raw URLs are refused), 'file:/path/clip.mp4' or 'folder:/path' "
    "(newest image). Defaults to the configured default_source. Use sentinel_sources to list "
    "options.",
}
SCHEDULE_PROPERTY = {
    "type": "object",
    "description": "Optional active hours (local time); outside them the rule is idle.",
    "properties": {
        "start": {"type": "string", "description": "HH:MM, e.g. 22:00"},
        "end": {"type": "string", "description": "HH:MM, e.g. 06:00 (may cross midnight)"},
        "days": {
            "type": "array",
            "items": {"type": ["integer", "string"]},
            "description": "Weekdays: 0=Mon..6=Sun or names like 'mon'.",
        },
    },
    "additionalProperties": False,
}
