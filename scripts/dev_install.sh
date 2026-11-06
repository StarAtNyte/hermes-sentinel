#!/usr/bin/env bash
# Link hermes-sentinel into a Hermes home for live testing, or remove that link.
#
# Dry run by default: it prints the commands and changes nothing. Pass --apply to run them.
#
# Usage:
#   scripts/dev_install.sh [--home DIR] [--apply]               # symlink + enable + doctor
#   scripts/dev_install.sh --uninstall [--home DIR] [--apply]   # disable + remove symlink
#
# --home DIR  Hermes data root to install into (default: $HERMES_HOME, else ~/.hermes).
#             Point it at a scratch dir to try the plugin without touching your real setup.
# Env: HERMES_AGENT_DIR (default ~/.hermes/hermes-agent), HERMES_PYTHON (default python3).
#
# Applying against the real ~/.hermes edits its config.yaml (plugins.enabled). Running the
# Hermes CLI there can also finish a pending Hermes update. Only use --apply when you mean it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="$ROOT/hermes-sentinel"
PLUGIN_NAME="hermes-sentinel"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
HERMES_PYTHON="${HERMES_PYTHON:-python3}"
TARGET_HOME="${HERMES_HOME:-$HOME/.hermes}"
APPLY=0
MODE=install

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --uninstall) MODE=uninstall ;;
    --home) TARGET_HOME="${2:?--home needs a directory}"; shift ;;
    --home=*) TARGET_HOME="${1#--home=}" ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "dev_install.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

LINK="$TARGET_HOME/plugins/$PLUGIN_NAME"
HERMES=(env "HERMES_HOME=$TARGET_HOME" "$HERMES_PYTHON" -B "$HERMES_AGENT_DIR/hermes")

step() {
  # Print a command; run it only with --apply.
  printf '  $'; printf ' %q' "$@"; printf '\n'
  if [[ $APPLY -eq 1 ]]; then "$@"; fi
}

if [[ $APPLY -eq 0 ]]; then
  echo "Dry run ($MODE into HERMES_HOME=$TARGET_HOME). Nothing will change; re-run with --apply."
else
  echo "Applying $MODE into HERMES_HOME=$TARGET_HOME"
fi
[[ -d "$PLUGIN_DIR" ]] || { echo "plugin dir not found: $PLUGIN_DIR" >&2; exit 1; }
[[ -f "$HERMES_AGENT_DIR/hermes" ]] || { echo "Hermes not found at $HERMES_AGENT_DIR" >&2; exit 1; }

if [[ $MODE == install ]]; then
  if [[ -e "$LINK" || -L "$LINK" ]]; then
    if [[ -L "$LINK" && "$(readlink -f "$LINK")" == "$(readlink -f "$PLUGIN_DIR")" ]]; then
      echo "  (symlink already present: $LINK -> $PLUGIN_DIR)"
    else
      echo "refusing: $LINK exists and is not a symlink to $PLUGIN_DIR; remove it yourself" >&2
      exit 1
    fi
  else
    step mkdir -p "$TARGET_HOME/plugins"
    step ln -s "$PLUGIN_DIR" "$LINK"
  fi
  # Enabling a non-bundled plugin asks for consent to its declared capabilities.
  step "${HERMES[@]}" plugins enable "$PLUGIN_NAME" --no-allow-tool-override
  step "${HERMES[@]}" plugins doctor "$PLUGIN_NAME" --ci
  echo
  echo "Then start Hermes with HERMES_HOME=$TARGET_HOME and run /sentinel status."
else
  step "${HERMES[@]}" plugins disable "$PLUGIN_NAME" || echo "  (disable failed; continuing)"
  if [[ -L "$LINK" ]]; then
    if [[ "$(readlink -f "$LINK")" == "$(readlink -f "$PLUGIN_DIR")" ]]; then
      step rm "$LINK"   # removes the symlink only, never the plugin source
    else
      echo "refusing: $LINK points elsewhere ($(readlink "$LINK")); leaving it" >&2
      exit 1
    fi
  elif [[ -e "$LINK" ]]; then
    echo "refusing: $LINK is not a symlink (a real install?); leaving it" >&2
    exit 1
  else
    echo "  (no symlink at $LINK)"
  fi
fi
