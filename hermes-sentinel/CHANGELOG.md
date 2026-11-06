# Changelog

All notable changes to hermes-sentinel. Versions follow SemVer.

## 0.1.0 - initial release

Note: `plugin.yaml` and `__init__.py` still say `0.0.1`; bump both to match before pinning the catalog entry.

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

- Audio, video-site streams (for example YouTube), Desktop pane.
