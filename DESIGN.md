# Hermes Sentinel — design

> Give Hermes eyes. Tell it in plain words what to watch for on any camera or screen,
> and it alerts you (or acts) when it happens — and remembers what it saw.

Target: Nous Research Hermes Agent Plugin Hackathon (due 2026-11-11 17:00 EST).
Judged on usefulness, reliability, polish, creativity. All rules in `CLAUDE.md` apply.

## 1. User-facing behaviour

```
You:    watch my webcam and tell me if someone takes my coffee mug
Hermes: Watching "webcam 0" for: "the coffee mug is no longer on the desk".
        Checks only when something moves. Cap: 60 vision checks/hour.
...
Hermes: [photo] Sentinel: the mug was picked up by a person in a grey hoodie (conf 0.92).
        Reply "false alarm" if that's wrong.
You:    what happened at my desk today?
Hermes: 3 events: 09:12 mug placed, 11:40 someone sat down, 14:03 mug taken [photos]
```

Everything works from the CLI and any messaging platform. Hermes Desktop adds a pane.

### Rule modes
| mode | meaning | example |
|---|---|---|
| `trigger` | fire when the visual question becomes true | "package left at the door" |
| `condition` | fire when true continuously for `hold_seconds` | "stove on and nobody in kitchen for 5 min" |
| `absence` | fire when something expected is missing for `hold_seconds` | "dog not on its bed for 10 min" |

### Recipes (bundled, one-line setup)
package-at-door, pet-on-couch, print-failure (3D print spaghetti), training-loss-plateau (screen),
desk-intruder, stove-unattended.

## 2. Architecture

```
Source --frames--> ChangeGate --changed frame / periodic tick--> Judge (vision LLM)
 (webcam, RTSP/HTTP,          (local, ~free)                     (ctx.llm, aux task
  snapshot URL, screen,                                            "sentinel_vision")
  image folder)                                                          | verdict
                                                                         v
                                    RuleStateMachine (k-of-n confirm, hold, cooldown)
                                                                         | event
                              +------------------------------------------+-------------+
                              v                                          v             v
                        EventStore (SQLite +                      Alerter        Desktop pane
                        JPEG snapshots, retention)       (inject_message ->     (plugin_api.py
                                                          agent relays MEDIA:,   REST, polling)
                                                          runs action_prompt)
```

### Modules (`hermes-sentinel/sentinel/`)
- `sources.py` — `FrameSource` protocol: `open()`, `read() -> (ts, ndarray BGR)`, `close()`;
  raises `SourceError`. Implementations: `CameraSource` (cv2 index), `StreamSource` (RTSP/HTTP
  MJPEG via cv2), `SnapshotUrlSource` (HTTP JPEG endpoint, urllib), `ScreenSource` (mss,
  monitor or region), `FolderSource` (newest image in a dir). `open_source(spec)` factory,
  `list_sources()` probe (camera indexes 0-4, monitors).
- `gate.py` — `ChangeGate`: downscale to 320px wide, grayscale, blur, brightness-normalise
  (divide by frame mean, rejects lighting flicker), running background (EMA), absdiff, threshold,
  morphological open, changed-area ratio inside ROI. Returns `GateResult(changed, ratio, bbox)`.
  If ratio > `global_change_max` (0.6) treat as lighting/shake: re-baseline, not changed.
  Also emits a periodic `tick` every `idle_check_seconds` so state rules (condition/absence)
  are re-judged without motion.
- `judge.py` — builds the vision request: current frame (JPEG, <=1024px) + optional ROI crop +
  up to 2 positive and 2 negative example images from feedback/teaching. Calls
  `ctx.llm.complete_structured(instructions, input, json_schema=VERDICT, task="sentinel_vision",
  purpose="sentinel:<rule_id>")`. VERDICT = `{match: bool, confidence: 0..1, reason: str,
  caption: str}`. Parses defensively; LLM failure returns `Verdict(error=...)`, never raises.
- `rules.py` — `RuleSpec` dataclass + `compile_rule(text, source)` (one LLM call turning plain
  English into a RuleSpec: `question` (yes/no visual question), `mode`, `hold_seconds`,
  `cooldown_seconds`, `roi`, `schedule`, `action` (`notify` | `agent`), `action_prompt`);
  deterministic fallback if LLM unavailable (question = text, mode trigger).
  `RuleStateMachine`: idle -> candidate -> firing -> cooldown, k-of-n positive verdicts
  (default 2 of 3) with confidence >= threshold; for condition/absence requires truth across
  `hold_seconds`. Pure, clock-injected, fully unit-tested.
- `store.py` — SQLite via `plugin_db("hermes-sentinel")`: `rules`, `events`
  (id, rule_id, ts ISO-8601 UTC, caption, confidence, snapshot_path, feedback), `examples`
  (rule_id, kind pos/neg, path), `usage` (hour bucket, vision_calls). FTS5 over captions for
  queries. Snapshots under `plugin_data_dir/snapshots/YYYY-MM-DD/`. Retention: `retention_days`
  (7) and `max_storage_mb` (500), pruned hourly.
