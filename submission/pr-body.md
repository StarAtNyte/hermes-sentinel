## Add hermes-sentinel

Plain-English visual watching for Hermes. You tell Hermes what to watch ("tell me if someone takes my coffee mug", "tell me when the loss curve flattens"); Sentinel watches a webcam, the screen, an RTSP/HTTP stream, a snapshot URL, an image folder or a local video file and alerts you in your chat, with a photo, when it happens. It keeps an event log you can ask about later ("what happened at my desk today?").

- Repo: https://github.com/StarAtNyte/hermes-sentinel (plugin in `hermes-sentinel/`, `subdir` set)
- Pinned: `<FILL_40_CHAR_SHA>`, version `0.1.0`, `requires_hermes: ">=0.22.5"` (same floor in `plugin.yaml` and the entry)
- Submitted by the repository owner (rule 5).

### How it works

A local change gate (downscale, grayscale, blur, brightness-normalise, running background, ROI) runs on every frame at no model cost and rejects noise, lighting changes, shadows and camera shake. Only changed frames, or a scheduled tick for state rules, go to a vision model. Verdicts are confirmed k-of-n (default 2 of 3) above a confidence threshold, then pass hold time and cooldown. Hard caps apply to vision calls (60/hour and 500 per rolling 24 h by default, persisted). A confirmed event is stored (SQLite plus JPEG) and injected into the chat that created the rule. "False alarm" replies become negative examples and raise that rule's threshold by 0.05; a correct confirmation lowers it by 0.01 (not below 0.6).

Beyond the basic loop:

- **Slow drift.** The gate also compares against the last judged frame, so gradual compact changes (a rising level, a slowly opening door) trigger a look. Not configurable; very small or very large changes are ignored.
- **Motion.** Rules whose question is about movement (keyword-detected or `motion=true`) are judged on up to 3 recent frames in time order, in one call.
- **Batching.** Rules that fire on the same frame share one call (up to 4 by default).
- **Second opinion.** A borderline positive verdict (confidence 0.5 to 0.8) gets one stricter look at a higher-resolution crop; it costs one budget unit and is capped at 6 per hour.
- **Video files.** `file:/path/clip.mp4` (optionally `?loop=true`) for repeatable demos and tests.
- **Secrets.** Four whole-URL secret slots (`SENTINEL_STREAM_URL_1..4`, declared as `type: secret` in `config_schema`) plus `${SECRET}` placeholders, all read through `agent.secret_scope.get_secret`.
- **Delivery self-test.** `/sentinel setup --test-delivery` sends one opt-in message and reports whether injection is allowed (a plugin cannot read the setting).

Rule modes: `trigger`, `condition` (true for N seconds), `absence` (expected thing missing for N seconds). Six bundled recipes: package-at-door, pet-on-couch, print-failure, training-loss-plateau, desk-intruder, stove-unattended.

### Hermes surfaces used

| Surface | Use |
|---|---|
| `ctx.register_tool` (9 tools) | `sentinel_sources`, `sentinel_watch`, `sentinel_rules`, `sentinel_update`, `sentinel_look`, `sentinel_events`, `sentinel_feedback`, `sentinel_teach`, `sentinel_setup_check` |
| `ctx.register_command` | `/sentinel setup [--test-delivery]\|status\|list\|pause\|resume\|test\|budget\|here\|help` |
| `ctx.register_auxiliary_task` | `sentinel_vision` (frame judge, vision model) and `sentinel_rules` (text-only rule compiler), routable independently |
| `ctx.llm` | Structured vision calls for the judge (single still, motion sequence, batch, second opinion) and rule compiling; no provider keys are read by the plugin |
| `ctx.inject_message` | Alert delivery into the originating chat (photo via `MEDIA:`); needs `allow_gateway_injection` outside the classic CLI |
| `ctx.on_unload` | Stops watcher threads and closes the store |
| `plugin_db` / plugin data dir | SQLite store and snapshots under `plugin-data/hermes-sentinel/` |
| Dashboard / Desktop pane | `dashboard/manifest.json` + `dashboard/plugin_api.py` (FastAPI router at `/api/plugins/hermes-sentinel/`) and `desktop/plugin.js` (Desktop Plugin SDK + React only): live source tiles (latest frame, about 1 fps), rules with pause/resume/delete, a Watch form (free-text source, optional recipe), event timeline with snapshots and correct / false-alarm buttons, a status-bar chip and a budget/delivery banner. It polls over `ctx.rest` (no websocket) and fetches images as `?encoding=data_url` JSON; it reuses the same runtime, store and engine objects as the tools. The pane is enabled by default (no `defaultEnabled: false`); it shows an empty state and 503 writes if the dashboard process that serves `plugin_api.py` is not the process that runs the agent, so it must be hosted by that process (see README) |

