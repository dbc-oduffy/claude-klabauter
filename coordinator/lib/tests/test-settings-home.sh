#!/usr/bin/env bash
# test-settings-home.sh — unit tests for the settings-home resolution seam.
#
# Tests the Python resolver (_claude_home.py settings-home / machine-local)
# and the coordinator-settings-home CLI wrapper.
#
# T1-T5 (the former lib/settings-home.sh direct-source coverage) moved to
# tests/test_settings_home.py — a pytest port exercising the naked-python
# lib/settings_home.py that replaced lib/settings-home.sh (2026-07-19 debash
# campaign, chunk E3-e). See that file for T1-T5's current coverage.
#
# Coverage (this file):
#   T6  CLI: no-args prints settings-home path
#   T7  CLI: check subcommand — divergent homes returns non-zero
#   T8  CLI: check subcommand — compat symlink returns zero
#
#   Python resolver (_claude_home.py):
#     T9  COORDINATOR_SETTINGS_HOME wins in Python settings-home subcommand
#     T10 CLAUDE_HOME-relative fallback in Python settings-home subcommand
#     T11 sandbox redirect: CLAUDE_HOME redirects settings-home root
#     T12 Python machine-local delegates to settings-home seam
#     T13 Python machine-local divergence check — fail-loud on divergent realpaths
#     T14 Python machine-local divergence check — compat symlink does NOT fail-loud
#
# Exit 0 == all tests passed.
#
# Spec backlink: docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME_BIN="$SCRIPT_DIR/../claude-home/claude-home"
COORDINATOR_SETTINGS_HOME_BIN="$SCRIPT_DIR/../../templates/bin/coordinator-settings-home"
CLAUDE_HOME_PY="$SCRIPT_DIR/../claude-home/_claude_home.py"

PASS=0
FAIL=0