- `budget.py` — vision-call caps per hour/day (config), persisted counters; when exhausted the
  engine pauses judging and posts one notice.
- `engine.py` — `Engine`: owns one capture thread per active source (shared by rules on that
  source), a judge worker pool (size 2), the scheduler, health (reconnect with exponential
  backoff 1s->60s, one "source offline" alert, one "back online"). Start lazily on first rule or
  on load if rules exist; stop on `ctx.on_unload`. Thread-safe, never blocks the agent loop.
- `alerts.py` — on event: `ctx.inject_message(...)` into the rule's stored origin/session with a
  compact text containing caption, confidence, event id, `MEDIA:<snapshot>` and, for
  `action=agent`, the `action_prompt`. Fallback `ctx.post_message`. Rate-limited.
  Never waits for a human (rule 12).
- `tools.py` — agent tools (section 3). `commands.py` — `/sentinel` slash command.
- `dashboard/plugin_api.py` — FastAPI router for the Desktop pane.
- `desktop/plugin.js` — pane: source tiles (latest frame, 1 fps polling), rule list with
  pause/resume, event timeline with thumbnails and correct/false-alarm buttons. SDK only.

## 3. Agent tools
| tool | purpose |
|---|---|
| `sentinel_sources` | list available cameras/monitors/configured streams |
| `sentinel_watch` | create a rule from plain English (+ source, optional recipe, roi) |
| `sentinel_rules` | list rules with status, last event, checks used |
| `sentinel_update` | pause / resume / delete / edit a rule |
| `sentinel_look` | grab a frame now and describe it (or answer a question about it) |
| `sentinel_events` | query event memory ("what happened at the door today?"), FTS + time range |
| `sentinel_feedback` | mark an event correct / false alarm -> example images + threshold tuning |
| `sentinel_teach` | attach a positive/negative example image to a rule |

Slash: `/sentinel status|list|pause <id>|resume <id>|test <id>|budget`.

## 4. Config (`config_schema`)
`default_source`, `vision_calls_per_hour` (60), `vision_calls_per_day` (500),
`confirm_k` (2), `confirm_n` (3), `confidence_threshold` (0.7), `idle_check_seconds` (300),
`retention_days` (7), `max_storage_mb` (500), `stream_urls` (dict name->url; credentials only
via secret scope), `alert_snapshots` (true).

## 5. Reliability rules
- Gate before judge, always. No frame reaches the LLM unless changed or a scheduled tick.
- k-of-n confirmation + cooldown + rate limit per rule and global.
- Every external failure is caught, logged, surfaced once, retried with backoff.
- No unbounded growth: retention, storage cap, bounded queues (drop oldest frame).
- Deterministic tests with synthetic scenes (`tests/harness/scenes.py`) and FakeCtx.
- Works without Desktop, without network (judge pauses, gate keeps running), without GPU.

## 6. Catalog compliance map
| rule | how |
|---|---|
| 3 no self-update | none; updates only via SHA bumps |
| 6 capabilities | `test_register.py` asserts declared == registered |
| 8 desktop SDK | pane uses `@hermes/plugin-sdk` + react only; `rule_lint.py` |
| 9 no core overrides | only `ctx.*` public APIs; `rule_lint.py` |
| 10 deps | `opencv-python-headless`, `numpy`, `mss` with floors + upper bounds |
| 11 credentials | stream credentials via secret scope only |
| 12 unattended | nothing blocks on a human; alerts are fire-and-forget |
| 13 disclosure | README + PR: camera/screen capture, background threads, frames sent to the configured vision model, local snapshot storage + retention |
| 14 compat | `requires_hermes` SemVer floor in plugin.yaml + entry |
| 17 no binaries | only source; no media committed; fixtures generated at test time |

## 7. Schedule
- Nov 6: dev harness (workflow); core engine: sources, gate, state machine, store, budget.
- Nov 7: judge, rule compiler, alerts, tools, slash command; FakeCtx end-to-end tests.
- Nov 8: live test in real Hermes (CLI + Telegram), feedback/teach, event queries, recipes.
- Nov 9: Desktop pane, README with GIFs, disclosure, hardening.
- Nov 10: validate/doctor clean, cross-platform check, demo recording.
- Nov 11: user publishes repo, opens catalog PR, posts demo on X, submits form.

## 8. Demo script (<=90 s)
1. (0-10s) "Hermes, watch my webcam and text me if someone takes my mug."
2. (10-30s) Hand takes mug -> phone buzzes on Telegram with photo + caption.
3. (30-50s) "Tell me when training loss plateaus" on screen -> alert with chart crop.
4. (50-70s) "False alarm" feedback -> rule tunes; Desktop pane timeline.
5. (70-90s) "What happened at my desk today?" -> timeline answer with thumbnails.
