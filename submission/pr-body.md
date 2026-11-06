## Add hermes-sentinel

Plain-English visual watching for Hermes. You tell Hermes what to watch ("tell me if someone takes my coffee mug", "tell me when the loss curve flattens"); Sentinel watches a webcam, the screen, an RTSP/HTTP stream, a snapshot URL or an image folder and alerts you in your chat, with a photo, when it happens. It keeps an event log you can ask about later ("what happened at my desk today?").

- Repo: https://github.com/StarAtNyte/hermes-sentinel (plugin in `hermes-sentinel/`, `subdir` set)
- Pinned: `<FILL_40_CHAR_SHA>`, version `0.1.0`, `requires_hermes: ">=0.22.5"` (same floor in `plugin.yaml` and the entry)
- Submitted by the repository owner (rule 5).

### How it works

A local change gate (downscale, grayscale, blur, brightness-normalise, running background, ROI) runs on every frame at no model cost and rejects noise, lighting changes, shadows and camera shake. Only changed frames, or a scheduled tick for state rules, go to a vision model. Verdicts are confirmed k-of-n (default 2 of 3) above a confidence threshold, then pass hold time and cooldown. Hard caps apply to vision calls (60/hour and 500 per rolling 24 h by default, persisted). A confirmed event is stored (SQLite plus JPEG) and injected into the chat that created the rule. "False alarm" replies become negative examples and raise that rule's threshold by 0.05.

Rule modes: `trigger`, `condition` (true for N seconds), `absence` (expected thing missing for N seconds). Six bundled recipes: package-at-door, pet-on-couch, print-failure, training-loss-plateau, desk-intruder, stove-unattended.

### Hermes surfaces used

| Surface | Use |
|---|---|
| `ctx.register_tool` (8 tools) | `sentinel_sources`, `sentinel_watch`, `sentinel_rules`, `sentinel_update`, `sentinel_look`, `sentinel_events`, `sentinel_feedback`, `sentinel_teach` |
| `ctx.register_command` | `/sentinel status\|list\|pause\|resume\|test\|budget\|here\|help` |
| `ctx.register_auxiliary_task` | `sentinel_vision` (frame judge, vision model) and `sentinel_rules` (text-only rule compiler), routable independently |
| `ctx.llm` | Structured vision calls for the judge and rule compiling; no provider keys are read by the plugin |
| `ctx.inject_message` | Alert delivery into the originating chat (photo via `MEDIA:`); needs `allow_gateway_injection` outside the classic CLI |
| `ctx.on_unload` | Stops watcher threads and closes the store |
| `plugin_db` / plugin data dir | SQLite store and snapshots under `plugin-data/hermes-sentinel/` |
| Dashboard / Desktop pane | `dashboard/manifest.json` + `dashboard/plugin_api.py` (FastAPI router at `/api/plugins/hermes-sentinel/`) and `desktop/plugin.js` (Desktop Plugin SDK + React only): live source tiles, rules with pause/resume/delete, event timeline with correct / false-alarm buttons. It polls over `ctx.rest`; it reuses the same runtime, store and engine objects as the tools |

No hooks, no middleware, no `requires_env`. `capabilities.provides_tools` in the entry lists the eight tools above and matches `plugin.yaml` and `register(ctx)` (asserted by `tests/test_register.py`).

### Disclosures (rule 13)

