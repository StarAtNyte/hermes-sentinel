# Demo assets

- `sentinel_demo_mug_replay.mp4` (64 s, no audio): a replay assembled from the real test run of 2026-11-07. A generated desk clip stands in for a webcam; the event text, confidence, snapshot and the two Hermes Desktop screenshots are real. Every slide says it is a replay. No people appear in it.
- `status_page.html`: a local page that flips from "All systems operational" to "Major outage" when you click the button. Open it full screen and tell Hermes to watch your screen for an outage to record a live, face-free demo with the `screen` source.

These files live outside `hermes-sentinel/` on purpose: the plugin folder is what the catalog pins and Hermes scans, and it carries no media.
