# Architecture

This describes the code as it is. [DESIGN.md](../../DESIGN.md) is the original plan; where they differ (Desktop pane and dashboard backend now exist, six-recipe set, `sentinel_rules` task, route book, process lock), this document is right.

## Data flow

```
capture thread (one per source) --frame--> ChangeGate (one per rule, ROI aware)
    --changed | drift | tick | confirm look--> bounded judge queue (drop oldest)
        --> judge worker (pool of 2) takes the jobs queued from one frame together
            --> budget -> Judge (ctx.llm): still | motion sequence | batch
            --> optional second opinion on a borderline hit --> RuleStateMachine
            --fired--> snapshot + event in Store --> Alerter --> ctx.inject_message
```

## Module map (`sentinel/`)

Every package meets the Code standard in `CLAUDE.md`. `scripts/structure_lint.py` and the strict ruff pass (`ruff.strict.toml`) both run clean and fail `scripts/check.sh` and CI otherwise: modules at most 400 lines, classes 250, functions 40, nesting 3, at most 8 modules per package directory, docstrings on public names, `__init__.py` files that only re-export.

| Path | Role |
|---|---|
| `__init__.py` (plugin root) | `register(ctx)`: aux tasks, tools, `/sentinel`, `on_unload`, delayed boot. |
| `runtime.py` | `Sentinel`: lazily built service graph (store, budget, engine, route book, alerter, judge) for one plugin context. Delayed boot timer (25 s), `shutdown()`. Stays at this path: the dashboard API finds it by module name. |
| `service/` | Collaborators of `Sentinel`: `engine_wiring.py` (`EngineWiring`, the injectable factories and clocks), `pruner.py` (`BackgroundPruner`), `registry.py` (the active-Sentinel pointer the dashboard reads), `aux_tasks.py` (`register_aux_tasks`). |
| `config.py` | `SentinelConfig.load`: reads `config_schema` keys via `ctx.get_config`, validates, records `problems` instead of raising. |
| `sources/` | Frame sources. A `file:` video source and the secret-slot logic are included. `base.py` (`FrameSource`), `registry.py` (`open_source`), `discovery.py` (`list_sources`, camera and monitor probes), `frames.py`, `timeouts.py` (bounded opens), `dependencies.py` (lazy OpenCV, numpy, mss), `errors.py`, `constants.py`. `spec/` holds `SourceSpec`, parsing, validation, URL redaction and secret placeholder resolution. `secrets.py` and `secret_status.py` hold the secret cache and the waiting-for-scope status. `backends/` holds `capture.py` (camera, stream), `snapshot.py`, `screen.py`, `folder.py`, `video_file.py` (local recordings; raises `SourceExhausted` at the end unless looping). |
| `rules/` | `spec.py` (`RuleSpec`, validated, JSON round-trip), `spec_validation.py`, `schedule.py`, `validation.py`, `errors.py`, `constants.py`, `recipes.py` (six bundled `Recipe`s, `build_rule`), `calibration.py` (threshold steps from feedback: +0.05 on a false alarm up to 0.95, -0.01 on a confirmation down to 0.6). `machine/` is the pure, clock-injected `RuleStateMachine` (`state_machine.py`, `window.py`, `evidence.py`, `types.py`). `compiler/` is `compile_rule` (LLM on `sentinel_rules`, deterministic `fallback.py`, `schema.py`, and `motion.py`, the keyword heuristic that sets `RuleSpec.motion`). |
| `storage/` | `store.py` (`Store` facade), `database.py` (connection, single RLock, `tx()`/`read()`), `schema.py` (migrations, FTS5), `files.py` (snapshot and example images), `retention.py` (retention and storage-cap pruning), `records.py`, `errors.py`, `constants.py`. `repositories/` holds `rules.py`, `events.py`, `event_search.py`, `examples.py`, `usage.py`. |
| `vision/` | `imaging.py`, `gate/` (`ChangeGate`: structural-difference change detection, noise estimation, shake alignment, global-change re-baseline, idle tick; `tracking/drift.py` compares against a slow reference, `tracking/recent_frames.py` keeps up to `motion_frames` read-only frames, only for motion rules; numpy required, OpenCV optional) and `judge/` (`Judge`: request building, `ctx.llm.complete_structured` on task `sentinel_vision`, defensive parsing; failures become `Verdict(error=...)`; `motion.py` encodes an ordered frame sequence within 1.2 MB, `batch.py` the multi-rule call, `verify.py` the second-opinion prompt and verdict merge, `blocks.py` shared request pieces). Import `ChangeGate` from `vision.gate` (numpy is only needed once frames flow); the package root re-exports the judge and imaging API. |
| `judging/` | What happens to a group of jobs from one frame: `policy.py` (`JudgingPolicy` from config), `items.py`, `pipeline.py`, `motion.py` (choose earlier frames at least 0.25 s before the current one), `second_opinion.py` (`SecondOpinion`, padded crop of the changed region), `hourly_cap.py` (rolling-hour cap, thread-safe). |
| `alerts/` | `delivery.py` (`Alerter`: delivery, rate limits, blocked-delivery state), `formatting.py` (untrusted-text handling), `rate_limit.py`, `route_book.py` (`RouteBook`), `routing.py` (origin capture and chat scoping), `self_test.py` (`send_test`, the opt-in delivery test), `rule_binding.py` (what `/sentinel here` binds), `chat_hints.py` (hints for rules whose chat route is unknown). |
| `engine/` | `Engine` is a thin facade over collaborators that share one `EngineState` (`state.py`, with the documented `lock`): `rulebook.py` (`RuleBook`: install, remove, sync), `lifecycle.py` (`Lifecycle`: start, standby, stop, shutdown flag), `scheduler.py`, `status.py`, `settings.py`, `models.py` (`RuleRuntime`, `Job`, `SourceWorker`). `capture/`: `workers.py` (`CaptureWorkers`), `health.py` (`SourceHealth`: backoff and notices), `frames.py` (`FrameGrabber`). `jobs/`: `gating.py` (`FrameGate`, `DefaultGateFactory`), `queue.py` (`JobQueue`), `timing.py`. `verdicts/`: `runner.py` (`JudgeRunner`), `applier.py`, `judge_health.py`, `recorder.py` (snapshot, event, alert), `reservation.py` (`BudgetReserver`), `rule_tester.py`. `infra/`: `process_lock.py`, `example_cache.py`, `errors.py`, `source_keys.py`. |
| `budget.py` | `Budget`: hourly and rolling-day caps over the store's `usage` table; one exhaustion notice per episode. |
| `version.py` | `__version__`, the one place besides `plugin.yaml` that carries the version. The plugin root `__init__.py` holds `register(ctx)` and nothing else. |
| `agent/` | The agent-facing layer. `tools/`: `registry.py` (`SentinelTools`, a thin dispatcher, and `register_tools`), `catalog.py` (the nine `ToolSpec`s in order, `SCHEMAS`, `TOOL_NAMES`, `EMOJI`), `dispatch.py` (`wrap_handler`: secret priming, error mapping). Each tool is one module with its schema next to its handler: `management/` (`watch.py`, `list_rules.py`, `update.py`, `feedback.py`, `teach.py`, `examples.py`) and `inspection/` (`sources.py`, `look.py`, `events.py`, `setup_check.py`). `common/` holds the shared pieces: `args.py`, `results.py` (`ok`, `err`, `ToolError`), `schema.py`, `context.py` (`ToolContext`: origin scoping, rule lookup, source argument, save-and-apply; `ToolSpec`), `briefs.py`, `timebounds.py`, `images.py`, `throttles.py`. `commands/`: `command.py` (`SentinelCommand`, the parser and dispatcher) with one module per subcommand group (`status.py` for status and list, `pause.py`, `here.py`, `setup.py`, `rule_probe.py` for `test`, `budget.py`) and `common.py` (usage text, caller helpers). `setup/`: `runner.py` (`run_checks`), `report.py`, `env.py` (`SetupEnv`), `model.py` (`Check`), `masking.py`, and one module per check under `install/` (deps, vision, storage, config), `devices/` (sources, default source, secrets) and `watching/` (injection, delivery, budget, engine, rules). The checks are shared by `/sentinel setup`, `sentinel_setup_check` and `GET /setup`. |
| `dashboard/plugin_api.py` | Thin entry point Hermes loads on its own: finds the loaded plugin package by a read-only scan of `sys.modules` (`hermes_plugins.hermes_sentinel[__home_*]`), imports its `sentinel.dashboard_api.router` and exports it as the module-level `router`, mounted at `/api/plugins/hermes-sentinel/`. If the plugin is not loaded in the process it exports a fallback router whose routes all answer 503. |
| `sentinel/dashboard_api/` | The dashboard backend, a regular part of the plugin package. `router.py` composes the area routers into one flat router; `routes/` has one router per area (`status`, `sources`, `setup`, `images`, `rules`, `events`); `common/` has `access.py` (finds the loaded `Sentinel` by scanning `sys.modules`, caches the owner `SentinelTools`), `http.py`, `masking.py` (source specs never leave unredacted), `shapes.py`, `source_listing.py`. Frames and snapshots are served as raw JPEG or `?encoding=data_url` JSON. |
| `desktop/plugin.js` | Desktop pane (SDK + React only): polling controller, source tiles, rules, Watch form, events, status chip. No websocket. |

