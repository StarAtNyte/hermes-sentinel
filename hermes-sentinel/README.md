# hermes-sentinel

A Hermes Agent plugin that watches cameras (webcam/RTSP), the screen, or a web
page screenshot for events you describe in plain English. A cheap local
change gate runs on every frame; only changed frames are sent to a vision
model judge. Confirmed events (multi-frame confirmation, cooldown) are stored
with captions and wake the agent to alert or act.

**Status:** scaffold. No detection features are implemented yet.

## Install

TODO

## Usage

- `/sentinel status` — show plugin status.

## Configuration

TODO

## Disclosure

TODO: complete before the catalog PR (admission rule 13). Cover:

- Third-party network calls (vision model requests through Hermes `ctx.llm`, RTSP streams, web page fetches).
- Reads outside plugin data (camera devices, screen capture).
- Shell commands, if any.
- Long-running background processes (capture loops).
- Stored data (event frames and captions in the plugin data directory).
- Telemetry: none.

## License

MIT. See [LICENSE](LICENSE).
