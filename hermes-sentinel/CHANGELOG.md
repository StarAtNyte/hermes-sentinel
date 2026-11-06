# Changelog

All notable changes to hermes-sentinel. Versions follow SemVer.

## Unreleased

### Added

- `/sentinel setup` and the `sentinel_setup_check` tool (9 tools now): one command that checks Python packages, the `allow_gateway_injection` setting, the vision model, cameras and monitors, `default_source`, watcher state, storage and free disk, the alert route, budget caps and rules with offline sources, then prints `Ready: yes/no - N things to fix` with the exact fix for each problem.
- Dashboard `GET /setup` and a compact Setup checklist card on the Desktop pane (shown while any check is not ok).
- `sentinel_watch` points at `/sentinel setup` when a fail-level problem exists.
- Desktop pane (`desktop/plugin.js`, backend `dashboard/plugin_api.py`): source tiles, rules with pause/resume/delete, Watch form, event timeline with feedback buttons, status chip.
- `encoding=data_url` on the frame and snapshot endpoints (JSON `{ok, data_url}`); raw JPEG without the parameter.

### Fixed

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

### Not included

- Audio, video-site streams (for example YouTube).
