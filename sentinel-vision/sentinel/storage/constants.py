"""Names, limits and intervals shared by the storage modules."""

from __future__ import annotations

import re

PLUGIN_NAME = "sentinel-vision"
DB_FILENAME = "sentinel.db"
SNAPSHOT_DIR = "snapshots"
EXAMPLE_DIR = "examples"

FEEDBACK_VALUES = ("correct", "false_alarm")
EXAMPLE_KINDS = ("pos", "neg")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CAPTION_LEN = 2000
MAX_REASON_LEN = 2000
MAX_QUERY_LIMIT = 500
DEFAULT_MAX_EXAMPLES_PER_KIND = 10
USAGE_KEEP_DAYS = 35
PRUNE_INTERVAL_SECONDS = 3600.0
JPEG_MAGIC = b"\xff\xd8\xff"
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