No hooks, no middleware, no `requires_env` in `plugin.yaml`, so the entry declares `requires_env: []`. The four optional `SENTINEL_STREAM_URL_*` secret slots are `config_schema` fields of `type: secret` (a separate declaration from `requires_env` in the catalog guide; they are optional, none is needed to load the plugin). `capabilities.provides_tools` in the entry lists the nine tools above and matches `plugin.yaml` and `register(ctx)` (asserted by `tests/test_register.py`).

### Disclosures (rule 13)

- **Network calls.** (a) Vision and rule-compiling requests, containing camera/screen frames and rule text, go to the model provider the user configured through Hermes for the `sentinel_vision` / `sentinel_rules` auxiliary tasks. Frames are JPEG, at most 1024 px, optionally with an ROI crop and up to 2 positive and 2 negative example images. Movement rules send up to 3 recent frames in one call (about 1.2 MB of images at most); a second opinion sends a crop of the changed region at up to 1536 px; a batched call sends one frame with several rules' questions. (b) Requests to stream/snapshot URLs the user supplies (`rtsp://`, `stream:`, `snapshot:`, `stream_urls`); snapshot redirects are followed and the `Authorization` header is dropped on a host change. No other hosts.
- **Camera and screen capture.** Opens local cameras (probes indexes 0-4 when listing sources), captures the screen with `mss`, reads image files from `folder:` sources, video files from `file:` sources (regular readable files with a video extension, resolved to absolute paths, never passed to OpenCV as URLs) and `image_path` passed to `sentinel_teach`. Only what the user points it at.
- **Long-running background processes.** A watcher with daemon threads (one capture thread per source, two judge workers, a scheduler, a prune thread, short probe threads, a standby thread). It starts when a rule exists, or about 25 s after load if enabled rules already exist, and stops on unload. `engine.lock` in the plugin data dir keeps two Hermes processes on one profile from opening the same camera. The Desktop pane's `plugin_api.py` is a read/command layer over that watcher and starts no extra process.
- **Alert content and message injection.** Photos and captions are injected into the chat session, so they reach the agent's main model and, via `MEDIA:`, the messaging platform. Captions are length-capped, stripped of `MEDIA:` tokens, slash-leading lines and forged markers, and placed in a quoted untrusted-observation block with an instruction not to follow anything inside it. Outside the classic CLI, injection works only when the user sets `plugins.entries.hermes-sentinel.allow_gateway_injection: true`; the plugin never sets it. If delivery is refused the event is still stored and `/sentinel status` prints the exact snippet.
- **Extra vision calls.** A second opinion on a borderline positive verdict is an additional vision call (default cap 6 per hour, counted in the 60/hour and 500/day budgets). A malformed batch reply is retried as separate calls. Settings `second_opinion`, `batch_judging`, `batch_max_rules` and `motion_frames` turn these down or off.
- **Local storage.** Rules, events, captions, snapshots, teaching examples, usage counters and chat routes (platform, chat id, session key) under `$HERMES_HOME/plugin-data/hermes-sentinel/`. Photos can show people and private rooms. Deleted after `retention_days` (7); oldest photos pruned above `max_storage_mb` (500).
- **Dashboard API.** Responses carry no stream URLs with credentials (masked) and no secrets. The latest-frame endpoint serves JPEG of at most 640 px wide with `no-store`.
- **Credentials.** None stored on disk. `${NAME}` placeholders in configured stream URLs and the four `SENTINEL_STREAM_URL_*` secret slots are resolved with `agent.secret_scope.get_secret` at connect time; an in-memory copy of the secrets that sources reference is kept while the plugin is loaded (so capture threads work on a multiplexing gateway) and cleared on unload; literal passwords in URLs are rejected and URLs are redacted in errors. No `.env`, `auth.json` or token files are read.
- **Shell commands.** None. No writes outside the plugin data directory.
- **Telemetry and updates.** None. No update checks and no downloads of code.
- **Dependencies.** `numpy>=1.26,<3`, `opencv-python-headless>=4.8,<6`, `mss>=9,<11` (floors and upper bounds). OpenCV ships its own wheels; the repo contains no binaries or bytecode.

### Test evidence