- **Network calls.** (a) Vision and rule-compiling requests, containing camera/screen frames and rule text, go to the model provider the user configured through Hermes for the `sentinel_vision` / `sentinel_rules` auxiliary tasks. Frames are JPEG, at most 1024 px, optionally with an ROI crop and up to 2 positive and 2 negative example images. (b) Requests to stream/snapshot URLs the user supplies (`rtsp://`, `stream:`, `snapshot:`, `stream_urls`); snapshot redirects are followed and the `Authorization` header is dropped on a host change. No other hosts.
- **Camera and screen capture.** Opens local cameras (probes indexes 0-4 when listing sources), captures the screen with `mss`, reads image files from `folder:` sources and from `image_path` passed to `sentinel_teach`. Only what the user points it at.
- **Long-running background processes.** A watcher with daemon threads (one capture thread per source, two judge workers, a scheduler, a prune thread, short probe threads, a standby thread). It starts when a rule exists, or about 25 s after load if enabled rules already exist, and stops on unload. `engine.lock` in the plugin data dir keeps two Hermes processes on one profile from opening the same camera. The Desktop pane's `plugin_api.py` is a read/command layer over that watcher and starts no extra process.
- **Alert content and message injection.** Photos and captions are injected into the chat session, so they reach the agent's main model and, via `MEDIA:`, the messaging platform. Captions are length-capped, stripped of `MEDIA:` tokens, slash-leading lines and forged markers, and placed in a quoted untrusted-observation block with an instruction not to follow anything inside it. Outside the classic CLI, injection works only when the user sets `plugins.entries.hermes-sentinel.allow_gateway_injection: true`; the plugin never sets it. If delivery is refused the event is still stored and `/sentinel status` prints the exact snippet.
- **Local storage.** Rules, events, captions, snapshots, teaching examples, usage counters and chat routes (platform, chat id, session key) under `$HERMES_HOME/plugin-data/hermes-sentinel/`. Photos can show people and private rooms. Deleted after `retention_days` (7); oldest photos pruned above `max_storage_mb` (500).
- **Dashboard API.** Responses carry no stream URLs with credentials (masked) and no secrets. The latest-frame endpoint serves JPEG of at most 640 px wide with `no-store`.
- **Credentials.** None stored. `${NAME}` placeholders in configured stream URLs are resolved with `agent.secret_scope.get_secret` at connect time; literal passwords in URLs are rejected and URLs are redacted in errors. No `.env`, `auth.json` or token files are read.
- **Shell commands.** None. No writes outside the plugin data directory.
- **Telemetry and updates.** None. No update checks and no downloads of code.
- **Dependencies.** `numpy>=1.26,<3`, `opencv-python-headless>=4.8,<6`, `mss>=9,<11` (floors and upper bounds). OpenCV ships its own wheels; the repo contains no binaries or bytecode.

### Test evidence

- `scripts/check.sh` on the pinned commit: ruff clean, 1066 plugin tests passed, tool tests passed, `rule_lint` clean (rules 3, 7, 8, 9, 11, 17 statically).
- Tests use a fake plugin context and a scripted fake LLM, never import Hermes, and use synthetic scenes generated at test time (nothing committed as media). They cover the gate, the k-of-n state machine with an injected clock, budgets, the engine, alerts and injection hardening, tools, the slash command and an end-to-end flow. `test_register.py` asserts declared tools equal registered tools.
- `hermes plugins validate hermes-sentinel --install-deps`: `<FILL: paste result and any warnings, with explanation>`.
- `hermes plugins doctor hermes-sentinel --ci`: `<FILL: paste result>`.
- Live run on Hermes `<FILL: version>` (CLI, Telegram, Desktop): `<FILL: what was tested, which vision model, date>`.

### Screenshots

- Telegram alert with photo and caption: `<FILL>`
- Desktop pane (source tiles, rules, event timeline): `<FILL>`
- `/sentinel status`: `<FILL>`

(Also listed under `screenshots:` in the entry, pinned to the commit SHA.)

### Limitations (also in the README)

Single stills only (no motion, no audio); accuracy depends on the vision model; the change gate catches onsets, not slow drift; screen capture needs a display; not a safety device.

### Checklist against the admission rules

- [ ] 1. Human-merged: this PR, one file `plugin-catalog/hermes-sentinel.yaml`.
- [ ] 2. Full 40-character `sha`, `version` quoted.
- [ ] 3. No self-update: no update checker, no downloader, no remote `plugin.js`.
- [ ] 4. First listing; later SHA bumps go in new PRs with `version` and screenshot URLs re-pinned.
- [ ] 5. Submitted by the repository owner.
- [ ] 6. `capabilities` lists the 8 tools, no hooks, middleware or env vars; matches `register(ctx)`.
- [ ] 7. Security scan: `<FILL: result>`; every `caution` read and answered above.
- [ ] 8. `desktop/plugin.js` imports only `@hermes/plugin-sdk` and `react`; no prototype patching, `eval`, dynamic import, script injection, `data-*` queries or `document.body` observers; no `window.hermesDesktop` calls.
- [ ] 9. Only `ctx.*` public APIs; no core attribute rebinding; `hermes plugins validate` "no core override" check: `<FILL: result and any undocumented-import warnings>`.
- [ ] 10. Dependencies have floors and upper bounds.
- [ ] 11. Secrets only via `agent.secret_scope.get_secret`; no credential files read.
- [ ] 12. Nothing waits on a human; alerts are fire-and-forget; no auto-approve, no YOLO children.
- [ ] 13. Disclosures above and in the README.
- [ ] 14. `requires_hermes: ">=0.22.5"` (SemVer, not newer than current release v0.22.5) in `plugin.yaml` and the entry; `version` `0.1.0` matches the code.
- [ ] 15. Not a skin or fork of a bundled plugin.
- [ ] 16. Checked the catalog for existing camera/vision-monitoring plugins: `<FILL: result>`.
- [ ] 17. Source only: no binaries, no `.pyc`/`.pyo`, no archives.
- [ ] 18. Not a password-manager login backend.
