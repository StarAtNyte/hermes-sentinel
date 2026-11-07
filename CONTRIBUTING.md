# Contributing

## Setup

Install [uv](https://docs.astral.sh/uv/), then from the repository root:

```
uv sync --group dev
bash scripts/check.sh
```

`check.sh` runs `ruff check`, `ruff format --check`, the plugin tests (`hermes-sentinel/tests`), the tool tests (`tests_tools`), `scripts/rule_lint.py`, a strict ruff pass (`ruff check --config ruff.strict.toml .`: docstrings, naming, complexity) and `scripts/structure_lint.py`. The last two fail on any finding, and CI runs the same steps. Format with `uv run --group dev ruff format .`. All code meets the Code standard in `CLAUDE.md`: modules at most 400 lines, classes 250, functions 40, nesting 3, at most 8 modules per package, docstrings and type hints on public names, `__init__.py` files that only re-export. Line length is 100; the base ruff rules are E, F, W, I, B, UP, SIM, and the strict pass adds N, D, C901, RET and RSE. `check.sh` must pass before a change is merged.

## No real Hermes in tests or checks

- Tests use the harness in `hermes-sentinel/tests/harness/` (fake context, fake LLM, module shims). They never import Hermes.
- Do not run the `hermes` CLI against `~/.hermes` while developing. `check.sh` skips the Hermes steps by default; `--with-hermes` runs `plugins validate` and `plugins doctor` only in an isolated `HERMES_HOME` under `~/.cache/hermes-sentinel-dev/`.
- Treat `~/.hermes/hermes-agent` as read-only reference. Use public plugin APIs (`ctx.*`) only.

## Catalog rules (summary)

The full list is in [CLAUDE.md](CLAUDE.md). The ones that shape code:

- No self-updating code, no downloads of code, no binaries or bytecode in the repo.
- No patching of Hermes core and no private Hermes internals; no `eval`/`exec`.
- Secrets only through `agent.secret_scope.get_secret`; never read `os.environ`, `.env` or token files.
- `capabilities` and `provides_tools` in `plugin.yaml` must match what `register(ctx)` registers (tested).
- Dependencies need a floor and an upper bound. `requires_hermes` is a SemVer floor.
- Nothing may wait on a human; unattended runs must fail cleanly.
- Risky behaviour (network, capture, background threads, storage) must be disclosed in the README.

## Style

- Tool handlers take `(args, **kwargs)` and return a JSON string; they never raise.
- Every external failure is caught, logged, surfaced once and retried with backoff.
- Add a test with every behaviour change. Keep docs in step with the code; claims in the README must be true of the current code.
- Never commit generated media. Test scenes are synthesised at test time.