- `scripts/check.sh` on the pinned commit: ruff clean, 1422 plugin tests passed, 195 tool tests passed (including the Node check of `desktop/plugin.js`), `rule_lint` clean (rules 3, 8, 9, 11, 17 statically). `rule_lint` does not cover rule 7: that is the Hermes install security scan, reported below.
- Tests use a fake plugin context and a scripted fake LLM, never import Hermes, and use synthetic scenes generated at test time (nothing committed as media). They cover the gate (including labelled slow-drift scenes), motion input, batching and second opinions, video-file and secret sources, delivery self-test and chat binding, the k-of-n state machine with an injected clock, budgets, the engine, alerts and injection hardening, tools, the slash command and an end-to-end flow. `test_register.py` asserts declared tools equal registered tools.
- `hermes plugins validate hermes-sentinel --install-deps`: `<FILL: paste result and any warnings, with explanation>`.
- `hermes plugins doctor hermes-sentinel --ci`: `<FILL: paste result>`.
- Live test evidence (2026-11-07, Hermes v0.23.0, isolated `HERMES_HOME`, Nous Research provider, model `openai/gpt-6-luna-pro`, classic CLI): from the prompt "watch the video file ... and tell me if the white coffee mug on the desk is taken away" the agent found the tools by itself, created a rule, and when the mug left a generated test video the gate flagged the change, one judge call returned a match (confidence 0.98), event #1 was stored with a snapshot and an alert was injected into the chat; the whole run used 4 vision calls. `scripts/check.sh --with-hermes` passed on the same commit (security scan safe, doctor ok, 1430 plugin tests, 195 tool tests). Full write-up: `hermes-sentinel/docs/LIVE_TEST.md`.
- Not yet tested live: the Desktop pane, Telegram delivery with a photo, a real webcam, and tuning on real-world footage. In the classic CLI the injected alert turn was stored in the session but not rendered live in the terminal, and the photo went out as a file path; Desktop and messaging platforms render it.

### Screenshots

- Telegram alert with photo and caption: `<FILL>`
- Desktop pane (source tiles, rules, event timeline): `<FILL>`
- `/sentinel status`: `<FILL>`

(Also listed under `screenshots:` in the entry, pinned to the commit SHA.)

### Limitations (also in the README)

No audio and no video-site streams. Movement is judged from up to 3 recent frames, not video. The slow-drift check catches gradual compact changes but not very small or very large ones. Batching and second opinions trade extra calls for accuracy, and a disagreeing second opinion can cancel a real event. `allow_gateway_injection` cannot be read ahead of time (`/sentinel setup --test-delivery` tests it). On a multiplexing gateway, cameras that need a `${SECRET}` wait for the first `/sentinel` or `sentinel_*` call after start-up. Accuracy depends on the vision model; screen capture needs a display; not a safety device.

### Checklist against the admission rules

- [ ] 1. Human-merged: this PR, one file `plugin-catalog/hermes-sentinel.yaml`.
- [ ] 2. Full 40-character `sha`, `version` quoted.
- [ ] 3. No self-update: no update checker, no downloader, no remote `plugin.js`.
- [ ] 4. First listing; later SHA bumps go in new PRs with `version` and screenshot URLs re-pinned.
- [ ] 5. Submitted by the repository owner.
- [ ] 6. `capabilities` lists the 9 tools, no hooks, middleware or env vars; matches `register(ctx)`.
- [ ] 7. Hermes install security scan (`hermes plugins validate` / install): `<FILL: result>`; every `caution` read and answered above.
- [ ] 8. `desktop/plugin.js` imports only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime` (no JSX source, the runtime import is the one the docs allow); no prototype patching, `eval`, dynamic import, script injection, `data-*` queries or `document.body` observers; no `window.hermesDesktop` calls.
- [ ] 9. Only `ctx.*` public APIs; no core attribute rebinding; `hermes plugins validate` "no core override" check: `<FILL: result and any undocumented-import warnings>`.
- [ ] 10. Dependencies have floors and upper bounds.
- [ ] 11. Secrets only via `agent.secret_scope.get_secret`; no credential files read.
- [ ] 12. Nothing waits on a human; alerts are fire-and-forget; no auto-approve, no YOLO children.
- [ ] 13. Disclosures above and in the README.
- [ ] 14. `requires_hermes: ">=0.22.5"` (SemVer, not newer than current release v0.22.5) in `plugin.yaml` and the entry; `version` `0.1.0` matches the code.
- [ ] 15. Not a skin or fork of a bundled plugin.
- [ ] 16. Checked the catalog for existing camera/vision plugins; none covers the same job:
  - `hermes-desktop-camera`: a composer button that takes one photo and attaches it to the next message. Manual, one shot, no rules, watching, judging or alerts. Sentinel never adds attachments to the composer.
  - `hermes-monitor`: Desktop status-bar health for Kanban workers and provider quota. Not about cameras or images.
  - `covision`: read-only Covision meeting and task tools over MCP. Unrelated.
  - `hermes-vision-inline`: middleware that attaches images inline to the session model instead of the auxiliary vision backend. Sentinel adds no middleware and does not change how Hermes handles images; it runs its own continuous watcher, change gate, k-of-n judging, event store and alerts on its own `sentinel_vision` auxiliary task.
  - Re-run `ls plugin-catalog | grep -i -E "camera|vision|watch|monitor|sentinel"` on the day of the PR: `<FILL: result>`.
- [ ] 17. Source only: no binaries, no `.pyc`/`.pyo`, no archives.
- [ ] 18. Not a password-manager login backend.
