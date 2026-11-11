#!/usr/bin/env bash
# Local quality gate for sentinel-vision. Runs, in order:
#   1. ruff check                    (whole workspace)
#   2. ruff format --check           (whole workspace)
#   3. pytest plugin tests           (sentinel-vision/tests)
#   4. pytest tool tests             (tests_tools)
#   5. rule_lint                     (catalog admission rules 3/7/8/9/11/17)
#   6. ruff check (strict)           (ruff.strict.toml: docstrings, naming, complexity; Code standard)
#   7. structure_lint                (module/class/function size, nesting, package width,
#                                     __init__ logic, docstrings; FAILS on any finding)
#   8. hermes plugins validate <plugin> --install-deps
#   9. dev-link: symlink + enable the plugin in the isolated home (so its deps are in that env)
#  10. hermes plugins doctor <plugin> --ci   (fails on any "WARN:" line, not just a bad exit)
# then prints a pass/fail summary and exits non-zero if any step failed.
#
# Isolation: the Hermes steps run with HERMES_HOME pointed at a dev-only data root
# (default: ${XDG_CACHE_HOME:-~/.cache}/hermes-sentinel-dev/hermes-home), never ~/.hermes.
# Running the Hermes CLI against the real home can finish pending updates, sync skills and
# install tools; an isolated home prevents that. Hermes builds a private runtime for that
# root the first time (about 2-3 minutes, ~400 MB), then reuses it. --install-deps installs
# into that isolated runtime, not the user's Hermes venv. Bytecode is redirected to the
# cache dir so neither the Hermes checkout nor the plugin dir gets __pycache__ files.
#
# Usage: scripts/check.sh [--with-hermes] [-h|--help]
#        The Hermes steps (8-10) are SKIPPED by default so the real Hermes install is never
#        touched; pass --with-hermes to run them (still in the isolated home).
# Env:   HERMES_AGENT_DIR (default ~/.hermes/hermes-agent, read only)
#        HERMES_PYTHON    (default python3; must have Hermes' dependencies)
#        SENTINEL_HERMES_HOME (override the isolated data root)
#        UV               (default: uv on PATH, else ~/.local/bin/uv)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="$ROOT/sentinel-vision"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
HERMES_PYTHON="${HERMES_PYTHON:-python3}"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/hermes-sentinel-dev"
DEV_HERMES_HOME="${SENTINEL_HERMES_HOME:-$CACHE_ROOT/hermes-home}"
SKIP_HERMES=1

for arg in "$@"; do
  case "$arg" in
    --skip-hermes) SKIP_HERMES=1 ;;
    --with-hermes) SKIP_HERMES=0 ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "check.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
if [[ ! -x "$UV" ]]; then
  echo "check.sh: uv not found (set UV=/path/to/uv)" >&2
  exit 2
fi

# Never let the Hermes steps touch the real data root.
real_home="$(cd "$HOME/.hermes" 2>/dev/null && pwd -P || echo "$HOME/.hermes")"
mkdir -p "$DEV_HERMES_HOME" "$CACHE_ROOT/pycache"
dev_home_resolved="$(cd "$DEV_HERMES_HOME" && pwd -P)"
case "$dev_home_resolved/" in
  "$real_home/"*)
    echo "check.sh: refusing to use $DEV_HERMES_HOME as HERMES_HOME (inside ~/.hermes)" >&2
    exit 2 ;;
esac

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$CACHE_ROOT/pycache"

declare -a NAMES=() RESULTS=() TIMES=()
FAILED=0

run_step() {
  local name="$1"; shift
  local start end rc
  printf '\n\033[1m==> %s\033[0m\n    $ %s\n' "$name" "$*"
  start=$(date +%s)
  ( cd "$ROOT" && "$@" )
  rc=$?
  end=$(date +%s)
  NAMES+=("$name"); TIMES+=("$((end - start))s")
  if [[ $rc -eq 0 ]]; then
    RESULTS+=("PASS")
  else
    RESULTS+=("FAIL (exit $rc)"); FAILED=1
  fi
}

skip_step() {
  NAMES+=("$1"); RESULTS+=("SKIP ($2)"); TIMES+=("-")
  printf '\n\033[1m==> %s\033[0m\n    skipped: %s\n' "$1" "$2"
}

