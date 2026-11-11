"""Engine-side judging: motion sequences, batching several rules into one call, second opinions.

The :class:`~sentinel.vision.judge.Judge` knows how to make each kind of model call; this
package decides *which* call to make and pays for it:

* :class:`JudgingPipeline` turns a group of prepared rule checks into verdicts: rules on the
  same frame share one batched call (a fallback to separate calls if the reply is unusable),
  every call reserves the vision budget first;
* :class:`SecondOpinion` re-checks a borderline positive verdict on a higher-resolution crop,
  under an hourly cap and the same budget;
* :func:`motion_sequence` picks the earlier frames a motion rule is shown.
"""

from .hourly_cap import HourlyCap
from .items import JudgeItem, Outcome
from .motion import motion_sequence
from .pipeline import JudgingPipeline
from .policy import JudgingPolicy
from .second_opinion import SecondOpinion, padded_crop

__all__ = [
    "HourlyCap",
    "JudgeItem",
    "JudgingPipeline",
    "JudgingPolicy",
    "Outcome",
    "SecondOpinion",
    "motion_sequence",
    "padded_crop",
]
