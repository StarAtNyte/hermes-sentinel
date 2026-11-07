# Skeptical judge review: hermes-sentinel

Reviewed cold, in judge order (PR body, README, demo script, LIVE_TEST, catalog entry, plugin.yaml, code layout), on 2026-11-07. Scores are for the project as it stood before the doc fixes listed at the end.

## Scores

| Criterion | Score | Evidence |
|---|---|---|
| Usefulness | 8 | Clear, relatable job (watch a mug, a door, a loss curve, get a photo alert, ask history later). Real cost story: free local gate, k-of-n, hard hourly/daily caps, per-rule pacing. Held back by: the useful surfaces (webcam, Telegram, Desktop) are the unverified ones, and the "slow drift", "second opinion" and "batching" extras add surface area before the core is proven on real footage. |
| Reliability | 6 | 1457 plugin + 195 tool tests, strict ruff, structure lint, install scan safe, doctor ok. But the only live evidence is ONE run on a synthetic generated video through the classic CLI, where the alert turn was not even rendered live. A real-webcam run found a cost bug (about 6 calls/minute) and a second one (24 calls in 4.5 minutes while latched); both fixes are verified only by simulated clocks. Retest pending. "Not a safety device" is honest but a judge will weigh it. |
| Polish | 7 | Excellent engineering hygiene, setup check command, honest limitations, delivery self-test. Pulled down by README length (about 390 lines, 40 KB), jargon in the opening (gate, k-of-n, ROI, latched), a mandatory `allow_gateway_injection` YAML step that silently breaks alerts if missed, no real screenshot or GIF of an alert in Telegram/Desktop (the Desktop shot is a stub-host render), and a 1,600-character catalog description. |
| Creativity | 7 | Idea (eyes for an agent) is not new, but the execution (change gate in front of a vision model, feedback that tunes thresholds, event memory the agent can query, `file:` source for repeatable demos) is thoughtful. Uses real surfaces: 9 tools, command, two auxiliary tasks, `ctx.llm`, `inject_message`, `on_unload`, dashboard + Desktop pane. No hooks or middleware, which is fine. |

## Top 10 things that lose points

