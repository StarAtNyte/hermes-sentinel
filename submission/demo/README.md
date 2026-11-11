# Demo assets

- `sentinel_demo_mug_replay.mp4` (64 s, no audio): a replay assembled from the real test run of 2026-11-07. A generated desk clip stands in for a webcam; the event text, confidence, snapshot and the two Hermes Desktop screenshots are real. Every slide says it is a replay. No people appear in it.
- `status_page.html`: a local page that flips from "All systems operational" to "Major outage" when you click the button. Open it full screen and tell Hermes to watch your screen for an outage to record a live, face-free demo with the `screen` source.

These files live outside `hermes-sentinel/` on purpose: the plugin folder is what the catalog pins and Hermes scans, and it carries no media.

## Screen-watch demo (2026-11-11)

- `sentinel-screen-demo.mp4`: 35-second edited demo of a simulated service outage watched by the real Sentinel plugin. The page footage and event snapshot are real; the labeled result panel reconstructs the recorded event and `/sentinel status` output. The classic CLI accepted the alert injection but did not visibly render the alert, so this is not proof of an on-screen chat notification.
- `screen-demo-assets/raw-screen-recording.mp4`: unedited isolated-display capture, including the simulated change.
- `screen-demo-assets/event-snapshot.jpg` and `run-evidence.txt`: the saved event image and a concise record of the real run.

The watch used five vision checks, detected one event and reported one delivered alert. The demo intentionally contains no camera footage, face, narration or music.

## Final demo video

- `sentinel-demo-final.mp4`: 78-second cut for X. It combines a real Hermes Desktop run watching a CI page (a test is broken, the alert arrives with a screenshot) with a real webcam run (a purse is taken). Camera footage has the background blurred and personal areas hidden. Waiting time is skipped and labelled in the video.
- `sentinel-product-demo.mp4`: an earlier 82-second camera-only cut. `sentinel-hand-demo-backup.mp4`: a backup made from blurred stills of a 2026-11-07 run.