Import from a package root (`from .storage import Store`, `from .rules import RuleSpec`, `from .sources import open_source`). There are no compatibility shims: `sentinel.tools`, `sentinel.commands` and `sentinel.setup_check` no longer exist, and `runtime.py` stays at its path because the dashboard API finds it by module name.

## Gate: fast path, slow drift, recent frames

The fast path compares each frame with a running background (EMA). A change that is slower than the background adapts would vanish, so the gate also keeps a slow reference: the last judged frame or tick frame, replaced after `drift_refresh_seconds` (600 s) without a judged frame. At most once per `drift_check_seconds` (1 s) the frame is scored against it with the same lighting- and shake-tolerant score at half the threshold. A drift fires only if enough of the ROI differs (at least 0.4%), not too much (over 15% is lighting and refreshes the reference silently), the changed pixels are compact (fill at least 0.25), the changed region stays put (IoU at least 0.2) and two consecutive checks agree. The result is `GateResult(changed=True, reason="drift", drift=True, bbox=...)`. These values are `GateConfig` fields, not plugin settings. `recent_frames()` returns up to 3 frames at least 0.5 s apart, subsampled to at most 640 px wide, read-only, under a 16 MB cap; `reset()` clears them.

## Judging: motion, batching, second opinion

- **Motion.** `RuleSpec.motion` is set by `rules/compiler/motion.py` (a keyword heuristic, also used for recipes), by the LLM compiler's optional `motion` field, or by the `motion` tool argument. A motion rule is judged on `gate.recent_frames()` plus the current frame (earlier frames at least 0.25 s older, up to `motion_frames` in total, 2 to 3), labelled by time offset, in one call. Images are shrunk step by step (896 down to 320 px) to stay within 1.2 MB, then the oldest are dropped. Without an earlier frame, or with `motion_frames` 0 or 1, it is a single still.
- **Batching.** One call answers several rules with `{verdicts: [{rule_id, match, confidence, reason, caption}]}`; a ROI is described in the prompt, not cropped. The host raises `ValueError` for a reply that does not match the schema (Hermes `agent/plugin_llm.py`), which is treated as "unusable" and falls back to separate calls.
- **Second opinion.** Runs only for a verdict that would count as a hit with confidence in `[second_opinion_band_low, second_opinion_band_high)`, never for rules that are firing or cooling down and never for rules whose threshold is at or above the high edge. It takes a slot of an hourly cap and one budget unit, looks at a padded crop of the changed region (up to 1536 px) with a differently worded prompt, and averages confidences on agreement; on disagreement the second look wins and the hit becomes a miss. If the cap, the budget or the call refuses, the first verdict stands and the cap slot is returned.

