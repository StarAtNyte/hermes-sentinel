# Architecture

This describes the code as it is. [DESIGN.md](../../DESIGN.md) is the original plan; where they differ (no Desktop pane or dashboard backend, six-recipe set, `sentinel_rules` task, route book, process lock), this document is right.

## Data flow

```
capture thread (one per source) --frame--> ChangeGate (one per rule, ROI aware)
    --changed | tick | confirm look--> bounded judge queue (drop oldest)
        --> judge worker (pool of 2): budget -> Judge (ctx.llm) -> RuleStateMachine
            --fired--> snapshot + event in Store --> Alerter --> ctx.inject_message
```

## Module map (`sentinel/`)

| Module | Role |
|---|---|
| `__init__.py` (plugin root) | `register(ctx)`: aux tasks, tools, `/sentinel`, `on_unload`, delayed boot. |
| `runtime.py` | `Sentinel`: lazily built service graph (store, budget, engine, route book, alerter, judge) for one plugin context. Delayed boot timer (25 s), prune hooks, `shutdown()`. |
| `config.py` | `SentinelConfig.load`: reads `config_schema` keys via `ctx.get_config`, validates, records `problems` instead of raising. |
| `sources.py` | `FrameSource` implementations (camera, stream, snapshot URL, screen, folder), `SourceSpec.parse`, `open_source`, `list_sources`, secret placeholder resolution, URL redaction, open timeouts. OpenCV and mss import lazily. |
| `gate.py` | `ChangeGate`: structural-difference change detection, noise estimation, shake alignment, global-change re-baseline, idle tick. numpy required, OpenCV optional. |
| `judge.py` | `Judge`: builds the request (frame, ROI crop, up to 2+2 examples), calls `ctx.llm.complete_structured` on task `sentinel_vision`, parses defensively; failures become `Verdict(error=...)`. Also `describe()` for `sentinel_look`. |
| `rules.py` | `RuleSpec` (validated, JSON round-trip), `Schedule`, `RuleStateMachine`, `compile_rule` (LLM on `sentinel_rules`, deterministic fallback). |
| `recipes.py` | Six bundled `Recipe`s and `build_rule`. |
| `engine.py` | `Engine`: capture threads, gates, judge queue and workers, scheduler, source health, process lock, standby. |
| `budget.py` | `Budget`: hourly and rolling-day caps over the store's `usage` table; one exhaustion notice per episode. |
| `store.py` | `Store`: SQLite (rules, events, examples, usage, FTS5), snapshot files, retention and storage-cap pruning. |
| `alerts.py` | `Alerter`: message formatting, untrusted-text handling, routes, rate limits, delivery-blocked state; `RouteBook`; origin capture and chat scoping helpers. |
| `tools.py` | Eight tool schemas and handlers; `register_tools`. |
| `commands.py` | `SentinelCommand` for `/sentinel`. |

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

`tests/harness/` contains `FakeCtx` (enforces the same checks as Hermes' plugin context, including injection permission), a scripted fake LLM that validates schemas, shims for `plugin_data_dir`, `plugin_db` and `get_secret`, and synthetic scenes. `Engine(threaded=False)` is driven step by step in tests.
