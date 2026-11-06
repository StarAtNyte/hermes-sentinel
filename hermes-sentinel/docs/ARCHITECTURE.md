# Architecture

This describes the code as it is. [DESIGN.md](../../DESIGN.md) is the original plan; where they differ (Desktop pane and dashboard backend now exist, six-recipe set, `sentinel_rules` task, route book, process lock), this document is right.

## Data flow

```
capture thread (one per source) --frame--> ChangeGate (one per rule, ROI aware)
    --changed | tick | confirm look--> bounded judge queue (drop oldest)
        --> judge worker (pool of 2): budget -> Judge (ctx.llm) -> RuleStateMachine
            --fired--> snapshot + event in Store --> Alerter --> ctx.inject_message
```

## Module map (`sentinel/`)

Refactored packages (`sources/`, `storage/`, `rules/`) meet the Code standard in `CLAUDE.md`: no structure_lint findings, `__init__.py` files only re-export. Rows marked **flat** are not refactored yet (see "Remaining refactor").

| Path | Role |
|---|---|
| `__init__.py` (plugin root) | `register(ctx)`: aux tasks, tools, `/sentinel`, `on_unload`, delayed boot. |
| `runtime.py` **flat** | `Sentinel`: lazily built service graph (store, budget, engine, route book, alerter, judge) for one plugin context. Delayed boot timer (25 s), prune hooks, `shutdown()`. |
| `config.py` **flat** | `SentinelConfig.load`: reads `config_schema` keys via `ctx.get_config`, validates, records `problems` instead of raising. |
| `sources/` | Frame sources. `base.py` (`FrameSource`), `registry.py` (`open_source`), `discovery.py` (`list_sources`, camera and monitor probes), `frames.py`, `timeouts.py` (bounded opens), `dependencies.py` (lazy OpenCV, numpy, mss), `errors.py`, `constants.py`. `spec/` holds `SourceSpec`, parsing, validation, URL redaction and secret placeholder resolution. `backends/` holds `capture.py` (camera, stream), `snapshot.py`, `screen.py`, `folder.py`. |
| `rules/` | `spec.py` (`RuleSpec`, validated, JSON round-trip), `spec_validation.py`, `schedule.py`, `validation.py`, `errors.py`, `constants.py`, `recipes.py` (six bundled `Recipe`s, `build_rule`). `machine/` is the pure, clock-injected `RuleStateMachine` (`state_machine.py`, `window.py`, `evidence.py`, `types.py`). `compiler/` is `compile_rule` (LLM on `sentinel_rules`, deterministic `fallback.py`, `schema.py`). |
| `storage/` | `store.py` (`Store` facade), `database.py` (connection, single RLock, `tx()`/`read()`), `schema.py` (migrations, FTS5), `files.py` (snapshot and example images), `retention.py` (retention and storage-cap pruning), `records.py`, `errors.py`, `constants.py`. `repositories/` holds `rules.py`, `events.py`, `event_search.py`, `examples.py`, `usage.py`. |
| `vision/` **partial** | `imaging.py`, `gate/` (`ChangeGate`: structural-difference change detection, noise estimation, shake alignment, global-change re-baseline, idle tick; numpy required, OpenCV optional) and `judge/` (`Judge`: request building, `ctx.llm.complete_structured` on task `sentinel_vision`, defensive parsing; failures become `Verdict(error=...)`). Flat `gate.py` and `judge.py` remain beside the package. |
| `alerts/` **partial** | `Alerter` delivery, `formatting.py` (untrusted-text handling), `rate_limit.py`, `route_book.py` (`RouteBook`), `routing.py` (origin capture and chat scoping). |
| `engine.py` **flat** | `Engine`: capture threads, gates, judge queue and workers, scheduler, source health, process lock, standby. |
| `budget.py` **flat** | `Budget`: hourly and rolling-day caps over the store's `usage` table; one exhaustion notice per episode. |
| `tools.py` **flat** | Nine tool schemas and handlers; `register_tools`. |
| `setup_check.py` **flat** | `run_checks(SetupEnv)`: pure, defensive readiness checks; `build_env` wires a live `Sentinel`; shared by `/sentinel setup`, `sentinel_setup_check` and `GET /setup`. |
| `commands.py` **flat** | `SentinelCommand` for `/sentinel`. |
| `dashboard/plugin_api.py` | FastAPI router mounted by Hermes at `/api/plugins/hermes-sentinel/`. Finds the loaded `Sentinel` by scanning `sys.modules` for `hermes_plugins.hermes_sentinel[__home_*]`; masks source specs; serves frames and snapshots as raw JPEG or `?encoding=data_url` JSON. |
| `desktop/plugin.js` | Desktop pane (SDK + React only): polling controller, source tiles, rules, Watch form, events, status chip. No websocket. |