## Sources: files and secrets

`file:` opens a local video with OpenCV (path must be a regular readable file, resolved to an absolute path, known video extension, no `://`). At the end it raises `SourceExhausted` and closes unless `loop=true`; the engine then posts one "finished" notice and does not reconnect. Secrets: `${NAME}` in a source or `stream_urls` entry is read with `get_secret`; every tool and `/sentinel` call also copies the secrets that sources and rules reference into an in-memory cache, which capture threads use when no scope exists (multiplexing gateway). The cache is never persisted, is replaced by a fresh read whenever a scope exists, and is cleared on unload. With neither scope nor cache, the source reports "waiting for secret scope" and retries every 5 s without an offline notice.

## Threading model

All threads are daemons and nothing heavy happens at import or `register()`.

- **Boot timer.** `register()` calls `Sentinel.boot_delayed()`: a cancellable timer starts the engine after 25 s if enabled rules already exist. Short-lived `hermes` commands exit before then, so they never open a camera. Adding a rule through a tool starts the engine immediately. `shutdown()` cancels the timer.
- **Capture thread.** One per source, shared by every rule on that source. It reads a frame every 0.5 s, feeds each rule's gate, and publishes the latest frame under its own small lock (used by `sentinel_look` and `sentinel_teach`). On failure it closes and reopens the source with backoff (1 s doubling to 60 s). It runs inside a copy of the `contextvars` context of the call that started it so secret placeholders resolve. Opens run with a timeout in a helper thread.
- **Judge workers.** A pool of 2. A rule is never judged by two workers at once, so verdicts reach its state machine in order. The queue holds at most one job per rule (a newer frame replaces it) and 8 overall. A worker takes every queued job that came from the same captured frame and source, up to `batch_max_rules` (default 4, ceiling 8), and judges them in one call; rules with teaching examples or a motion question are never grouped. A malformed batch reply falls back to one call per rule; a batch call that itself raises is not retried one by one, each rule gets one judge failure and its backoff. Budget is reserved once per call.
- **Scheduler.** Once a second: polls state machines (cooldown end, stale evidence), runs the retention prune when due, and re-syncs rules from the store every 30 s so rules from another process are picked up.
- **Standby.** If the lock file is held by another process, `start()` returns without capturing and a standby thread retries; it takes over when the owner exits. The standby thread does not spawn another standby.
- **Stop.** `stop()` sets a shutdown flag under the engine lock; an engine is never restarted afterwards, and tool calls in flight at unload cannot revive it. Sources of capture threads still blocked in a read are closed from helper threads so unload never hangs. `JudgeRunner` checks the stop flag before reserving budget and after the judge returns, so a late verdict writes and sends nothing.
- **Locking.** Every lock carries a one-line comment saying what it guards, and no lock is held across I/O except where noted.
  - `EngineState.lock` (re-entrant, shared by every engine collaborator) guards rules, workers, thread handles and the lifecycle flags. `Lifecycle.start` keeps it while it reads the store and takes the process lock, so a start cannot race a stop.
  - `JobQueue.condition` guards the queued jobs and wakes the judge workers. Each source worker has a small lock for its latest-frame slot (read by `sentinel_look` and `sentinel_teach`), and a rule being judged is marked in the shared state, so verdicts stay ordered.
  - `ProcessLock` guards the lock-file handle; `ExampleCache` guards its LRU entries.
  - `Sentinel` has one re-entrant lock for its lazily built services and the closed flag; the module-level registry lock guards the active-`Sentinel` pointer.
  - Leaf locks, each guarding only its own object: `ChangeGate`, `RuleStateMachine`, `Budget`, `HourlyCap`, `Alerter`, `RouteBook`, `SecretCache`, the tool throttles, and the bounded-open helper.
  - `Database` has one re-entrant lock around the SQLite connection (`tx()` and `read()`), and each source serialises `open/read/close` on its own lock.

