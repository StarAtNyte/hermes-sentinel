# hermes-sentinel

Give Hermes eyes: describe in plain English what to watch for on a camera or your screen, and Hermes alerts you (with a photo) when it happens and remembers what it saw.

## What it does

You tell Hermes what to watch. Sentinel checks frames on your machine with a cheap change detector and asks a vision model about a frame only when something changed. When the answer is confirmed across several looks, it wakes the agent in the chat that created the rule.

```
You:    watch my webcam and tell me if someone takes my coffee mug
Hermes: Watching "camera:0" for: "Is the coffee mug missing from the desk?". Checks only
        when something moves. Cap: 60 vision checks/hour, 500/day.
        ...
Hermes: [photo] Sentinel: the mug was picked up by a person in a grey hoodie
        (confidence 0.92, event #14). Reply "false alarm" if that is wrong.
You:    false alarm, that was me
Hermes: Marked event 14 as a false alarm. The photo is now a "should not trigger" example
        and the rule is slightly stricter.
You:    what happened at my desk today?
Hermes: 3 events: 09:12 mug placed, 11:40 someone sat down, 14:03 mug taken [photos]
```

It works from the classic CLI, from messaging platforms and from Hermes Desktop, which adds a Sentinel pane (see [Desktop pane](#desktop-pane)).

## Features

- Plain-English rules, compiled once into a precise yes/no visual question, a mode, hold time and cooldown.
- Sources: webcam, screen (whole monitor or a region), RTSP and HTTP streams, snapshot (JPEG) URLs, and an image folder.
- Three rule modes: `trigger`, `condition` (true for N seconds), `absence` (expected thing missing for N seconds).
- Six bundled recipes that need no setup and no rule-compiling call.
- A local change gate that rejects sensor noise, lighting changes, shadows and small camera shake before any model is called.
- k-of-n confirmation (2 of 3 by default), confidence threshold, per-rule cooldown, global alert rate limit.
- Hard budgets on vision calls per hour and per rolling 24 hours, persisted across restarts.
- Event memory: captions, times and photos in a local SQLite database, searchable by text and time.
- Feedback loop: mark alerts correct or false alarm; teach rules with example images.
- Optional agent action: a rule can carry an instruction ("turn on the porch light") that the agent carries out when it fires.
- Active-hours schedules per rule, region-of-interest per rule.
- Alert text built to resist prompt injection from what the camera sees.

## How it works

```
 Source          Change gate            Vision judge          Confirmation        Alert / action
 ------          -----------            ------------          ------------        --------------
 camera:N   -->  local, no model   -->  one structured   -->  k-of-n verdicts  --> event stored
 screen          grayscale, blur,       call to your          >= confidence,       (SQLite + JPEG)
 rtsp://         brightness-normalise,  vision model          hold time,           inject_message
 snapshot:       compare to running     (aux task             cooldown             into the chat,
 folder:         background, ROI        sentinel_vision)                           MEDIA: photo,
                 changed? or idle tick?                                            optional action
        |                |                     |                    |
        +-- frames never leave the machine until the gate says "changed" or a tick is due
```

Why it is cheap: the gate runs on every frame at no cost, so a quiet scene sends nothing to the model. Trigger rules do one start-up look and then wait for motion (their idle tick is stretched to at least 6 hours). Condition and absence rules re-check on `idle_check_seconds` (default 300 s) so they notice states that do not involve motion. Rules in cooldown are not judged at all. A queue holds at most one pending job per rule and eight overall.

Why it is reliable: one noisy verdict cannot fire a rule (k-of-n, confidence threshold), one rule cannot flood you (cooldown, 20 alerts/hour overall), and a failing source or model is reported once and retried with backoff rather than repeated. Every external failure is caught; nothing blocks the agent loop.

## Requirements

- Hermes Agent 0.22.5 or newer (`requires_hermes: ">=0.22.5"`).
- Python 3.11 or newer.
- A vision-capable model reachable through Hermes (see Required setup).
- Python packages installed by Hermes from `plugin.yaml`: `numpy>=1.26,<3`, `opencv-python-headless>=4.8,<6`, `mss>=9,<11`.
- A webcam, a screen, or a network camera, depending on what you watch. Screen capture needs a display (not available on a headless server).

## Install

Once the catalog entry is merged:

```
hermes plugins install hermes-sentinel
```

Until then, or for development, link a local checkout. From the repository root, the helper script prints the commands and changes nothing unless you pass `--apply`:

```
scripts/dev_install.sh                     # dry run against $HERMES_HOME or ~/.hermes
scripts/dev_install.sh --home /tmp/h --apply   # try it in a scratch Hermes home
scripts/dev_install.sh --uninstall --apply     # remove the link again
```

It symlinks `hermes-sentinel/` into `<home>/plugins/`, runs `hermes plugins enable hermes-sentinel --no-allow-tool-override` and then `hermes plugins doctor hermes-sentinel --ci`. Applying against your real `~/.hermes` edits its `config.yaml`; prefer a scratch `--home`.

## Required setup

Three things must be in place or alerts will not arrive.

**1. Enable the plugin.**

```
hermes plugins enable hermes-sentinel
```

**2. Allow alert injection.** Sentinel delivers alerts by injecting a message into your chat session. Outside the classic in-process CLI, Hermes refuses this unless you allow it. This is off by default. Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    hermes-sentinel:
      allow_gateway_injection: true
```

If every delivery fails, `/sentinel status` and the tool replies show this exact snippet until a delivery succeeds.

**3. Point the vision judge at a vision-capable model.** Sentinel registers two auxiliary tasks you can route independently in Hermes' model settings (the `auxiliary.<task>` block of `config.yaml`, or the auxiliary-task picker):

| Task | Used for | Model needed |
|---|---|---|
| `sentinel_vision` | Judging frames, `sentinel_look`, `/sentinel test` | Vision-capable. This is where cost goes. |
| `sentinel_rules` | Compiling plain-English requests into rules | Text only; a small fast model is enough. Without it, a deterministic fallback is used (the text becomes the question). |

Example (names depend on your providers):

```yaml
auxiliary:
  sentinel_vision:
    provider: openrouter
    model: google/gemini-2.5-flash
  sentinel_rules:
    provider: openrouter
    model: google/gemini-2.5-flash-lite
```

**Telegram, Discord and other chats.** In a messaging chat Hermes does not hand tools the chat's routing key; slash commands get it. Send `/sentinel here` once in that chat so Sentinel learns where to deliver. `/sentinel here all` moves your existing rules in that chat to the current one. A rule created in a chat is delivered to that chat (by platform and chat id, never to the local CLI session of the process that happens to run it), and that chat only sees its own rules. If the chat route cannot be used, the alert is reported as undelivered rather than sent elsewhere.

## Desktop pane

Hermes Desktop shows a Sentinel page (sidebar entry) and a status-bar chip. It is enabled by default. The page has:

- source tiles with the latest captured frame (refreshed about once a second while the page is open, paused when the window is hidden), online/offline state and the number of rules using each source;
- the rules list with mode, state, vision calls per hour, last event and cooldown, and Pause / Resume / Delete (delete asks to confirm). Long status text such as `source offline: ...` wraps instead of overflowing;
- a Watch form: a plain-English text, a free-text Source field (leave empty for the default source; examples `camera:0`, `screen`; suggestions never include masked keys) and an optional recipe. Credentialed sources are masked in listings and cannot be typed back; use a named `stream_urls` entry with `${SECRET}` placeholders;
- the event timeline with snapshot thumbnails and correct / false-alarm buttons that feed the same tuning as the `sentinel_feedback` tool;
- a compact Setup checklist (fetched once from `GET /setup`, without probing cameras; a button runs the device probe) shown while any check is not ok;
- a budget meter and, if Hermes refuses injected alerts, the exact `allow_gateway_injection` YAML to copy.

It polls the plugin's REST API at `/api/plugins/hermes-sentinel/` (no websocket) and fetches frames and snapshots as JSON data URLs (`?encoding=data_url`; without the parameter the endpoints return a raw JPEG). URL credentials are masked in every response, including token-only userinfo and secret-looking RTSP path segments.

**Which process hosts the pane.** `dashboard/plugin_api.py` is a thin view over the Sentinel running in the process that loaded the plugin (it finds it by scanning `sys.modules` for `hermes_plugins.hermes_sentinel` or the profile-scoped `hermes_plugins.hermes_sentinel__home_*` names). Camera frames only exist in the process that owns the watcher (`engine.lock`). If Desktop is served by a separate dashboard process that never loaded the plugin, the pane shows "Sentinel is not loaded in this process" and writes return 503; start Desktop against the Hermes process that runs your agent. Everything else (CLI, chat, alerts) is unaffected.

## Quick start

```
/sentinel setup                               # step 1: checks packages, config, cameras, delivery, storage; tells you what to fix
/sentinel status                              # then: state of the watcher; shows budget and the YAML above
/sentinel here                                # once per session while Sentinel does not know its key (chat or local)
"Hermes, use the package-at-door recipe on camera:0"   # or any plain-English request
```

Then, any time: `/sentinel list`, `/sentinel test <rule_id>`, `/sentinel pause <rule_id>`.

## Sources

| Spec | Watches | Notes |
|---|---|---|
| `camera:N` (or just `N`) | Webcam by OpenCV index | Default is `camera:0`. `webcam`, `cam:N`, `webcam:N` and a bare `camera`/`webcam` (index 0) also work. `sentinel_sources` probes indexes 0-4. |
| `screen`, `screen:N` | Monitor N (1 = primary, 0 = all) | Needs a display. `monitor:N` is an alias. |
| `screen:N@x,y,w,h` | A region of a monitor | Pixels, relative to the monitor. |
| `rtsp://host/path` | RTSP stream | Via OpenCV/FFmpeg. `rtsps`, `rtmp`, `rtmps`, `udp` and `tcp` URLs are accepted as streams too. |
| `stream:http://host/video.mjpg` | HTTP(S) MJPEG or video stream | URLs ending `.mjpg`, `.m3u8`, `.mp4` and similar are treated as streams. |
| `snapshot:https://host/cam.jpg` | A JPEG URL, fetched once per read | A plain `http(s)://` URL without a video extension is also a snapshot. Redirects are followed; the `Authorization` header is dropped when the host changes. |
| `folder:/path/to/dir` | The newest image file in a folder | Useful for cameras that drop files, or for testing. `dir:/path` and a bare existing directory path work too. |
| `stream:<name>` or `<name>` | A named stream from `stream_urls` | See below. |

Named streams and secrets. Put cameras with passwords in config using `${SECRET}` placeholders; they are resolved through the Hermes secret scope at connect time and never written to disk, rules or error text. A literal password in a URL is rejected.

```yaml
plugins:
  entries:
    hermes-sentinel:
      settings:
        stream_urls:
          door: rtsp://admin:${DOORCAM_PASSWORD}@10.0.0.5/live
```

Then ask for `stream:door`. Limitation: a rule that needs a secret and was loaded at start-up (not created or resumed by a tool call in this run) cannot see the secret scope, so its camera stays offline until you resume the rule or create it again. See Limitations.

## Rule modes

| Mode | Fires when | Example |
|---|---|---|
| `trigger` | The question is confirmed true (k of n looks). Latches until it is confirmed false again. | "someone takes my coffee mug", "package left at the door" |
| `condition` | The question has been true continuously for `hold_seconds`. One alert per episode. | "the stove is on and nobody is in the kitchen for 5 minutes" |
| `absence` | Something expected is confirmed missing for `hold_seconds`. The question stays positive ("Is the dog on its bed?") and a confident "no" counts. | "the dog is not on its bed for 10 minutes" |

Giving `hold_seconds > 0` to a trigger rule turns it into a condition rule. Other per-rule options: `cooldown_seconds` (quiet time after an alert, default 300), `roi` (`[x, y, w, h]` fractions of the frame; only changes inside it count), `schedule` (`start`/`end` as `HH:MM`, optionally crossing midnight, and `days`), and `action_prompt` (an instruction for the agent when the rule fires).

## Recipes

Recipes are hand-tuned rules that skip the compiling call and behave the same offline. Ask for one by name. Details and tuning tips are in [docs/RECIPES.md](docs/RECIPES.md).

| Recipe | Mode | Hold | Cooldown | Suggested source |
|---|---|---|---|---|
| `package-at-door` | trigger | - | 30 min | camera |
| `pet-on-couch` | trigger | - | 15 min | camera |
| `print-failure` | condition | 60 s | 30 min | camera (threshold at least 0.75) |
| `training-loss-plateau` | condition | 15 min | 60 min | screen |
| `desk-intruder` | trigger | - | 10 min | camera |
| `stove-unattended` | condition | 5 min | 30 min | camera |

## Tools

The agent uses these; you normally just talk to Hermes.

| Tool | Purpose |
|---|---|
| `sentinel_sources` | List webcams, monitors and configured streams (`probe_cameras=false` skips the probe). |
| `sentinel_watch` | Create a rule from plain English (`what`) or a `recipe`; options `source`, `roi`, `hold_seconds`, `cooldown_seconds`, `schedule`, `action_prompt`. |
| `sentinel_rules` | List rules with status (watching, paused, source offline, starting), last event and checks used. |
| `sentinel_update` | `pause`, `resume`, `delete` (optionally `purge_events`) or `edit` a rule. |
| `sentinel_look` | Grab a frame now and describe it or answer a question. Returns a `MEDIA:` photo. Costs one vision check. |
| `sentinel_events` | Search captured events by text, rule, time range (`today`, `yesterday`, `2h`, ISO-8601) and feedback. |
| `sentinel_feedback` | Mark an event `correct` or `false_alarm`. A false alarm becomes a negative example and raises the rule's confidence threshold by 0.05 (to at most 0.95). Repeating the same verdict changes nothing. |
| `sentinel_setup_check` | Readiness check with the exact fix for each problem (same as `/sentinel setup`). Takes no arguments. |
| `sentinel_teach` | Add a `yes` or `no` example image from a file, an event photo, or the live frame. The two newest of each kind are sent with every check. |

At most 50 rules can exist in total, across all chats (not 50 per chat). Plain-English compiling is limited to 10 per hour per running plugin instance (recipes are not counted).

## Slash commands

`/sentinel [subcommand]` (alias for status when empty):

| Command | What it does |
|---|---|
| `setup` (`check`, `doctor`) | One-command readiness check: Python packages, `allow_gateway_injection`, vision model, cameras and monitors, `default_source`, watcher, storage and disk, alert route, budget, rules. Ends with `Ready: yes/no - N things to fix`. Probes cameras for a few seconds. |
| `status` (or `st`) | State of the watcher, sources, rules, budget, delivery problems and config warnings. |
| `list` (`ls`, `rules`) | Rules with state and last event. |
| `pause <rule_id>` / `resume <rule_id>` | Disable or enable a rule. |
| `test <rule_id>` | Look once and report what the judge says and whether it would count as evidence. Uses one vision call from the budget. |
| `budget` | Vision calls used this hour and in the last 24 hours, and when capacity returns. |
| `here [<rule_id>\|all]` | Send alerts from this session's rules (or one rule, or all of your rules) to the chat where you typed it. |
| `help` | Usage line. |

In a messaging chat you only see and control the rules created in that chat. Local sessions (CLI, TUI, Desktop) see everything.

## Configuration

Set under `plugins.entries.hermes-sentinel.settings.<key>` in `config.yaml`. Invalid values fall back to the default and are listed by `/sentinel status`.

| Key | Default | Meaning |
|---|---|---|
| `default_source` | `camera:0` | Source used when a request names none. |
| `vision_calls_per_hour` | `60` | Cap on vision checks per clock hour, across all rules. `0` pauses judging; `null`, `-1`, `unlimited`, `none` or `off` removes the cap. |
| `vision_calls_per_day` | `500` | Cap per rolling 24 hours, across all rules. |
| `confirm_k` | `2` | Positive verdicts needed out of the last `confirm_n`. Must not exceed `confirm_n`. |
| `confirm_n` | `3` | Size of the confirmation window (1-20). |
| `confidence_threshold` | `0.7` | Minimum judge confidence (0-1) for a verdict to count. |
| `idle_check_seconds` | `300` | Re-check condition and absence rules this often without motion (trigger rules stretch it to at least 6 hours). Range 0 to 7 days; `0` disables the gate tick. |
| `retention_days` | `7` | Delete events and snapshots older than this. Minimum 0.01. |
| `max_storage_mb` | `500` | Snapshot storage cap; oldest photos are pruned first. Minimum 1. |
| `stream_urls` | `{}` | Named streams, name to URL, with `${SECRET}` placeholders. At most 64 entries, names up to 64 characters, URLs up to 2048 characters. |
| `alert_snapshots` | `true` | Attach the event photo (`MEDIA:`) to alerts. |

New rules copy `confirm_k`, `confirm_n` and `confidence_threshold` at creation; changing the settings later does not alter existing rules.

## Cost control

Sentinel is designed so that a quiet scene costs nothing and a busy one hits a ceiling you chose.

- **Gate first.** No frame reaches the model unless the local gate saw a change, an idle tick is due, or a rule is collecting its confirmation looks.
- **Idle ticks.** Trigger rules: one start-up look, then ticks no more often than every 6 hours. Condition/absence rules: every `idle_check_seconds`.
- **k-of-n.** A candidate event needs 2 positives out of 3 looks by default. Looks are spaced about 2 seconds apart, or `hold / (n - 1)` for rules with a hold time.
- **Cooldown.** After an alert a rule is not judged until its cooldown ends.
- **Queue.** At most one pending job per rule and eight overall; the oldest is dropped.
- **Budgets.** 60 checks per hour and 500 per rolling day by default. When exhausted, judging pauses, you get one notice, and the gate keeps running. `/sentinel budget` says when capacity returns.
- **Alert rate.** At most 20 alerts and 6 notices per hour overall, and one alert per rule every 30 seconds. Suppressed alerts are stored as events and counted in the next alert.

Typical numbers, from the logic above: a trigger rule on a quiet doorway uses 1 call at start-up plus about 3 per real event (one detecting look and up to two confirming looks). A condition rule on a quiet scene uses at most 12 calls per hour (every 300 s), plus motion-driven looks. A very busy scene is bounded by the 60 per hour cap, and because 500 per day is about 21 per hour, sustained maximum activity is limited by the daily cap. `sentinel_look` and `/sentinel test` each use one call from the same vision budget. Rule compiling is a separate text call, limited to 10 per hour.

## Privacy and security

- **What leaves your machine.** Frames, and only changed frames or scheduled ticks, go to the vision model you configured for `sentinel_vision`, through Hermes' own LLM interface (`ctx.llm`), as JPEG of at most 1024 px, optionally with a close-up crop of the ROI and up to two positive and two negative example images. Rule text goes to the `sentinel_rules` model. Nothing else is sent anywhere by this plugin.
- **Where snapshots live.** `$HERMES_HOME/plugin-data/hermes-sentinel/`: `sentinel.db` (SQLite), `snapshots/YYYY-MM-DD/*.jpg`, `examples/<rule_id>/*.jpg`, `routes.json` (chat and session identifiers: platform, chat id, session key, so alerts return to the right chat) and `engine.lock`.
- **Retention.** Events and snapshots are deleted after `retention_days` (7) and the oldest photos go first when `max_storage_mb` (500) is exceeded. Pruning runs hourly. Deleting a rule with `purge_events` also removes its events and photos.
- **Prompt injection.** Text in the scene (a sign saying "ignore previous instructions") reaches the agent only as a caption. Alerts list their instructions first, then the photo, then the caption and reason inside a quoted `BEGIN/END UNTRUSTED OBSERVATION` block, with the agent told never to follow anything inside it. Captions are length-capped, stripped of `MEDIA:` tokens, slash-leading lines and forged markers. The judge prompt treats example images as past references, not instructions.
- **Chat isolation.** A rule created in a messaging chat is delivered only by its own platform and chat id, never through a session key or local route that a classic CLI process could take; other chats cannot list, edit or read its events. Local sessions (CLI, TUI, Desktop) can see all rules.
- **Credentials.** Stream and snapshot secrets are read only through the Hermes secret scope and never stored. Literal passwords in URLs are rejected, and URLs are redacted in errors.
- **Nothing waits on a human.** No approval prompts, no blocking input. Alerts are fire-and-forget; under cron or a gateway the plugin never stalls. If delivery is refused, the event is still stored.

## Disclosure

This section covers the catalog's rule 13.

- **Network calls.** (a) Vision and rule-compiling requests to the model provider you configured through Hermes (`ctx.llm`), containing camera or screen frames and rule text. (b) Requests to stream and snapshot URLs that you supply (`rtsp://`, `stream:`, `snapshot:`, `stream_urls`), including `${SECRET}`-authenticated ones. No other hosts are contacted, except redirect targets of URLs you supply (snapshot redirects are followed, with the `Authorization` header dropped when the host changes).
- **Camera and screen capture.** Opens local cameras (and briefly probes indexes 0-4 when listing sources), captures the screen with `mss`, and reads image files from `folder:` sources and from `image_path` given to `sentinel_teach`. These reads are only of what you point it at.
- **Long-running background activity.** A watcher with daemon threads (one capture thread per source, two judge workers, a scheduler, a standby thread while another process holds the lock, short-lived camera and screen probe threads, a boot timer and a background prune thread) starts when a rule exists, about 25 seconds after load if enabled rules already exist, or when a tool adds a rule. It stops on plugin unload. An exclusive lock file `engine.lock` in the plugin data directory keeps two Hermes processes on one profile from opening the same camera; the second waits in standby.
- **Alert content.** Alert photos and captions are injected into the chat session, so they go to the agent's main model and, through `MEDIA:`, to the messaging platform you are using. Rule text and captions also go to the agent.
- **Gateway injection opt-in.** `allow_gateway_injection: true` lets this plugin inject messages into your chat sessions (outside the classic CLI, Hermes refuses it unless you opt in). Leave it off if you do not want that; alerts then cannot be delivered.
- **Local storage.** Rules, events, captions, snapshots, teaching examples, usage counters and chat routes are stored under `$HERMES_HOME/plugin-data/hermes-sentinel/`, as listed above. Photos may contain people and private spaces.
- **Telemetry.** None. No analytics, no update checks, no downloads of code.
- **Credentials.** None stored. Secrets are read through `agent.secret_scope.get_secret` only when a configured URL contains a `${NAME}` placeholder.
- **No shell commands**, and no writes outside the plugin data directory.

## Troubleshooting

| Symptom | Check |
|---|---|
| Anything does not work | Run `/sentinel setup`; it lists what is missing and the fix. |
| No alerts arrive | Run `/sentinel status`. If it shows a delivery problem, add `allow_gateway_injection: true` (see Required setup) and restart. On Telegram/Discord send `/sentinel here`. Check the rule is `watching`, not paused, and inside its schedule. |
| "Camera not found" or source offline | Run `sentinel_sources`. Another app may hold the webcam; try another index. On Linux check `/dev/video*` permissions. For streams, test the URL in a player; remember `${SECRET}` rules loaded at start-up cannot read secrets until resumed. |
| Too many false alarms | Reply "false alarm" to alerts (each one tightens the rule by 0.05). Raise `confidence_threshold`, set `confirm_k` 3 of 4, add a `roi`, add `no` examples with `sentinel_teach`, or rewrite the question to be more specific. |
| Budget paused | `/sentinel budget` shows usage and when capacity returns. Raise `vision_calls_per_hour` / `vision_calls_per_day`, lengthen `idle_check_seconds`, or narrow the ROI. |
| `/sentinel status` says "standing by" | Another Hermes process on this profile owns the cameras. This one takes over when it exits. |
| Dependency install fails | The plugin needs `numpy`, `opencv-python-headless` and `mss` (declared in `plugin.yaml`). Run `hermes plugins doctor hermes-sentinel` and install any missing package into Hermes' environment. Sentinel reports a missing package with an install hint instead of crashing. |
| Judge errors in status | The `sentinel_vision` model is unreachable or not vision-capable. Check the auxiliary task config. Sentinel backs off (5 s up to 5 min) and posts one notice per failure episode. |

## Limitations

- No audio, and no video-site streams such as YouTube. Only the source types listed above.
- Frames are judged one at a time; the model sees no motion. "Is someone walking" is answered from a single still.
- Rules that fire on the same frame are judged separately (one call per rule), not batched.
- The change gate catches onsets, not slow drift; slow state changes rely on the idle tick.
- Hermes only allows alert injection outside the classic CLI when `allow_gateway_injection` is set. Sentinel cannot check this ahead of time; it learns after the first failed delivery.
- Rules loaded at start-up that need `${SECRET}` URLs cannot read the secret scope until a tool call creates or resumes them.
- A rule from an older session in the same chat is matched to that chat only after you run `/sentinel` once in the new session.
- Local `/sentinel here` without a session id will not guess which local rule belongs to which session; pass a rule id.
- Vision model quality decides accuracy. Expect misses and false alarms; the feedback loop reduces them, it does not remove them.
- Desktop pane: needs a Hermes Desktop that hosts the plugin REST bridge and the agent in one process (see below); it is read-mostly, the agent tools remain the full interface.

## Development

From the repository root (needs [uv](https://docs.astral.sh/uv/)):

```
bash scripts/check.sh              # ruff, plugin tests, tool tests, rule_lint
bash scripts/check.sh --with-hermes   # also hermes plugins validate and doctor, in an isolated home
uv run --group dev pytest hermes-sentinel/tests
```

Tests never import Hermes. `hermes-sentinel/tests/harness/` provides a fake plugin context (`FakeCtx`), a scripted fake LLM, Hermes module shims and synthetic scenes; see its [README](tests/harness/README.md). `scripts/rule_lint.py` statically checks the catalog admission rules. Architecture notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contribution notes in the repository's `CONTRIBUTING.md`.

## License

MIT. See [LICENSE](LICENSE).
