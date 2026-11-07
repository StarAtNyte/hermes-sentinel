# Demo video: 90 seconds, hard cap 85 s final cut

One story: watch a mug, a hand takes it, the phone buzzes with a photo. Then the Desktop pane and a plain-English history question. Nothing is sped up except waiting; show that it is real.

## Setup checklist (do the day before, rehearse twice)

**Separate Hermes home.** Record in a throwaway profile, not your real `~/.hermes`.
```
hermes profile create sentinel-demo
hermes -p sentinel-demo gateway setup        # Telegram bot token + your user id
```
Install the pinned build into that profile (`hermes -p sentinel-demo plugins install hermes-sentinel` once merged; before that, `scripts/dev_install.sh --home <profile home> --apply`), then `hermes -p sentinel-demo plugins enable hermes-sentinel`. Start the gateway (`hermes -p sentinel-demo gateway start`) so Telegram works.

**Config** (`<profile home>/config.yaml`):
```yaml
plugins:
  entries:
    hermes-sentinel:
      allow_gateway_injection: true
      settings:
        default_source: camera:0
        vision_calls_per_hour: 60
        confirm_k: 2
        confirm_n: 3
auxiliary:
  sentinel_vision:
    provider: <your provider>
    model: <a fast vision-capable model>
  sentinel_rules:
    provider: <your provider>
    model: <a small text model>
```
Pick the fastest vision model that reads a mug on a desk reliably; test with `sentinel_look` first. Cloud latency is the main variable in the alert time.

**Physical.**
- Webcam fixed on a tripod or monitor mount, framing desk plus mug, nothing else moving in frame. Check `sentinel_sources` lists `camera:0`.
- One mug, plain colour, contrasting with the desk. Good steady light; no flicker, no window glare changing.
- Close other apps that hold the webcam. Turn off auto-exposure shifts if you can.
- Phone on a stand with Telegram open on the demo bot chat, notifications on, screen recording on the phone (or film the phone with a second camera).
- Blur or crop anything private in the room, notifications from other apps, bot token, and the Hermes home path.

**Delivery check.** In the Telegram chat run `/sentinel setup --test-delivery`; if it reports blocked, add the `allow_gateway_injection` YAML it prints and restart.

**Dry run.** Create the rule, take the mug, confirm the alert arrives, send "false alarm" once, then delete the rule and `/sentinel list` is empty. Time the alert: if it is over 15 s, change the model or lower latency before filming.

**Pre-warm for the take.** Run `/sentinel status` once in the Telegram chat and send `/sentinel here` there, so the route is known. Keep the Desktop app open on the Sentinel pane in a second window.

## Shot list (timestamps are of the final video)

| Time | Screen | Action and exact text |
|---|---|---|
| 0:00-0:05 | Title card or terminal, large font | Text: "Hermes Sentinel: give Hermes eyes". Voice or caption: "Tell Hermes what to watch. It tells you when it happens." |
| 0:05-0:17 | Hermes chat (CLI or Telegram) with the webcam view in a corner | Type: `watch my webcam and tell me if someone takes my coffee mug`. Hermes replies that it is watching `camera:0` with the check cap. Cut after the reply. |
| 0:17-0:27 | Webcam view, mug on desk | Show the empty scene. Voiceover: "Checks happen only when something moves; a quiet desk costs nothing." (Show `/sentinel status` for two seconds: gate running, vision calls used 1 of 60.) |
| 0:27-0:42 | Split: webcam + phone | A hand takes the mug. Cut to the phone: Telegram alert with the photo and a caption such as "the mug was picked up by a person ... (confidence 0.9x)". Keep real time here if under 15 s; if the wait is longer, cut at most 8 s and show an on-screen "8 s skipped". |
| 0:42-0:52 | Telegram | Type: `false alarm, that was me`. Reply: event marked, the photo is now a "should not trigger" example and the rule is stricter. |
| 0:52-1:07 | Hermes Desktop, Sentinel pane | Show source tile with the live frame, the rule with its pause button, the event timeline with the thumbnail and the correct / false-alarm buttons. Click pause then resume. |
| 1:07-1:17 | Chat | Type: `what happened at my desk today?`. Reply lists the events with times. |
| 1:17-1:25 | Terminal or text card | Text: "Local change gate. Vision model only on changed frames. Budget caps. No telemetry. MIT." and the repo/PR URL. End card with `@NousResearch`. |

