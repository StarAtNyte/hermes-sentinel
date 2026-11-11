# nous

Entry for the Nous Research Hermes Agent Plugin Hackathon (deadline 2026-11-11, 5 PM EST).

This repository holds **sentinel-vision**, a Hermes Agent plugin that watches cameras or the screen for events you describe in plain English and alerts you, with a photo, when they happen. A local change gate keeps cost near zero on quiet scenes; only changed frames reach your vision model.

Start with the plugin README: [sentinel-vision/README.md](sentinel-vision/README.md). It covers install, required setup, usage, configuration, privacy and the catalog disclosures.

## Repository layout

```
sentinel-vision/            the plugin (plugin.yaml, __init__.py, sentinel/)
  sentinel/                 sources, gate, judge, rules, engine, alerts, store, budget, tools, commands
  tests/                    plugin tests and the Hermes-free test harness (tests/harness/)
  docs/                     RECIPES.md, ARCHITECTURE.md
  README.md CHANGELOG.md LICENSE
tests_tools/                tests for scripts/rule_lint.py
scripts/
  check.sh                  local quality gate
  dev_install.sh            link the plugin into a Hermes home (dry run by default)
  rule_lint.py              static check of the catalog admission rules
DESIGN.md                   original design notes (the code is authoritative)
CLAUDE.md                   hackathon and catalog rules for this project
CONTRIBUTING.md
```

## Development

Needs [uv](https://docs.astral.sh/uv/).

```
bash scripts/check.sh                 # ruff, plugin pytest, tool pytest, rule_lint
bash scripts/check.sh --with-hermes   # adds hermes plugins validate and doctor, isolated home only
```

The default run never touches a real Hermes install. Tests use a fake plugin context and never import Hermes. See [CONTRIBUTING.md](CONTRIBUTING.md) and [sentinel-vision/docs/ARCHITECTURE.md](sentinel-vision/docs/ARCHITECTURE.md).

## License

MIT. See [sentinel-vision/LICENSE](sentinel-vision/LICENSE).