## Rule state machine

`idle -> candidate -> firing -> cooldown`. Each judged frame produces a verdict with a confidence; verdicts below `confidence_threshold` do not count. A rule fires when k of the last n verdicts agree (and, for `condition` and `absence`, the state has held for `hold_seconds`). After firing it enters cooldown and latches: it needs a confirmed opposite verdict before it can fire again. Evidence older than a few idle intervals is dropped as stale. The machine is pure and clock-injected, so tests drive it deterministically.

While in `candidate`, the engine asks for extra looks spaced `max(2 s, hold / (n - 1))` apart. Rules in cooldown are not queued. For trigger rules the default gate stretches the idle tick to at least 6 h after the start-up look.

## Data model

Under `$HERMES_HOME/plugin-data/hermes-sentinel/`:

| Path | Contents |
|---|---|
| `sentinel.db` | SQLite, WAL. Tables `rules` (id, spec_json, enabled), `events` (rule, ISO-8601 UTC ts, caption, reason, confidence, source, snapshot path, feedback), `examples` (rule, pos/neg, path, event), `usage` (UTC hour bucket, rule, calls), `events_fts` (FTS5 over caption and reason, `LIKE` fallback), plus migrations. |
| `snapshots/YYYY-MM-DD/*.jpg` | Event photos. |
| `examples/<rule_id>/*.jpg` | Teaching examples, re-encoded to at most 768 px, JPEG quality 80. |
| `routes.json` | Per-session routing keys learned from `/sentinel`. |
| `engine.lock` | Exclusive lock file for the single watching process. |

