#!/usr/bin/env bash
# test_machine_local_template.sh — unit tests for templates/bin/machine-local
# (the compat-forwarder template) and its settings-home wiring through
# install-substrate.py.
#
# Extracted 2026-07-22 from lib/tests/test_resolve_coordinator_clone_shim.sh
# (bash, T-C4a..T-C4d), which bundled these machine-local-template assertions
# into the resolve-coordinator-clone shim's own test file even though the
# subject under test here is unrelated to that shim. The extraction happened
# as a side effect of retiring the resolve-coordinator-clone shim's bash test
# (the shim itself was ported to Python in the same change, see
# test_resolve_coordinator_clone_shim.py) — this file preserves the
# machine-local coverage that would otherwise have been silently dropped.
#
# Coverage:
#   T-C4a  machine-local template resolves _machine_local.py via COORDINATOR_SETTINGS_HOME seam
#   T-C4b  same machine-local template works as compat forwarder (location-independent)
#   T-C4c  install-substrate.py populates settings-home/bin/ AND retains ~/.claude/bin/
#          (round-trip install; asserts BOTH paths exist post-install)
#   T-C4d  legacy ~/.claude/bin/machine-local is RETAINED after install (not absent)
#
# Spec backlink: docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C4
#
# Exit 0 == all tests passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

echo "=== test_machine_local_template.sh ==="

_TMPDIR="$(mktemp -d)"
_cleanup() { rm -rf "$_TMPDIR"; }
trap _cleanup EXIT

_ML_TEMPLATE="$SCRIPT_DIR/../../templates/bin/machine-local"
_COORD_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- T-C4a: machine-local template resolves _machine_local.py via COORDINATOR_SETTINGS_HOME ---
# Verifies the _real= target: when COORDINATOR_SETTINGS_HOME is set, the wrapper
# looks for _machine_local.py under <COORDINATOR_SETTINGS_HOME>/bin/.
_sh_c4a="$_TMPDIR/sh_c4a"
mkdir -p "${_sh_c4a}/bin"
# Create a stub _machine_local.py that emits a marker on invocation
cat > "${_sh_c4a}/bin/_machine_local.py" <<'PYSTUB'
import sys
print(f"STUB_ML_PY_C4A called args={' '.join(sys.argv[1:])}")
sys.exit(0)
PYSTUB

if [[ ! -f "$_ML_TEMPLATE" ]]; then
    _fail "T-C4a" "machine-local template not found at $_ML_TEMPLATE"
else
    # Run wrapper with COORDINATOR_SETTINGS_HOME → stub will be reached
    if out="$(env -i \
        HOME="$_TMPDIR/nohome" \
        PATH="$PATH" \
        COORDINATOR_SETTINGS_HOME="${_sh_c4a}" \
        bash "$_ML_TEMPLATE" test-subcommand 2>"$_TMPDIR/stderr-c4a")"; then
        if [[ "$out" == *"STUB_ML_PY_C4A"* ]]; then
            _pass "T-C4a machine-local resolves _machine_local.py via COORDINATOR_SETTINGS_HOME"
        else
            _fail "T-C4a machine-local resolves via COORDINATOR_SETTINGS_HOME" "stdout: ${out:-<none>}"
        fi
    else
        # Non-zero exit is expected if stub sys.exit(0) doesn't match expected subcommand.
        # Check that it at least reached the stub (not the "not found" error).
        _stderr="$(cat "$_TMPDIR/stderr-c4a")"
        if echo "$out" | grep -q "STUB_ML_PY_C4A" || echo "$_stderr" | grep -q "STUB_ML_PY_C4A"; then
            _pass "T-C4a machine-local resolves _machine_local.py via COORDINATOR_SETTINGS_HOME (stub reached)"
        else
            _fail "T-C4a machine-local resolves via COORDINATOR_SETTINGS_HOME" \
                "stdout=${out:-<none>} stderr=${_stderr:-<none>}"
        fi
    fi
fi

# --- T-C4b: machine-local template is location-independent (compat forwarder scenario) ---
# Same template works regardless of installation path: copy it to a synthetic "legacy"
# location; with COORDINATOR_SETTINGS_HOME still pointing to the real stub, it resolves.
_compat_path="$_TMPDIR/compat_bin/machine-local"
mkdir -p "$_TMPDIR/compat_bin"
cp "$_ML_TEMPLATE" "$_compat_path"
chmod +x "$_compat_path"