1. First screen buried the pitch: the old README opened with a one-line sentence then a dense "Verified live" paragraph, not a copy-paste path to seeing it work. (Fixed: one-line pitch, 6-step try-it.)
2. Nothing the judge can see working end to end on the surfaces they will use. Evidence is classic-CLI + synthetic video; Desktop, Telegram and the live webcam are explicitly untested. The video is the single biggest risk.
3. The first-run path has a silent failure: without `allow_gateway_injection: true` alerts are refused and Hermes gives the plugin no way to read the setting. Needs a vision-capable model routed too. Two config edits before the first win. `/sentinel setup` mitigates; it is still friction.
4. Jargon without a gloss in the first screens: "change gate", "k-of-n", "ROI", "latched", "slow-drift", "second opinion", "batching". A five-minute judge will not read the "How it works" diagram.
5. README is a reference manual: 21-row config table, 8 source variants, 18 limitation bullets, a full disclosure section duplicated in the PR. (Fixed partly: config table moved to docs/CONFIGURATION.md; the rest should move next.)
6. Claims that sound unverified: "Alert text built to resist prompt injection", "reliable", the illustrative chat transcript (event #14, grey hoodie) that looked like a real recording. (Fixed: transcript labelled illustrative.)
7. Inconsistent numbers: PR body said 1422 plugin tests, LIVE_TEST said 1430, collection shows 1457 (plugin) + 195 (tools). (Fixed to 1457; the 1430 `--with-hermes` run is labelled as an earlier commit and must be re-run on the pin.)
8. Placeholders still in the PR body and catalog entry (`<FILL_40_CHAR_SHA>`, validate/doctor output, three screenshot URLs that point at files that do not exist in `docs/` yet: `docs/screenshots/*.png`). A judge who opens the catalog entry sees broken image links today.
9. Demo script is a 85 s plan with every segment depending on an untested surface and with only a vague fallback. (Fixed: added status section, decision rule, hook, caption and cut guidance.)
10. Cost-tuning history is visible in the docs (two cost bugs found live, fixes covered only by simulation). Honest, but a reader may conclude the live behaviour is still unproven. A fresh clean webcam run with a visible `/sentinel budget` would turn this into a strength.

## Ranked fix list

1. Record and verify one real run on Telegram (or Desktop) with a photo alert; capture a real screenshot into `docs/screenshots/` (names must match the catalog entry). HIGH value, needs the user.
2. Re-run the webcam hand-raise test on the fixed code; paste the `/sentinel budget` result into LIVE_TEST.
3. Re-run `check.sh --with-hermes` on the final pin and fill the PR placeholders with real output.
4. Decide on the Desktop pane: verify in the real app, or remove it from the demo and soften the claim in the PR/catalog description.
5. Cut the catalog `description` to about 3 sentences; keep the disclosure in the PR and README (rule 13 text there is exact).
6. Move Sources, Cost control, Privacy/Disclosure detail into docs/ and leave one-paragraph summaries with links in the README.
7. Consider making the injection prerequisite less brittle in the UX (e.g. `/sentinel setup` as the first thing the agent suggests after install). Code change, out of scope here.
8. Trim "Features" to 8 bullets; the extras (drift, batching, second opinion) belong under "How it works".
9. Add a 10-second GIF of the alert to the top of the README once item 1 exists.
10. Delete or archive DESIGN.md before submission (release-checklist already flags it).

## High-risk and code-level issues (not fixed)

- `hermes-sentinel/dashboard/plugin_api.py:4,37` and `hermes-sentinel/sentinel/dashboard_api/common/access.py:45`: scan `sys.modules` (read-only) and `importlib.import_module` the plugin package by name. Rule 9 text says "never replace, wrap or rebind ... `sys.modules[...]`"; this only reads it, but a strict reviewer or scanner may still flag it. Commit 033672a moved it, but it remains in the PR path.
- Broad `except Exception` with `# noqa: BLE001` in `sentinel/runtime.py:162,180,216,223,242,247`, `sentinel/vision/judge/judge.py:122,144,149,163,180,194,212,217,232,246`, `sentinel/vision/imaging.py:37,130`, `sentinel/config.py:206`, `sentinel/vision/judge/blocks.py:19`. The Code standard says catch the narrowest exception; most log, but `runtime.py:162` ("keep routes in memory only") and `blocks.py:19` swallow without surfacing. About 18 noqa suppressions contradict "over a limit means split it, not add a noqa" in spirit.
- `sentinel/runtime.py:49` `ALERTS_PER_HOUR = 20` is hard-coded, not a setting, and `sentinel/agent/tools/management/watch.py:40` `MAX_RULES = 50` and `registry.py:28` `COMPILE_CALLS_PER_HOUR = 10` are also fixed; documented in README, so only a note.
- Classic-CLI alert not rendered live (LIVE_TEST "Observations"): not a plugin bug as far as known, but it means no judge-visible success on the surface used for the only live test.
- `__pycache__/` directories exist in `hermes-sentinel/`, `hermes-sentinel/sentinel/`, `hermes-sentinel/dashboard/` and `hermes-sentinel/tests/` on disk; confirm `.gitignore` keeps them out of the pinned commit (rule 17).

## What I changed (docs only)

- `hermes-sentinel/README.md`: one-line pitch at the top, a 6-step "Try it in 2 minutes" using a `file:` video and `/sentinel setup`, a "Verified live, and what is not" section stating plainly that the real Desktop app, Telegram and the webcam retest are unverified, the illustrative transcript labelled as such, the CLI-rendering caveat next to the surfaces claim, and the 21-row configuration table moved to `hermes-sentinel/docs/CONFIGURATION.md` with a short summary and link.
- `submission/pr-body.md`: opens with the value and the live-test evidence, with the unverified list; test counts corrected (1457 plugin, 195 tool); the unverified list in the evidence section expanded; rule 13 disclosure text untouched.
- `submission/demo-script.md`: new status section (verified vs. not), a decision rule for the Desktop segment, a 5 s hook, caption and cut guidance, slow-alert policy.
- Counts verified against code: 9 tools, 21 settings plus 4 secret slots, defaults 60/500/2 of 3/0.7/7 days/500 MB, 6 second opinions per hour, 20 alerts per hour, 50 rules, 10 compiles per hour, `requires_hermes >=0.22.5` in plugin.yaml, entry and PR.