Import from a package root (`from .storage import Store`, `from .rules import RuleSpec`, `from .sources import open_source`); there are no compatibility shims at the old module paths.

## Remaining refactor

TODO (tracked by `python3 scripts/structure_lint.py --report`, printed non-failing by `scripts/check.sh`). These still violate the size, nesting or docstring limits and are to be split into packages in the same style as `sources/`, `storage/` and `rules/`:

- **vision**: finish `vision/` (move flat `gate.py` and `judge.py` into `vision/gate/` and `vision/judge/`, leave no flat modules).
- **alerts**: finish `alerts/` (the facade and its tests; `tests/alerts/` is still empty, `tests/test_alerts.py` is flat).
- **engine**: `engine.py` (about 1200 lines, `Engine` about 940 lines) into `engine/` (capture workers, scheduler, judge pool, process lock, source health).
- **agent layer**: `tools.py` (about 1000 lines, `SentinelTools` 500), `commands.py`, `setup_check.py`, `runtime.py`, `config.py`, `budget.py` and `dashboard/plugin_api.py` into an `agent/` package. This also resolves the `flat-package` finding on `sentinel/` and the logic in the plugin root `__init__.py`.
- Docstrings for the public functions in `dashboard/plugin_api.py` and `budget.py`.
- When `tools` and `setup_check` move, repoint the `list_sources` patches in `tests/test_setup_check.py` and `tests/test_tools.py` to the real module.

## Threading model

All threads are daemons and nothing heavy happens at import or `register()`.

- **Boot timer.** `register()` calls `Sentinel.boot_delayed()`: a cancellable timer starts the engine after 25 s if enabled rules already exist. Short-lived `hermes` commands exit before then, so they never open a camera. Adding a rule through a tool starts the engine immediately. `shutdown()` cancels the timer.
- **Capture thread.** One per source, shared by every rule on that source. It reads a frame every 0.5 s, feeds each rule's gate, and publishes the latest frame under its own small lock (used by `sentinel_look` and `sentinel_teach`). On failure it closes and reopens the source with backoff (1 s doubling to 60 s). It runs inside a copy of the `contextvars` context of the call that started it so secret placeholders resolve. Opens run with a timeout in a helper thread.
- **Judge workers.** A pool of 2. A rule is never judged by two workers at once, so verdicts reach its state machine in order. The queue holds at most one job per rule (a newer frame replaces it) and 8 overall.
- **Scheduler.** Once a second: polls state machines (cooldown end, stale evidence), runs the retention prune when due, and re-syncs rules from the store every 30 s so rules from another process are picked up.
- **Standby.** If the lock file is held by another process, `start()` returns without capturing and a standby thread retries; it takes over when the owner exits. The standby thread does not spawn another standby.
- **Stop.** `stop()` sets a shutdown flag under the engine lock; an engine is never restarted afterwards, and tool calls in flight at unload cannot revive it. Sources of capture threads still blocked in a read are closed from helper threads so unload never hangs. `_run_job` checks the stop flag before reserving budget and after the judge returns, so a late verdict writes and sends nothing.
- **Locking.** `worker.lock` guards only short state swaps, never I/O; `latest_lock` guards the latest-frame slot. Store access goes through one re-entrant lock. Sources serialise `open/read/close` on a per-source lock.

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

Tests mirror the package layout: `tests/sources/`, `tests/storage/`, `tests/rules/` (with `machine/`), `tests/vision/`. Shared fakes and helpers live in `tests/harness/` (`source_fakes.py`, `store_helpers.py`, ...); a package's fixtures live in its own `conftest.py`.

`tests/harness/` contains `FakeCtx` (enforces the same checks as Hermes' plugin context, including injection permission), a scripted fake LLM that validates schemas, shims for `plugin_data_dir`, `plugin_db` and `get_secret`, and synthetic scenes. `Engine(threaded=False)` is driven step by step in tests.
