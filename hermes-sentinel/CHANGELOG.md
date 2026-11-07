# Changelog

All notable changes to hermes-sentinel. Versions follow SemVer.

## Unreleased

### Added

- Per-rule judge pacing as a safety net under the cooldown: settings `rule_min_interval_seconds` (10 s between judge calls; confirmation looks exempt) and `rule_calls_per_hour` (30 per rolling hour). At the cap the rule keeps its state, you get one notice, and judging resumes as the hour frees. Shown in `/sentinel status` and as `judge_cap_hour` in the dashboard rule payload.
- `/sentinel setup` and the `sentinel_setup_check` tool (9 tools now): one command that checks Python packages, the `allow_gateway_injection` setting, the vision model, cameras and monitors, `default_source`, watcher state, storage and free disk, the alert route, budget caps and rules with offline sources, then prints `Ready: yes - N items to review` or `Ready: no - N things to fix` with the exact fix for each problem.
- Dashboard `GET /setup` and a compact Setup checklist card on the Desktop pane (shown while any check is not ok).
- `sentinel_watch` points at `/sentinel setup` when a fail-level problem exists.
- Desktop pane (`desktop/plugin.js`, backend `dashboard/plugin_api.py`): source tiles, rules with pause/resume/delete, Watch form, event timeline with feedback buttons, status chip.
- `encoding=data_url` on the frame and snapshot endpoints (JSON `{ok, data_url}`); raw JPEG without the parameter.

- Latched backoff: setting `latched_min_interval_seconds` (default 60 s, 10..3600). A rule that has fired and finished its cooldown but is still latched in `firing` is judged at most this often, because those looks only detect that the scene cleared. The normal 10 s gap, the confirmation looks and the hourly cap are unchanged. Shown in `/sentinel status`. A live replay with a looping, continuously moving video made 24 judge calls in 4.5 minutes for one alert before this change.

### Fixed

- After a Hermes restart a rule's stored chat session is no longer live, so the host refused the injection and the alert was lost (the event was stored, the user never saw it). `/sentinel resume <id>` now rebinds the rule to the calling session. An alert no route accepted is held per rule (`held_alerts.json` next to the store), shown by `/sentinel status` and `/sentinel list` as "alert held for rule <id>: run /sentinel resume <id> here to receive it", and cleared when it is delivered or the rule is resumed. Found in live run 5.
- A rule that had fired kept being judged during its cooldown while the scene kept changing (a person moving in front of a webcam): the cooldown check only looked at the `cooldown` state, but a fired rule sits in `firing` until the scene clears. Judge calls (gate and worker) are now skipped for the whole cooldown. Found in the live webcam test: about 6 calls per minute against a 60 per hour budget.
- The pane found no Sentinel when the plugin was loaded under a profile-scoped module name; it now scans `sys.modules` and states which process must host it.
- Source masking covers `snapshot:`/`folder:` prefixes, token-only userinfo and secret RTSP path segments.
- The Watch form no longer sends masked source keys; Source is a free-text field. Onboarding no longer claims sources are auto-detected.
- Removed the unused `/events` websocket from the pane (polling only); long rule status text wraps; a transient image error no longer latches the pane into direct-image mode.

## 0.1.0 - initial release

### Added