hermes() {
  HERMES_HOME="$DEV_HERMES_HOME" "$HERMES_PYTHON" -B "$HERMES_AGENT_DIR/hermes" "$@"
}

# Symlink the plugin into the isolated dev home and enable it there (idempotent).
dev_link_plugin() {
  local link="$DEV_HERMES_HOME/plugins/$(basename "$PLUGIN_DIR")"
  mkdir -p "$DEV_HERMES_HOME/plugins" || return 1
  if [[ -L "$link" ]]; then
    [[ "$(readlink -f "$link")" == "$(readlink -f "$PLUGIN_DIR")" ]] || {
      echo "check.sh: $link points elsewhere; refusing" >&2; return 1; }
  elif [[ -e "$link" ]]; then
    echo "check.sh: $link exists and is not a symlink; refusing" >&2; return 1
  else
    ln -s "$PLUGIN_DIR" "$link" || return 1
  fi
  hermes plugins enable "$(basename "$PLUGIN_DIR")" </dev/null
}

# doctor --ci exits 0 even when it prints warnings; every warning must be read and fixed
# (catalog checklist), so any "WARN:" in its output fails the step.
hermes_doctor_strict() {
  local out rc
  out="$(mktemp "$CACHE_ROOT/doctor-output.XXXXXX")" || return 2
  hermes plugins doctor "$@" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  if [[ $rc -eq 0 ]] && grep -q "WARN:" "$out"; then
    echo "check.sh: doctor printed warnings (WARN:); treating as failure" >&2
    rc=1
  fi
  rm -f "$out"
  return "$rc"
}

run_step "ruff check" "$UV" run --group dev ruff check .
run_step "ruff format --check" "$UV" run --group dev ruff format --check .
if [[ -n "$(find "$PLUGIN_DIR/tests" -name 'test_*.py' -print -quit 2>/dev/null)" ]]; then
  run_step "pytest (plugin tests)" "$UV" run --group dev pytest "$PLUGIN_DIR/tests"
else
  skip_step "pytest (plugin tests)" "no tests in $PLUGIN_DIR/tests yet"
fi
run_step "pytest (tests_tools)" "$UV" run --group dev pytest "$ROOT/tests_tools"
run_step "rule_lint" "$UV" run --group dev python "$ROOT/scripts/rule_lint.py" "$PLUGIN_DIR"
run_step "ruff check (strict)" "$UV" run --group dev ruff check --config "$ROOT/ruff.strict.toml" .
run_step "structure_lint" "$UV" run --group dev python "$ROOT/scripts/structure_lint.py"

if [[ $SKIP_HERMES -eq 1 ]]; then
  skip_step "hermes plugins validate" "pass --with-hermes to run"
  skip_step "dev-link plugin (isolated home)" "pass --with-hermes to run"
  skip_step "hermes plugins doctor" "pass --with-hermes to run"
elif [[ ! -f "$HERMES_AGENT_DIR/hermes" ]]; then
  skip_step "hermes plugins validate" "no Hermes checkout at $HERMES_AGENT_DIR"
  skip_step "dev-link plugin (isolated home)" "no Hermes checkout at $HERMES_AGENT_DIR"
  skip_step "hermes plugins doctor" "no Hermes checkout at $HERMES_AGENT_DIR"
else
  echo
  echo "Hermes steps use isolated HERMES_HOME=$DEV_HERMES_HOME"
  run_step "hermes plugins validate" hermes plugins validate "$PLUGIN_DIR" --install-deps
  # doctor checks python_dependencies against the data root's environment, which only carries
  # the deps of plugins installed and enabled there. Link + enable the plugin in the isolated
  # dev home (never ~/.hermes, see the guard above) so doctor sees what a real install sees.
  run_step "dev-link plugin (isolated home)" dev_link_plugin
  run_step "hermes plugins doctor" hermes_doctor_strict "$PLUGIN_DIR" --ci
fi

printf '\n\033[1m==> Summary\033[0m\n'
for i in "${!NAMES[@]}"; do
  printf '  %-26s %-16s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}" "${TIMES[$i]}"
done
if [[ $FAILED -ne 0 ]]; then
  printf '\n\033[31mcheck.sh: FAILED\033[0m\n'
  exit 1
fi
printf '\n\033[32mcheck.sh: all checks passed\033[0m\n'
