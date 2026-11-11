# Configuration

Set under `plugins.entries.sentinel-vision.settings.<key>` in `config.yaml`. Invalid values fall back to the default and are listed by `/sentinel status`.

| Key | Default | Meaning |
|---|---|---|
| `default_source` | `camera:0` | Source used when a request names none. |
| `vision_calls_per_hour` | `60` | Cap on vision checks per clock hour, across all rules. `0` pauses judging; `null`, `-1`, `unlimited`, `none` or `off` removes the cap. |
| `vision_calls_per_day` | `500` | Cap per rolling 24 hours, across all rules. |
| `confirm_k` | `2` | Positive verdicts needed out of the last `confirm_n`. Must not exceed `confirm_n`. |
| `confirm_n` | `3` | Size of the confirmation window (1-20). |
| `confidence_threshold` | `0.7` | Minimum judge confidence (0-1) for a verdict to count. |
| `idle_check_seconds` | `300` | Re-check condition and absence rules this often without motion (trigger rules stretch it to at least 6 hours). Range 0 to 7 days; `0` disables the gate tick. |
| `retention_days` | `7` | Delete events and snapshots older than this. Minimum 0.01. |
| `max_storage_mb` | `500` | Snapshot storage cap; oldest photos are pruned first. Minimum 1. |
| `stream_urls` | `{}` | Named streams, name to URL, with `${SECRET}` placeholders. At most 64 entries, names up to 64 characters, URLs up to 2048 characters. |
| `alert_snapshots` | `true` | Attach the event photo (`MEDIA:`) to alerts. |
| `motion_frames` | `3` | Frames (2-3) shown, in time order, to the model for rules about movement. `0` or `1` sends a single still. |
| `batch_judging` | `true` | Judge rules that fire on the same camera frame in one vision call. |
| `batch_max_rules` | `4` | Most rules per batched call (1-8). `1` turns batching off. |
| `second_opinion` | `true` | Re-check a borderline positive verdict on a higher-resolution crop with a differently worded prompt. |
| `second_opinion_band_low` / `second_opinion_band_high` | `0.5` / `0.8` | Confidence band that earns a second opinion. Verdicts at or above the high edge are trusted. |
| `second_opinion_per_hour` | `6` | Cap on second opinions per rolling hour, across all rules (`0` turns them off). |
| `rule_min_interval_seconds` | `10` | Safety net: least seconds between two judge calls of one rule (0-3600; `0` turns it off). The extra looks of a rule collecting its k-of-n confirmations are exempt. |
| `latched_min_interval_seconds` | `60` | Safety net: least seconds between two judge calls of a latched rule (10-3600): one that fired, whose cooldown is over, and that waits for a clear verdict to re-arm. Those looks only detect that the scene cleared, so they are spaced wider than `rule_min_interval_seconds`. |
| `rule_calls_per_hour` | `30` | Safety net: most judge calls one rule may make per rolling hour (1-1000), on top of the global budget. At the cap you get one notice, the rule keeps its state and judging resumes as the hour frees. |

New rules copy `confirm_k`, `confirm_n` and `confidence_threshold` at creation; changing the settings later does not alter existing rules.

Back to the [README](../README.md).