Total 85 s. Do not add music with lyrics. Captions on all speech; most viewers watch muted.

## Prompts to type (exact)

1. `watch my webcam and tell me if someone takes my coffee mug`
2. `false alarm, that was me`
3. `what happened at my desk today?`

Optional cut-in if time remains (do not extend past 90 s): `/sentinel budget`.

## Fallback if a live alert is slow or missing

- **First, before filming:** keep the rule's cooldown short while recording by asking for it in the request ("... cooldown 30 seconds"), so a retake does not wait 5 minutes.
- **Slow (over 15 s):** film three takes and use the fastest; the gate needs a change plus 2 positive looks, spaced about 2 s, so 6 to 10 s of model latency is normal. A steadier, closer mug and a faster model both help. Keep your hand in view for 2 to 3 seconds when lifting the mug, not a flick.
- **No alert at all:** run `/sentinel status`. A delivery warning means `allow_gateway_injection` is missing or `/sentinel here` was not sent in the Telegram chat. A judge error means the `sentinel_vision` model is not vision-capable. Use `/sentinel test <rule_id>` to see what the judge says about the current frame.
- **Backup content:** record the working take plus a B-roll of the Desktop pane and a screen recording of the history question; if live capture fails on the day, use the recorded Telegram clip and say nothing false about timing (the on-screen "skipped" marker stays).
- **Backup source:** `folder:/path` with images dropped in by hand is deterministic, but say so on screen; prefer the webcam for the main take.
- **Reliable fallback for the live take: a `file:` loop.** Record the 10 to 15 s webcam clip once (empty desk for about 4 s, a hand takes the mug, a few more seconds) and keep it outside the repo, for example `~/demo/mug.mp4`. Then ask: `watch file:/home/<you>/demo/mug.mp4?loop=true&speed=15 and tell me if someone takes my coffee mug, cooldown 30 seconds`. Sentinel reads about 2 frames per second whatever the clip's rate, so use `speed=` of fps divided by 2 (15 for 30 fps, 30 for 60 fps) to play it in real time. The loop restarts the clip, so the mug reappears; a trigger rule latches after an alert and needs a confirmed opposite verdict before it can fire again, so rehearse how many passes it takes and film the take that fires cleanly. The same chat, alerts, Desktop pane and history work as with a camera. Rehearse it: check with `/sentinel status` that the source is `watching`, and use `/sentinel test <rule_id>` once to see the judge's view of the current frame.
- **Be honest about it.** Say "recorded clip" on screen (a small caption such as `file: source, looped recording`) if the take uses it; do not present it as live. Use the live webcam for the main take when it works, and the file source for the backup take and for retakes.
- **Dry-run without a camera or model cost:** point `file:` at the clip, run `/sentinel setup`, then `/sentinel setup --test-delivery` in the Telegram chat before filming to prove the alert path (it sends one small message and starts one agent turn; it is never automatic).

## Recording tips

- 1080p, 30 fps, screen recorder at native resolution; terminal font 18 pt or larger; Desktop in light or dark, whichever contrasts better with the webcam preview.
- Record the webcam view, the phone, and the screen as separate clips, then cut; do not rely on one long take.
- Show timestamps or a visible clock in at least one shot so the alert delay is honest.
- Export under 90 s, check the length in the posted X player, and keep a captioned version. Upload natively to X (do not link out to a hosting site).
- Avoid on-screen secrets: bot token, user id, Hermes home path, API keys, other windows.

## Caption and hashtags

Caption: "Hermes Sentinel: tell Hermes in plain English what to watch on a camera or your screen. A local change gate keeps quiet scenes free; a vision model only sees changed frames; you get an alert with a photo. Plugin + Desktop pane, MIT. PR: <PR URL>"

Hashtags: #HermesAgent #NousResearch #AIAgents #OpenSource

## X post text (under 280 characters)

```
Hermes Sentinel: tell Hermes in plain English what to watch ("tell me if someone takes my mug") and it alerts you with a photo. Local change gate, vision model only on changed frames. @NousResearch plugin hackathon entry. PR: <PR URL> #HermesAgent
```

Count after replacing `<PR URL>`: a link counts as 23 characters on X. The text above is 262 characters with the link counted as 23; check in the composer.
