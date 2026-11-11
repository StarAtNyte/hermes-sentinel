"""Check that the Python packages the watcher needs are importable."""

from __future__ import annotations

from ..env import SetupEnv
from ..model import FAIL, OK, WARN, Check

__all__ = ["DEPS_HINT", "check_deps"]

DEPS_HINT = (
    "Install the plugin dependencies numpy>=1.26,<3, opencv-python-headless>=4.8,<6 and "
    "mss>=9,<11: enabling the plugin (hermes plugins enable sentinel-vision) installs them for you."
)


def check_deps(env: SetupEnv) -> Check:
    """OK when numpy, opencv and mss import; a missing mss alone only costs screen capture."""
    missing = [m for m in ("numpy", "cv2", "mss") if not env.module_available(m)]
    if not missing:
        return Check("deps", "Python packages", OK, "numpy, opencv and mss are installed.")
    names = {"cv2": "opencv-python-headless"}
    pretty = ", ".join(names.get(m, m) for m in missing)
    if set(missing) == {"mss"}:
        return Check(
            "deps",
            "Python packages",
            WARN,
            "mss is missing, so screen capture will not work (cameras are fine).",
            DEPS_HINT,
        )
    return Check(
        "deps",
        "Python packages",
        FAIL,
        f"Missing: {pretty}.",
        DEPS_HINT + "  (in the Python environment Hermes runs in)",
    )
