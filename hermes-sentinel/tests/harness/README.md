# Test harness: run the plugin without Hermes

Nothing here imports Hermes. Run from the project root: `uv run --group dev pytest`.

## Module identity

`plugin_loader` imports the plugin as `hermes_plugins.hermes_sentinel`, the name Hermes' loader uses. Reach plugin code through `plugin_module("sentinel.sources")`, never as a top-level `sentinel` package (pytest's `pythonpath` does not include the plugin dir). With one import path, exception classes are the same objects in the plugin, the harness and the tests. `import_plugin_fresh()` re-imports the package like a force reload; it creates new class objects, so restore `sys.modules` afterwards (see `test_plugin_loader.py`).

## Hermes module shims (`hermes_shims`)

Hermes exposes storage and secrets as module functions. The shims install once per session as stable module objects, so a plugin's import-time `from agent.secret_scope import get_secret` keeps working across tests:

- `plugins.plugin_storage.plugin_data_dir(name)` / `plugin_db(name, filename)`: same name and filename validation as Hermes. The DB opens with WAL, `foreign_keys=ON`, a busy timeout and `check_same_thread=False`.
- `agent.secret_scope.get_secret(name, default)` / `get_secret_str`: reads only the bound `secrets` dict, never `os.environ`. Every read is recorded.
- `agent.plugin_llm`: the data classes and `PluginLlmTrustError` from `llm_types`.

Each test binds a Hermes home and a secrets dict with `hermes_shims.bind(...)`; the fixtures do this for you.

## Host vocabulary (`hermes_surface`)

`VALID_HOOKS`, `VALID_MIDDLEWARE`, built-in slash commands, built-in aux tasks and built-in tool names are parsed with `ast` from the read-only Hermes checkout (`$HERMES_AGENT_DIR`, default `~/.hermes/hermes-agent`). If there is no checkout (CI), a pinned `SNAPSHOT` is used. `test_hermes_surface.py` fails when the snapshot drifts. Regenerate it with `uv run --group dev python hermes-sentinel/tests/harness/hermes_surface.py`.

## Fixtures (`tests/conftest.py`)

- `plugin_data_root`: a temporary Hermes home. It also sets `HERMES_HOME`.
- `secrets`: dict backing `get_secret`. Override this fixture in a test module to seed values.
- `shim_binding`: binds the shims. `.secrets_read` lists the secrets read so far.
- `fake_ctx`: a fresh `FakeCtx`.
- `plugin_data_dir`: `<tmp>/plugin-data/hermes-sentinel`, created through `plugins.plugin_storage`.
- `loaded_plugin`: the session-loaded package with `register(fake_ctx)` already called. Use `.module` and `.ctx`.

## FakeCtx: checks it enforces (same as `PluginContext`)

- `register_tool`: a bad schema raises `ValueError`. `override=True` without the `tools.override` capability raises `PluginToolOverrideError`. Shadowing a built-in tool returns `None` and records a warning.
- `register_command`: an empty name, or a name that clashes with a built-in command, is skipped (returns `None` and records a warning).
- `register_hook` / `register_middleware`: an unknown name is stored with a warning.
- `register_auxiliary_task`: rejects invalid keys and reserved keys, and ignores a bad `inherit_from` with a warning.
- `get_config` / `set_config`: keys are plugin-relative. Reserved roots, traversal and malformed keys raise `ValueError`. Reads are recorded in `ctx.config_reads`.
- `has_capability`: true for `granted_capabilities` or the legacy `allow_*` keys in `entry`.
- `ask_choice`: returns the next scripted entry from `ctx.choice_answers`. With nothing scripted it returns `None`, which is how an unattended run behaves.
- `post_message`: never raises. With `ctx.post_ok = False` it returns `ok=False` (no conversation).
- `spawn_task`: needs a running loop and raises `RuntimeError` otherwise, like Hermes. To drive the harness loop, use `ctx.call_in_loop(fn, ...)` (runs sync code inside the loop), `ctx.run(coro)` and `ctx.run_until_idle()`.
- `unload()`: runs `on_unload` callbacks in reverse order, keeps their errors in `ctx.unload_errors`, and cancels tasks.
- Warnings go to `ctx.warnings`. `test_register.py` asserts that list is empty.

Constructor knobs: `config` (settings), `entry` (the rest of `plugins.entries.<id>`: `allow_tool_override`, `granted_capabilities`, `llm: {...}`), `source` (`"bundled"` is trusted for tool overrides) and `host` (a `HermesSurface`).

## FakeLlm (`ctx.llm`)

Checks run in the same order as `agent/plugin_llm.py`:

1. Arguments: empty `instructions` or `input` raises `ValueError`.
2. Trust: a `task` the plugin did not register raises `PluginLlmTrustError`, as does a built-in task without `llm.allow_task_override`. So do `provider`, `model`, `agent_id` and `profile` overrides unless `entry["llm"]` allows them (allowlists included).
3. Input shape: unknown blocks raise `ValueError`, as does image `data` that is not `bytes` or `bytearray`.
4. The scripted response is turned into text and parsed like Hermes does: fences are stripped, and `parsed` is set only when JSON was requested. A JSON-schema mismatch raises `ValueError`.

Scripting:

- `script(resp, purpose=, task=)` queues responses (first in, first out).
- `respond(fn, ...)` sets a fallback that runs when the queue is empty.
- Lookup order is `(purpose, task)`, then `(purpose, None)`, `(None, task)`, `(None, None)`.
- A dict or list response is serialised to JSON. An exception instance is raised. A prebuilt result is returned unchanged. If nothing matches, the call raises `UnscriptedLlmCall`.

Calls that pass every check land in `ctx.llm.calls` (`.blocks`, `.image_sizes`). Calls refused by the trust gate land in `ctx.llm.rejected`.

## Scenes (`scenes.py`)

Every scene is deterministic for a given seed. Labels come from content only.

- Nuisance-only scenes, which must not trigger: `static_scene`, `lighting_flicker`, `camera_shake`, `slow_drift`, `compression_artifacts` (8x8 DCT quantisation at a quality that changes every frame), `heavy_low_light_noise`, `ir_night_switch`, `local_shadow_pass`, `headlight_sweep` and `subpixel_shake_rotation`.
- Event scenes: `object_appears`, `object_removed`, `screen_text_change`, plus the combined scenes `flicker_with_mug` and `shake_with_mug`. In a combined scene the nuisance runs throughout and only the real event is labelled.
- `disconnect` replays a static clip, then disconnects.
- Helpers: `jpeg_like(img, quality)` and `warp_affine(img, dx, dy, angle)`.