_pass() { printf '  PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
_fail() { printf '  FAIL: %s — %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }

echo "=== test-settings-home.sh ==="

# --- Precondition checks ---
for _f in "$CLAUDE_HOME_BIN" "$COORDINATOR_SETTINGS_HOME_BIN" "$CLAUDE_HOME_PY"; do
    if [[ ! -f "$_f" ]]; then
        echo "FATAL: required file not found: $_f" >&2
        exit 1
    fi
done

# Require python3 for the Python tests
if ! command -v python3 >/dev/null 2>&1; then
    echo "FATAL: python3 not found; Python tests cannot run" >&2
    exit 1
fi

# Sandbox: use a temp directory as the fake HOME for all tests.
# This isolates every test from the operator's real ~/.claude and ~/.coordinator-claude-settings.
_TMPDIR="$(mktemp -d)"
_cleanup() { rm -rf "$_TMPDIR"; }
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_run_cli() {
    local _subcmd="${1:-}"
    shift || true
    local _extra_args=("$@")
    (
        unset COORDINATOR_SETTINGS_HOME || true
        unset CLAUDE_HOME               || true
        HOME="$_TMPDIR/fakehome"
        for _assign in "${_extra_args[@]}"; do
            eval "export $_assign"
        done
        if [[ -n "$_subcmd" ]]; then
            bash "$COORDINATOR_SETTINGS_HOME_BIN" "$_subcmd"
        else
            bash "$COORDINATOR_SETTINGS_HOME_BIN"
        fi
    )
}

_run_py() {
    local _subcmd="$1"
    shift
    local _extra_args=("$@")
    (
        unset COORDINATOR_SETTINGS_HOME || true
        unset CLAUDE_HOME               || true
        HOME="$_TMPDIR/fakehome"
        for _assign in "${_extra_args[@]}"; do
            eval "export $_assign"
        done
        python3 "$CLAUDE_HOME_PY" "$_subcmd"
    )
}

# ---------------------------------------------------------------------------
# T6 — CLI: no-args prints settings-home path (COORDINATOR_SETTINGS_HOME override)
# ---------------------------------------------------------------------------
_t6_dir="$_TMPDIR/t6-sh"
_result="$(_run_cli "" "COORDINATOR_SETTINGS_HOME=$_t6_dir" 2>/dev/null)"
# Strip trailing newline for comparison
_result="${_result%$'\n'}"
if [[ "$_result" == "$_t6_dir" ]]; then
    _pass "T6 CLI: no-args prints settings-home path"
else
    _fail "T6 CLI: no-args prints settings-home path" "got '$_result', want '$_t6_dir'"
fi

# ---------------------------------------------------------------------------
# T7 — CLI: check subcommand with divergent homes returns non-zero
# ---------------------------------------------------------------------------
_t7_dir="$_TMPDIR/t7"
mkdir -p "${_t7_dir}/home/.claude/machine-local"
mkdir -p "${_t7_dir}/settings/machine-local"
_t7_exit=0
(
    unset COORDINATOR_SETTINGS_HOME || true
    CLAUDE_HOME="${_t7_dir}/home"
    COORDINATOR_SETTINGS_HOME="${_t7_dir}/settings"
    export CLAUDE_HOME COORDINATOR_SETTINGS_HOME
    bash "$COORDINATOR_SETTINGS_HOME_BIN" check
) 2>/dev/null || _t7_exit=$?
if [[ $_t7_exit -ne 0 ]]; then
    _pass "T7 CLI: check returns non-zero on divergent homes (exit $_t7_exit)"
else
    _fail "T7 CLI: check returns non-zero on divergent homes" "expected non-zero exit, got 0"
fi

# ---------------------------------------------------------------------------
# T8 — CLI: check subcommand with compat symlink returns zero
# ---------------------------------------------------------------------------
_t8_dir="$_TMPDIR/t8"
mkdir -p "${_t8_dir}/settings/machine-local"
mkdir -p "${_t8_dir}/home/.claude"
ln -s "${_t8_dir}/settings/machine-local" "${_t8_dir}/home/.claude/machine-local"
_t8_exit=0
(
    unset COORDINATOR_SETTINGS_HOME || true
    CLAUDE_HOME="${_t8_dir}/home"
    COORDINATOR_SETTINGS_HOME="${_t8_dir}/settings"
    export CLAUDE_HOME COORDINATOR_SETTINGS_HOME
    bash "$COORDINATOR_SETTINGS_HOME_BIN" check
) 2>/dev/null || _t8_exit=$?
if [[ $_t8_exit -eq 0 ]]; then
    _pass "T8 CLI: check returns zero on compat symlink"
else
    _fail "T8 CLI: check returns zero on compat symlink" "unexpected exit $_t8_exit"
fi

# ---------------------------------------------------------------------------
# T9 — Python: COORDINATOR_SETTINGS_HOME wins in settings-home subcommand
# ---------------------------------------------------------------------------
_t9_dir="$_TMPDIR/t9-py-override"
_result="$(_run_py "settings-home" "COORDINATOR_SETTINGS_HOME=$_t9_dir")"
if [[ "$_result" == "$_t9_dir" ]]; then
    _pass "T9 Python: COORDINATOR_SETTINGS_HOME wins"
else
    _fail "T9 Python: COORDINATOR_SETTINGS_HOME wins" "got '$_result', want '$_t9_dir'"
fi

# ---------------------------------------------------------------------------
# T10 — Python: CLAUDE_HOME-relative fallback in settings-home subcommand
# ---------------------------------------------------------------------------
_t10_ch="$_TMPDIR/t10-ch"
_t10_expected="${_t10_ch}/.coordinator-claude-settings"
_result="$(_run_py "settings-home" "CLAUDE_HOME=$_t10_ch")"
if [[ "$_result" == "$_t10_expected" ]]; then
    _pass "T10 Python: CLAUDE_HOME-relative fallback"
else
    _fail "T10 Python: CLAUDE_HOME-relative fallback" "got '$_result', want '$_t10_expected'"
fi

# ---------------------------------------------------------------------------
# T11 — Python: sandbox-redirect via CLAUDE_HOME
# ---------------------------------------------------------------------------
_t11_sandbox="$_TMPDIR/t11-sandbox"
_t11_expected="${_t11_sandbox}/.coordinator-claude-settings"
_result="$(
    unset COORDINATOR_SETTINGS_HOME 2>/dev/null || true
    HOME="$_t11_sandbox" CLAUDE_HOME="$_t11_sandbox" python3 "$CLAUDE_HOME_PY" "settings-home"
)"
if [[ "$_result" == "$_t11_expected" ]]; then
    _pass "T11 Python: sandbox-redirect via CLAUDE_HOME"
else
    _fail "T11 Python: sandbox-redirect via CLAUDE_HOME" "got '$_result', want '$_t11_expected'"
fi

# ---------------------------------------------------------------------------
# T12 — Python: machine-local delegates to settings-home seam
# ---------------------------------------------------------------------------
_t12_sh="$_TMPDIR/t12-sh"
_t12_expected="${_t12_sh}/machine-local"
_result="$(_run_py "machine-local" "COORDINATOR_SETTINGS_HOME=$_t12_sh")"
if [[ "$_result" == "$_t12_expected" ]]; then
    _pass "T12 Python: machine-local delegates to settings-home seam"
else
    _fail "T12 Python: machine-local delegates to settings-home seam" "got '$_result', want '$_t12_expected'"
fi

# ---------------------------------------------------------------------------
# T13 — Python: machine-local divergence check fails loud on divergent realpaths
# ---------------------------------------------------------------------------
# Legacy: home_dir()/.claude/machine-local = CLAUDE_HOME/.claude/machine-local
# New:    settings_home()/machine-local = COORDINATOR_SETTINGS_HOME/machine-local
_t13_dir="$_TMPDIR/t13"
mkdir -p "${_t13_dir}/home/.claude/machine-local"
mkdir -p "${_t13_dir}/settings/machine-local"
_t13_exit=0
(
    unset COORDINATOR_SETTINGS_HOME || true
    CLAUDE_HOME="${_t13_dir}/home"
    COORDINATOR_SETTINGS_HOME="${_t13_dir}/settings"
    export CLAUDE_HOME COORDINATOR_SETTINGS_HOME
    python3 "$CLAUDE_HOME_PY" "machine-local"
) 2>/dev/null || _t13_exit=$?
if [[ $_t13_exit -ne 0 ]]; then
    _pass "T13 Python: machine-local fails loud on divergent realpaths (exit $_t13_exit)"
else
    _fail "T13 Python: machine-local fails loud on divergent realpaths" "expected non-zero exit, got 0"
fi

# ---------------------------------------------------------------------------
# T14 — Python: machine-local divergence check does NOT fail on compat symlink
# ---------------------------------------------------------------------------
_t14_dir="$_TMPDIR/t14"
mkdir -p "${_t14_dir}/settings/machine-local"
mkdir -p "${_t14_dir}/home/.claude"
ln -s "${_t14_dir}/settings/machine-local" "${_t14_dir}/home/.claude/machine-local"
_t14_exit=0
_t14_result="$(
    unset COORDINATOR_SETTINGS_HOME 2>/dev/null || true
    CLAUDE_HOME="${_t14_dir}/home"
    COORDINATOR_SETTINGS_HOME="${_t14_dir}/settings"
    export CLAUDE_HOME COORDINATOR_SETTINGS_HOME
    python3 "$CLAUDE_HOME_PY" "machine-local" 2>/dev/null
)" || _t14_exit=$?
_t14_expected="${_t14_dir}/settings/machine-local"
if [[ $_t14_exit -eq 0 && "$_t14_result" == "$_t14_expected" ]]; then
    _pass "T14 Python: compat symlink does NOT trigger fail-loud; returns settings-home path"
else
    _fail "T14 Python: compat symlink does NOT trigger fail-loud" \
          "exit=$_t14_exit result='$_t14_result' want exit=0 result='$_t14_expected'"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
