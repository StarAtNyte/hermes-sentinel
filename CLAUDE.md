# Hermes Agent Plugin Hackathon — project rules

This project is an entry for the Nous Research Hermes Agent Plugin Hackathon.
Every plugin built here must satisfy the rules below. Check them before writing
code, before every commit, and again before opening the catalog PR.

## Hackathon facts

- Deadline: **2026-11-11, 5 PM EST**. Plan scope so a polished version exists by 2026-11-10.
- Entry = the plugin itself. Judged on **usefulness, reliability, polish, creativity**.
- Judges favour plugins that do more than a prompt or skill could. Use real
  integration surfaces (middleware, hooks, providers, Desktop UI, state), not prompt wrappers.
- To enter:
  1. PR adding `plugin-catalog/<name>.yaml` to `NousResearch/hermes-agent`
     (guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
  2. Demo video on X, **under 90 seconds**, showing the plugin working, tagging @NousResearch.
  3. Submit both links via https://form.typeform.com/to/FwJLvOln (allowed as soon as the PR is open).
- Developer guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins

## Plugin layout

- Native plugin: `plugin.yaml` + `__init__.py` with `register(ctx)`.
- Desktop UI: `desktop/plugin.js` (Desktop Plugin SDK only).
- Dashboard: `dashboard/manifest.json`, backend `dashboard/plugin_api.py`.
- Portable package: `plugin.json` + `skills/*/SKILL.md` + `mcp.json`.
- Tool handlers take `(args: dict, **kwargs)` and return a JSON string.
- Persistent data goes in the plugin's `plugin-data/` directory.

## Catalog admission rules (must all hold)

1. **Human-merged gate.** Listing only via a maintainer-merged PR to hermes-agent.
2. **Exact SHA pin.** Entry pins a full 40-character commit SHA. No branches, tags, short SHAs.
   Quote `version`.
3. **No self-updating code.** Never fetch or replace the plugin's own files (no update checkers,
   release downloaders, remote `plugin.js` loaders). Violations are declines, not fix requests.
4. **SHA bumps are new PRs**, re-reviewed over the commit range. Bump `version` in the same PR
   and re-pin screenshot/image URLs that embed the old SHA.
5. **Owner submits.** The PR must come from the repo owner or a major contributor.
6. **Declared capabilities match reality.** The `capabilities:` block (tools, hooks, middleware,
   env vars) must exactly match what the plugin registers at the pinned commit.
7. **Install security scan passes.** `dangerous` fails the entry; read and resolve every
   `caution` warning.
8. **Desktop plugins stay inside the SDK.** In `desktop/plugin.js`:
   - no prototype patching (`Storage.prototype.x =`, `Object.defineProperty(...prototype)`);
     a bundled library (e.g. three.js) setting fields on its own classes is fine;
   - no `eval` / `new Function`;
   - no `import()` of anything except `@hermes/plugin-sdk` and `react`;
   - no script-tag injection;
   - no reaching into app stores or markup (`data-slot`, `data-tour`, `data-sidebar`,
     `data-testid`, `[data-tree-*]`, `closest()`, `document.body` MutationObserver);
   - avoid raw `window.hermesDesktop` bridge calls.
   If the SDK lacks something, open an issue asking for a hook instead of patching around it.
9. **No runtime overrides of Hermes core.** Never replace, wrap or rebind core functions, methods,
   module attributes or private dicts (`AIAgent.x = ...`, `setattr(server, ...)`, `sys.modules[...]`).
   Avoid importing undocumented core modules and calling private methods (`db._execute_write`).
   Violations are declines.
10. **Dependency hygiene.** `python_dependencies` must resolve under Hermes core constraints.
    Use the oldest API-compatible floor **plus an upper bound** (no bare `>=X`). Prefer
    `uv --exclude-newer` release quarantine in CI.
