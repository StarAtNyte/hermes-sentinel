"""Bundled one-line watch recipes.

A recipe is a ready-made, hand-tuned rule: a precise yes/no question, a mode, timing and a
suggested source. ``sentinel_watch(recipe="package-at-door")`` builds the rule directly, with
no LLM compile step, so recipes work offline and behave the same for everyone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .compiler.motion import looks_like_motion
from .constants import MODE_ABSENCE, MODE_CONDITION, MODE_TRIGGER
from .spec import RuleSpec

__all__ = ["RECIPES", "Recipe", "build_rule", "get_recipe", "recipe_names"]


@dataclass(frozen=True)
class Recipe:
    """A bundled rule template."""

    name: str
    title: str
    description: str
    question: str
    mode: str
    hold_seconds: float = 0.0
    cooldown_seconds: float = 600.0
    source_hint: str = "camera"
    confidence_threshold: float | None = None
    motion: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary of the recipe."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "question": self.question,
            "mode": self.mode,
            "hold_seconds": self.hold_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "source_hint": self.source_hint,
        }


RECIPES: dict[str, Recipe] = {
    r.name: r
    for r in (
        Recipe(
            name="package-at-door",
            title="Package at the door",
            description="Alert when a parcel or delivery box is left at the door.",
            question="Is there a package, parcel, envelope or delivery box lying on the ground "
            "or doorstep near the door?",
            mode=MODE_TRIGGER,
            cooldown_seconds=1800.0,
        ),
        Recipe(
            name="pet-on-couch",
            title="Pet on the couch",
            description="Alert when a dog or cat climbs onto the couch.",
            question="Is a dog or cat on the couch or sofa (not on the floor next to it)?",
            mode=MODE_TRIGGER,
            cooldown_seconds=900.0,
        ),
        Recipe(
            name="print-failure",
            title="3D print failure",
            description="Alert when a 3D print fails (spaghetti, detached part, nozzle blob).",
            question="Does the 3D print look failed: loose spaghetti-like strands of filament, "
            "a part knocked loose from the bed, or a large blob of plastic on the nozzle?",
            mode=MODE_CONDITION,
            hold_seconds=60.0,
            cooldown_seconds=1800.0,
            confidence_threshold=0.75,
        ),
        Recipe(
            name="training-loss-plateau",
            title="Training loss plateau",
            description="Watch a loss chart on screen; alert when the curve stops improving.",
            question="Does the most recent part of the training loss curve on screen look flat "
            "(plateaued), with no clear downward trend over its last stretch?",
            mode=MODE_CONDITION,
            hold_seconds=900.0,
            cooldown_seconds=3600.0,
            source_hint="screen",
        ),
        Recipe(
            name="desk-intruder",
            title="Someone at my desk",
            description="Alert when a person sits at or reaches into your desk.",
            question="Is a person sitting at the desk, standing at it, or reaching onto it?",
            mode=MODE_TRIGGER,
            cooldown_seconds=600.0,
        ),
        Recipe(
            name="stove-unattended",
            title="Stove left unattended",
            description="Alert when a burner is on and nobody has been in the kitchen for 5 min.",
            question="Is a stove burner on (visible flame, glowing element or a lit indicator) "
            "with no person visible in the kitchen?",
            mode=MODE_CONDITION,
            hold_seconds=300.0,
            cooldown_seconds=1800.0,
        ),
    )
}

assert all(r.mode in (MODE_TRIGGER, MODE_CONDITION, MODE_ABSENCE) for r in RECIPES.values())


def recipe_names() -> list[str]:
    """Sorted names of the bundled recipes."""
    return sorted(RECIPES)


def get_recipe(name: Any) -> Recipe | None:
    """Look a recipe up by name (case, spaces and underscores are forgiven)."""
    if not isinstance(name, str):
        return None
    key = "-".join(name.strip().lower().replace("_", " ").split())
    return RECIPES.get(key)


def build_rule(
    recipe: Recipe,
    source: str,
    *,
    text: str = "",
    defaults: Mapping[str, Any] | None = None,
    origin: Mapping[str, Any] | None = None,
    roi: Sequence[float] | None = None,
    action_prompt: str = "",
) -> RuleSpec:
    """A validated rule from ``recipe``. Raises ``RuleValidationError`` for bad overrides."""
    defaults = dict(defaults or {})
    kwargs: dict[str, Any] = {
        k: defaults[k]
        for k in ("confirm_k", "confirm_n", "confidence_threshold")
        if defaults.get(k) is not None
    }
    if recipe.confidence_threshold is not None:
        kwargs["confidence_threshold"] = max(
            float(kwargs.get("confidence_threshold", 0.0)), recipe.confidence_threshold
        )
    prompt = (action_prompt or "").strip()
    spec = RuleSpec(
        question=recipe.question,
        source=source,
        text=(text or "").strip() or recipe.description,
        mode=recipe.mode,
        hold_seconds=recipe.hold_seconds,
        cooldown_seconds=recipe.cooldown_seconds,
        roi=tuple(roi) if roi is not None else None,  # type: ignore[arg-type]
        action="agent" if prompt else "notify",
        action_prompt=prompt,
        recipe=recipe.name,
        motion=recipe.motion or looks_like_motion(recipe.question),
        compiled_by="manual",
        origin=dict(origin or {}),
        **kwargs,
    )
    return spec.validate()