A `RuleSpec` stores the question, mode, hold and cooldown, k, n, threshold, ROI, schedule, action and prompt, source, recipe name, how it was compiled, and the `origin` of its creator (session id, session key, platform, chat id) used for delivery and visibility.

## Alert delivery

The origin is captured inside the tool call (a background thread cannot see the calling session) and stored on the rule. Routes for a chat origin with both `platform` and `chat_id`: the `origin` route only (a keyed injection could land in a classic CLI REPL, so `session_key` is used only when platform or chat id is missing). Local origins try `session_key`, then `session_id`, then a key-less injection. A chat-origin rule never falls back to a local route. `post_message` is an opt-in fallback only for non-chat origins; the engine does not use it. If every route fails, `Alerter.delivery_blocked()` keeps an explanation with the YAML to enable gateway injection until a delivery succeeds. Rate limits: 20 alerts and 6 notices per rolling hour, one alert per rule per 30 s; suppressed alerts are still stored and counted in the next one.

Alert text: instructions first, then `MEDIA:<snapshot>`, then caption and reason inside a quoted `BEGIN/END UNTRUSTED OBSERVATION` block. Untrusted text is length-capped and stripped of `MEDIA:` tokens, slash-leading lines and forged markers.

## Visibility

Messaging callers see and modify only rules whose origin matches their chat (session key, platform + chat id, or session id). Local callers (CLI, TUI, Desktop, API server) see all rules. Other chats' rules look like "no rule".

## Failure handling

| Failure | Behaviour |
|---|---|
| Source cannot open or read | Close, retry with backoff 1 s to 60 s, one "offline" notice after 5 s, one "back online" notice. Missing OpenCV or mss yields an install hint. |
| Vision model error | `Verdict(error=...)`, rule backs off 5 s to 300 s, one notice per failure episode, re-armed after 10 min of success. Gate keeps running. |
| Budget exhausted | Judging pauses, one notice, resumes automatically; `/sentinel budget` reports when. |
| Alert delivery refused | Event is stored; blocked-delivery explanation shown; no retry storm. |
| Store unavailable | Tools and commands report a short error; watcher keeps running where possible. |
| Another process owns the lock | Standby, then takeover. |
| Bad config value | Falls back to the default; listed by `/sentinel status`. |
| Plugin unload | Stop flag, threads joined with a timeout, store closed. |

Retention and the storage cap are enforced at start-up and at least hourly by a background prune, which is also kicked off when tools or commands first touch the store.

## Testing

Tests mirror the package layout: `tests/sources/`, `tests/storage/`, `tests/rules/` (with `machine/`), `tests/vision/` (gate and judge), `tests/judging/`, `tests/alerts/`, `tests/engine/`, `tests/agent/` (`tools/`, `commands/`, `setup/`) and `tests/dashboard/` (`routes/`, `common/`). Slow-drift scenes live in `tests/harness/drift_scenes.py` and the judging rig in `tests/harness/judging_rig.py`. Shared fakes and helpers live in `tests/harness/` (`source_fakes.py`, `store_helpers.py`, ...); a package's fixtures live in its own `conftest.py`.

`tests/harness/` contains `FakeCtx` (enforces the same checks as Hermes' plugin context, including injection permission), a scripted fake LLM that validates schemas, shims for `plugin_data_dir`, `plugin_db` and `get_secret`, and synthetic scenes. `Engine(threaded=False)` is driven step by step in tests.