11. **Credentials only through Hermes.** Read secrets with `agent.secret_scope.get_secret(NAME)`
    (declared in `requires_env` / `config_schema`), never bare `os.environ`. OAuth goes under
    `oauth:` in `plugin.yaml` with https-only URLs, used via `credentialed_provider_request`.
    Never read `.env`, `auth.json`, vendor CLI token files, browser profiles or keychains, and
    never refresh/write tokens.
12. **Respect approvals and unattended runs.** Never auto-approve, disable guards, or spawn
    children inheriting YOLO / non-interactive mode. Anything waiting on a human must fail
    cleanly or time out under cron, the messaging gateway, and other unattended runs.
13. **Disclose risky behaviour** in the PR description and README: third-party network calls,
    reads outside plugin data, shell commands, long-running background processes, stored
    credentials. Telemetry must be opt-in.
14. **Truthful compatibility metadata.** `requires_hermes` is a SemVer floor (e.g. `">=0.21.5"`),
    never CalVer, never newer than the current release. Declare the same floor in both
    `plugin.yaml` and the catalog entry. `version` matches the pinned code.
15. **No skins or forks of bundled plugins**, no vendor-lookalike branding.
16. **One listing per lineage.** Do not rebrand or lightly modify an existing listed plugin.
    Check the marketplace for duplicates before building.
17. **No unscannable payloads.** No compiled binaries (ELF, Mach-O, PE, WASM), no `.pyc`/`.pyo`,
    no archives hiding non-source. Native components only via public GitHub Actions builds with
    build attestation; setup-time downloads only from official upstream, checksum-pinned, disclosed.
18. **Password-manager login backends** are vendor/official tier only. Do not build one.

## Workflow checklist

Before each commit:
- [ ] No core patching, no SDK escapes, no self-update, no binaries/bytecode (rules 3, 8, 9, 17).
- [ ] Secrets only via `secret_scope.get_secret()` (rule 11).
- [ ] `capabilities:` in `plugin.yaml` matches what `register(ctx)` registers (rule 6).
- [ ] Unattended runs never block on human input (rule 12).
- [ ] `.gitignore` excludes `__pycache__/`, `*.pyc`, build output.

Before opening the catalog PR:
- [ ] Repo is public on GitHub/GitLab (https clone URL).
- [ ] `hermes plugins validate /path/to/plugin --install-deps` passes; every warning read and addressed.
- [ ] `hermes plugins doctor <path> --ci` passes.
- [ ] Dependencies have floors and upper bounds (rule 10).
- [ ] `requires_hermes` SemVer floor set in both `plugin.yaml` and the entry (rule 14).
- [ ] README documents features, setup and all disclosures (rule 13); screenshots for any UI.
- [ ] Entry file `plugin-catalog/<name>.yaml`: full 40-char `sha`, quoted `version`, `subdir` if monorepo.
- [ ] PR description: what it does, which Hermes surfaces it uses, all rule 13 disclosures.
- [ ] Wait for catalog CI to go green.

Before the deadline:
- [ ] Demo video under 90 seconds showing the plugin working, posted on X tagging @NousResearch.
- [ ] Typeform submitted with the PR link and the video link.

## Real Hermes is off-limits by default

- Do not run the Hermes CLI (`hermes ...`, `python3 ~/.hermes/hermes-agent/hermes ...`) in this project.
  A bare run once finished a pending update on the user's real `~/.hermes` install and synced skills.
- `scripts/check.sh` skips the Hermes steps (`plugins validate`, `plugins doctor`) unless `--with-hermes`
  is passed, and then runs them only with an isolated `HERMES_HOME` (never inside `~/.hermes`).
- Never enable the plugin in the user's real Hermes, and never write to `~/.hermes`.
  A live test needs a separate profile/home and the user's explicit go-ahead.
- Hermes source under `~/.hermes/hermes-agent` is read-only reference.

## Debugging

- `HERMES_PLUGINS_DEBUG=1 hermes plugins list` shows verbose discovery logs.
- `hermes logs --level WARNING` shows plugin failures.
- A crash in `register()` disables only that plugin; a failing hook is logged and skipped.
