# Contributing

## Setup

Install [uv](https://docs.astral.sh/uv/), then from the repository root:

```
uv sync --group dev
bash scripts/check.sh
```

`check.sh` runs `ruff check`, `ruff format --check`, the plugin tests (`hermes-sentinel/tests`), the tool tests (`tests_tools`) and `scripts/rule_lint.py`, then prints a non-failing `scripts/structure_lint.py --report` summary. Format with `uv run --group dev ruff format .`. New or touched code must meet the Code standard in `CLAUDE.md`; `sources/`, `storage/` and `rules/` (and their tests) must stay free of structure_lint findings. The modules still to be split are listed under "Remaining refactor" in `hermes-sentinel/docs/ARCHITECTURE.md`. Line length is 100; ruff rules E, F, W, I, B, UP, SIM. `check.sh` must pass before a change is merged.

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
