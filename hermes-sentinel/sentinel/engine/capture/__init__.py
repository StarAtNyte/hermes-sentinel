"""Capture side of the engine: one thread per source, its health, and on-demand frames."""

from .frames import FrameGrabber
from .health import SourceHealth
from .workers import CaptureWorkers

__all__ = ["CaptureWorkers", "FrameGrabber", "SourceHealth"]