if out="$(env -i \
    HOME="$_TMPDIR/nohome" \
    PATH="$PATH" \
    COORDINATOR_SETTINGS_HOME="${_sh_c4a}" \
    bash "$_compat_path" test-subcommand 2>"$_TMPDIR/stderr-c4b" || true)"; then
    :  # stdout captured
else
    out=""
fi
_stderr_b="$(cat "$_TMPDIR/stderr-c4b" 2>/dev/null || true)"
if echo "$out" | grep -q "STUB_ML_PY_C4A" || echo "$_stderr_b" | grep -q "STUB_ML_PY_C4A"; then
    _pass "T-C4b machine-local template is location-independent (compat forwarder scenario)"
else
    _fail "T-C4b machine-local template is location-independent" \
        "stdout=${out:-<none>} stderr=${_stderr_b:-<none>}"
fi

# --- T-C4c + T-C4d: install-substrate.py round-trip — both paths populated, legacy RETAINED ---
# Runs install-substrate.py --setup-only in a fully sandboxed environment.
# Assertions:
#   T-C4c: <settings-home>/bin/machine-local exists after install
#   T-C4d: <compat>/.claude/bin/machine-local EXISTS (RETAINED) after install
_install_ch="$_TMPDIR/install_ch"
_install_sh="$_TMPDIR/install_sh"
mkdir -p "${_install_ch}/.claude" "${_install_sh}"

_install_log="$_TMPDIR/install-substrate-out.txt"

# Run with --setup-only to skip fnm/Windows env steps.
# COORDINATOR_NON_INTERACTIVE=1 suppresses any interactive prompts.
# Migration helper is a no-op on a fresh sandbox (no legacy ~/.claude/machine-local).
source "${_COORD_ROOT}/lib/resolve-python.sh"
# CLAUDE_KLABAUTER_ROOT pass-through: a fresh sandboxed CLAUDE_HOME has no machine-local
# registry entry for repos.claude_klabauter, so cc_invoke's resolution ladder
# fails before install-substrate.py's own logic runs — resolve the REAL
# machine's repos.claude_klabauter once and forward it (same pattern as
# coordinator/tests/test_install_substrate.sh's _REAL_CLAUDE_KLABAUTER_ROOT).
_REAL_CLAUDE_KLABAUTER_ROOT="$(machine-local get repos.claude_klabauter 2>/dev/null || true)"
if CLAUDE_HOME="${_install_ch}" \
   COORDINATOR_SETTINGS_HOME="${_install_sh}" \
   CLAUDE_PLUGIN_ROOT="${_COORD_ROOT}" \
   COORDINATOR_NON_INTERACTIVE=1 \
   CLAUDE_KLABAUTER_ROOT="${_REAL_CLAUDE_KLABAUTER_ROOT}" \
   "$PYTHON_BIN" "${PYTHON_ARGS[@]}" "${_COORD_ROOT}/lib/install-substrate.py" --setup-only \
   >"$_install_log" 2>&1; then

    _new_bin="${_install_sh}/bin/machine-local"
    _compat_bin="${_install_ch}/.claude/bin/machine-local"

    if [[ -f "$_new_bin" ]]; then
        _pass "T-C4c settings-home/bin/machine-local exists post-install"
    else
        _fail "T-C4c settings-home/bin/machine-local exists post-install" \
            "missing ${_new_bin}; install log tail: $(tail -5 "$_install_log")"
    fi

    if [[ -f "$_compat_bin" ]]; then
        _pass "T-C4d legacy ~/.claude/bin/machine-local RETAINED (present) post-install"
    else
        _fail "T-C4d legacy ~/.claude/bin/machine-local RETAINED post-install" \
            "missing ${_compat_bin}; install log tail: $(tail -5 "$_install_log")"
    fi

    # Also verify that machine-local.cmd is retained at the compat path (Windows shim).
    _compat_cmd="${_install_ch}/.claude/bin/machine-local.cmd"
    if [[ -f "$_compat_cmd" ]]; then
        _pass "T-C4d-cmd machine-local.cmd retained at compat path"
    else
        _fail "T-C4d-cmd machine-local.cmd retained at compat path" "missing ${_compat_cmd}"
    fi

else
    _fail "T-C4c/T-C4d install-substrate.py --setup-only run" \
        "exit non-zero; log: $(tail -10 "$_install_log")"
fi

echo "=== machine-local-template: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
