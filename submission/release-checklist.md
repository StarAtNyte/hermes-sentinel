# Release checklist (owner, due Wed 2026-11-11 17:00 EST)

Aim to have the PR open by Mon 2026-11-10 evening. The Typeform is allowed as soon as the PR is open, so a CI fix after that costs nothing. Hermes steps below run only in an isolated `HERMES_HOME`, never `~/.hermes`.

## 1. Freeze the code (Mon 2026-11-10)
1. Merge the api, ui and kit work. `bash scripts/check.sh` passes (ruff, pytest, rule_lint).
2. Re-check `plugin.yaml` against `register(ctx)`: `provides_tools` lists the 9 tools, `requires_hermes: ">=0.22.5"`, `version: "0.1.0"` (also `__version__` in `sentinel/version.py`, CHANGELOG, entry).
3. README: remove "There is no Desktop pane in this release" and "Desktop UI pane: not included" (the pane now exists); document the pane and add screenshots. Confirm the Disclosure section mentions the dashboard API.
4. Add the screenshots at `hermes-sentinel/docs/screenshots/` (alert-telegram.png, desktop-pane.png, sentinel-status.png; PNG/JPEG only, text-sized, no faces or private rooms). Reference them in the README with relative paths.
5. Confirm no `__pycache__`, `.pyc`, binaries or archives are tracked: `git ls-files | grep -E '\.(pyc|pyo|so|dll|dylib|exe|wasm|zip|tar|gz)$'` prints nothing.

## 2. Decide what the public repo contains
The catalog clones the whole repo at the SHA and the scanner reads the `hermes-sentinel/` subdir. Recommendation:
- Keep: `hermes-sentinel/` (plugin, tests, docs), `README.md`, `LICENSE`, `scripts/check.sh`, `scripts/rule_lint.py`, `tests_tools/`, `pyproject.toml`, `uv.lock` (they let a reviewer reproduce the gate).
- Remove `CLAUDE.md` (a private agent-rules file, not useful to users) or keep it only if you are happy to publish it; it contains no secrets but references local paths and the real-Hermes warning.
- `DESIGN.md`: README says the code is authoritative over it. Either delete it or keep it as history; if kept, add a note at the top that the shipped plugin differs (the Desktop pane and Telegram steps changed, for example).
- `submission/`: delete from the pinned commit or keep; it holds the PR body and demo script, which are harmless but add noise. Cleaner: move it to a private location before pinning. Do not leave `<FILL_...>` placeholders in anything the README links to.
- Check that the root `README.md` links (DESIGN.md, CONTRIBUTING.md, CLAUDE.md) still resolve after removals.

Do removals in a commit before choosing the SHA (the SHA must be the last commit you want reviewed).

## 3. Choose the SHA
1. `git status` clean, `git log -1`, push to the default branch of `https://github.com/StarAtNyte/hermes-sentinel`.
2. `git rev-parse HEAD` gives the 40-character SHA. Short SHAs, branches and tags are rejected. Use this exact value everywhere in the entry (sha, docs_url, screenshots).
3. Do not push further commits to the pinned content without a new SHA and a re-run of steps 4 to 6.

## 4. Make the repo public (rule 5: owner submits)
1. GitHub Settings > Danger Zone > Change visibility > Public. MIT licence file is at `hermes-sentinel/LICENSE`; consider a copy at the root.
2. Verify from a logged-out shell: `git ls-remote https://github.com/StarAtNyte/hermes-sentinel HEAD` and open `https://raw.githubusercontent.com/StarAtNyte/hermes-sentinel/<SHA>/hermes-sentinel/docs/screenshots/desktop-pane.png`.
3. Search the history for secrets before it goes public (`git log -p | grep -iE 'api[_-]?key|token|password|secret'`).

## 5. Validate in an isolated home
Use a scratch home, not `~/.hermes`. Either `bash scripts/check.sh --with-hermes` (it uses `~/.cache/hermes-sentinel-dev/hermes-home`, first run builds a private runtime in about 2-3 minutes) or by hand:
```
export HERMES_HOME="$(mktemp -d)"
git clone https://github.com/StarAtNyte/hermes-sentinel /tmp/sentinel-pinned && git -C /tmp/sentinel-pinned checkout <SHA>
hermes plugins validate /tmp/sentinel-pinned/hermes-sentinel --install-deps
hermes plugins doctor /tmp/sentinel-pinned/hermes-sentinel --ci
```
Read every warning (security scan `caution`, undocumented core imports, desktop surface). Fix or explain each in the PR body. Do this against the pinned checkout, not the working tree. Do not run these commands in this project's default workflow; they are for this step only.

## 6. Live test on the pinned build
In a separate profile or home with a vision model set for `sentinel_vision` (see demo-script.md setup): CLI alert, Telegram alert with photo, `/sentinel status`, Desktop pane tiles/rules/events, then `/sentinel pause` and a clean exit. Record the Hermes version used for the PR body.

## 7. Fill in the entry
1. Copy `submission/catalog-entry.yaml` and replace every `<FILL_40_CHAR_SHA>`; delete the comment lines at the top.
2. Confirm `capabilities.provides_tools` equals `plugin.yaml` `provides_tools`. Confirm `requires_hermes` is the same string in both and not newer than the current Hermes release.
3. Check `ls plugin-catalog | grep -i -E 'sentinel|camera|vision|watch'` in your fork for duplicates (rule 16) and state the result in the PR.
   Last checked 2026-11-07 against a catalog of 1,052 entries: no plugin watches cameras or screens from plain-English rules with a change gate, a vision judge and alerts. Nearest neighbours: `hermes-desktop-camera` (a composer camera button), `hermes-ros` (grabs frames from ROS robots), `hermes-monitor` (kanban worker health), `hermes-vision-inline` (attaches images to the session model). Re-run the check on the day you open the PR, since the catalog grows daily.

## 8. Fork, add the file, open the PR
```
gh repo fork NousResearch/hermes-agent --clone=false
git clone https://github.com/StarAtNyte/hermes-agent && cd hermes-agent
git checkout -b add-hermes-sentinel
cp <path>/hermes-sentinel.yaml plugin-catalog/hermes-sentinel.yaml   # exactly one file
git add plugin-catalog/hermes-sentinel.yaml && git commit -m "plugin-catalog: add hermes-sentinel"
git push -u origin add-hermes-sentinel
gh pr create --repo NousResearch/hermes-agent --title "Add hermes-sentinel" --body-file <path>/pr-body.md
```
Fill every `<FILL...>` in `pr-body.md` first and tick the checklist honestly. Do not add Claude/Anthropic attribution to the commit or PR.

## 9. Wait for CI
The catalog job clones the repo at the pinned SHA and runs `hermes plugins validate`. If it fails because of the plugin, you must push a fix to your repo, get a new SHA, and update `sha`, `docs_url` and screenshot URLs in the PR (rule 4 spirit: the pin is the review). Check the PR for maintainer comments daily until merged.

## 10. Demo video and post
Record and edit per `demo-script.md` (under 90 seconds). Post on X tagging @NousResearch with the PR link. Confirm the length in the posted player.

## 11. Submit the form
https://form.typeform.com/to/FwJLvOln with the PR URL and the X video URL. Allowed as soon as the PR is open; do it before 17:00 EST on 2026-11-11 (that is 22:00 UTC; check your local time zone). Keep a screenshot of the confirmation.

## 12. After submitting
Leave the pinned commit alone. Further work goes on a branch; a SHA bump is a new PR with `version` raised and screenshot URLs re-pinned.