- Plain-English watch rules with three modes (`trigger`, `condition`, `absence`), hold time, cooldown, region of interest and active-hours schedule.
- Sources: webcam, screen (monitor or region), RTSP and HTTP streams, snapshot URLs, image folders, and named `stream_urls` with `${SECRET}` placeholders resolved through the Hermes secret scope.
- Local change gate (noise, lighting, shadow and camera-shake tolerant) in front of the vision model.
- Vision judge on the auxiliary task `sentinel_vision`; rule compiler on `sentinel_rules` with a deterministic fallback.
- k-of-n confirmation, confidence threshold, per-rule cooldown, alert and notice rate limits.
- Persisted vision-call budgets (60 per hour, 500 per rolling day by default).
- Event memory in SQLite with JPEG snapshots, full-text search, retention (7 days) and a storage cap (500 MB).
- Feedback and teaching: false alarms and corrections become example images and tune the rule.
- Six recipes: `package-at-door`, `pet-on-couch`, `print-failure`, `training-loss-plateau`, `desk-intruder`, `stove-unattended`.
- Eight agent tools (`sentinel_sources`, `sentinel_watch`, `sentinel_rules`, `sentinel_update`, `sentinel_look`, `sentinel_events`, `sentinel_feedback`, `sentinel_teach`) and the `/sentinel` command (`status`, `list`, `pause`, `resume`, `test`, `budget`, `here`).
- Alert delivery by `inject_message`, with chat isolation, sticky "delivery blocked" explanation and prompt-injection-resistant alert text.
- Single-owner engine via a lock file; a second Hermes process on the same profile stands by.
- Test harness: fake plugin context, fake LLM, Hermes module shims, synthetic scenes.
- `file:` video-file source (`.mp4 .mkv .mov .avi .webm .m4v`; `?loop=true`, `?speed=N`) for repeatable demos and tests. Without looping the watcher stops with one "finished" notice. Paths must be regular readable files and are never passed to OpenCV as URLs.
- Camera secrets: four optional whole-URL secret slots (`SENTINEL_STREAM_URL_1` to `_4`, `type: secret`), usable as `${SENTINEL_STREAM_URL_1}`; an in-memory secret cache so capture threads can reconnect on a multiplexing gateway; a "waiting for secret scope" status (retried every 5 s, no offline notice) and a "Camera secrets" row in `/sentinel setup`.
- Live-tested on Hermes v0.23.0 in an isolated home with a real vision model and a generated video file (alert injected, one judge call); the classic CLI did not render the injected turn live; Desktop and Telegram not yet tested ([docs/LIVE_TEST.md](docs/LIVE_TEST.md)).
- Slow-drift detection in the change gate: a second reference (the last judged frame) catches gradual, compact, local changes such as a rising level or a slowly opening door. Large lighting changes are filtered out; thresholds are `GateConfig` fields, not plugin settings.
- Motion input: rules about movement (keyword-detected, set with the `motion` argument of `sentinel_watch` and `sentinel_update` edit, or in the rule compiler's schema) are judged on up to 3 recent frames in time order, in one call, within 1.2 MB of images.
- Batched judging: rules that fire on the same captured frame share one vision call and one budget unit. Settings `batch_judging` (true) and `batch_max_rules` (4, ceiling 8). A malformed reply falls back to separate calls.
- Second opinion for borderline positive verdicts: a stricter, differently worded look at a crop of the changed region (up to 1536 px). Settings `second_opinion` (true), `second_opinion_band_low` / `_high` (0.5 / 0.8), `second_opinion_per_hour` (6). Costs one budget unit.
- Setting `motion_frames` (3).
- Inverse calibration: a correct confirmation lowers the rule's confidence threshold by 0.01 (never below 0.6) in addition to the existing +0.05 on a false alarm.
- `/sentinel setup --test-delivery` and `sentinel_setup_check` with `test_delivery=true`: one opt-in test message that reports whether Hermes accepted injection, with the exact `allow_gateway_injection` YAML when blocked.
- `/sentinel here` without an id binds this session's rules, else the only unbound rule, else lists the candidates with the exact commands. The chat route is learned from any `/sentinel` subcommand, which also binds that session's route-less rules; `/sentinel list` and `sentinel_rules` show hints for rules created in an earlier session of the chat.

### Not included

- Audio, and video-site streams (for example YouTube): sites' terms and the catalog's ban on downloaded binaries rule out the extraction route.
- A way to check `allow_gateway_injection` ahead of time: Hermes does not expose it to plugins, so only the opt-in delivery test exists.
