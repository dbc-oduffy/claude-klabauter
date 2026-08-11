#!/usr/bin/env bash
# test-check-install-singularity.sh — unit tests for lib/check-install-singularity.py
# (retargeted 2026-07-21, DR-079 bash-kill campaign: SUT renamed .sh -> .py,
# invoked via python3 instead of bash)
#
# Spec backlink:
#   docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R1b
#   AC4, AC5, AC5b
#
# Coverage (per AC):
#
#   AC4 — Each FAIL case fires non-zero + remediation:
#     T1  .claude-suffixed CLAUDE_HOME → exit 1, remediation on stderr
#     T2  Doubled .claude/.claude venv pin in registry → exit 1
#     T3  >1 distinct canonical coordinator tree (no explicit override) → exit 1
#     T4  Two PRESENT settings files with different coordinator paths → exit 1
#
#   AC4 must-NOT-FAIL sub-cases:
#     T5  registry live_path == flat ~/.claude/plugins/coordinator-claude (same
#         canonical dir after dedupe) → exit 0   [false-FAIL guard (a)]
#     T6  Flat path has .git + no other distinct tree (source_is_live) → exit 0
#         [false-FAIL guard (b)]
#     T7  settings.local.json absent → concordant, not a disagreement → exit 0
#         [false-FAIL guard (c)]
#
#   AC5 — Consented dev-loop override exemption:
#     T8  Single COORDINATOR_CLONE → .git-backed clone → exit 0
#     T9  Single COORDINATOR_ROOT → parent has .git → exit 0
#
#   AC5b — CLAUDE_PLUGIN_ROOT is NOT in the exemption set:
#     T10 CLAUDE_PLUGIN_ROOT set + >1 trees + no COORDINATOR_CLONE/ROOT → exit 1
#
#   Portability:
#     T11 bash -n syntax clean (no GNU-isms in the script under test)
#
# Exit 0 == all tests passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/../check-install-singularity.py"

PASS=0
FAIL=0

_pass() { printf '  PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
_fail() { printf '  FAIL: %s — %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }

printf '=== test-check-install-singularity.sh ===\n'

if [[ ! -f "$SUT" ]]; then
  printf 'FATAL: script under test not found at %s\n' "$SUT" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Temp directory for synthetic layouts.
# ---------------------------------------------------------------------------
_TMPDIR="$(mktemp -d)"
_cleanup() { rm -rf "$_TMPDIR"; }
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Synthetic layout builder.
#
# Creates a minimal ~/.claude tree rooted at $_TMPDIR/home:
#   home/.claude/
#     machine-local/registry.local.toml   (optional, written by caller)
#     plugins/coordinator-claude/          (the "flat" plugin root)
#     settings.json
#     settings.local.json                  (optional — absent by default)
#     plugins/known_marketplaces.json      (optional)
#
# Usage: _setup_home [--with-git] [--with-extra-tree <path>]
#   --with-git        create a .git dir inside the flat coordinator-claude path
#
# Sets: _FAKE_HOME, _FLAT_PATH, _REG_FILE
# ---------------------------------------------------------------------------
_setup_base() {
  rm -rf "${_FAKE_HOME}"
  mkdir -p "${_FLAT_PATH}"
  mkdir -p "${_FAKE_HOME}/.claude/machine-local"
  mkdir -p "${_FAKE_HOME}/.claude/plugins"
  # Default: empty settings files (no coordinator-claude entries)
  printf '{}' > "${_FAKE_HOME}/.claude/settings.json"
  # settings.local.json intentionally NOT created (absent by default)
}

# Review: code-reviewer chunk-5 (F5) — removed first pair of duplicate _FAKE_HOME/_FLAT_PATH
# definitions (were dead code; _setup_base resolves vars at call time from the second block).
_FAKE_HOME="${_TMPDIR}/home"
_FLAT_PATH="${_FAKE_HOME}/.claude/plugins/coordinator-claude"
_REG_FILE="${_FAKE_HOME}/.claude/machine-local/registry.local.toml"

# ---------------------------------------------------------------------------
# Helper: run the SUT in a clean subshell with HOME pointed at the fake home.
# All coordinator resolution env vars are scrubbed; extra assignments can be
# passed as "KEY=VALUE" arguments.
# Returns the exit code in __RC__ and merged stdout+stderr in __OUT__.
# ---------------------------------------------------------------------------
_run_sut() {
  # Run in a subshell; capture exit code; allow non-zero without aborting.
  local _rc=0
  local _out
  _out="$(
    unset COORDINATOR_CLONE COORDINATOR_ROOT CLAUDE_PLUGIN_ROOT CLAUDE_HOME \
          HOME USERPROFILE || true
    # Must export HOME so that the child "python3 $SUT" subprocess inherits it.
    export HOME="${_FAKE_HOME}"
    for _a in "$@"; do
      export "${_a?}"
    done
    python3 "$SUT" 2>&1
  )" || _rc=$?
  __RC__=$_rc
  __OUT__="$_out"
}

# ===========================================================================
# T1 — .claude-suffixed CLAUDE_HOME → FAIL
# ===========================================================================
_setup_base
_run_sut "CLAUDE_HOME=${_FAKE_HOME}/.claude"

if [[ "$__RC__" -ne 0 ]]; then
  _pass "T1: .claude-suffixed CLAUDE_HOME → exit non-zero"
else
  _fail "T1: .claude-suffixed CLAUDE_HOME" "expected non-zero exit, got 0"
fi
if printf '%s' "$__OUT__" | grep -qi 'remediation'; then
  _pass "T1b: .claude-suffixed CLAUDE_HOME → remediation message present"
else
  _fail "T1b: .claude-suffixed CLAUDE_HOME" "expected 'remediation' in output, got: ${__OUT__}"
fi

# ===========================================================================
# T2 — Doubled .claude/.claude venv pin → FAIL
# ===========================================================================
_setup_base
# Write a registry.local.toml with a doubled venv pin
cat > "$_REG_FILE" <<'TOML'
[coordinator]
python = "/home/user/.claude/.claude/.coordinator-venv/bin/python"
TOML

_run_sut
if [[ "$__RC__" -ne 0 ]]; then
  _pass "T2: doubled .claude venv pin → exit non-zero"
else
  _fail "T2: doubled .claude venv pin" "expected non-zero exit, got 0"
fi
if printf '%s' "$__OUT__" | grep -qi 'remediation'; then
  _pass "T2b: doubled .claude venv pin → remediation message present"
else
  _fail "T2b: doubled .claude venv pin" "expected 'remediation' in output, got: ${__OUT__}"
fi

# ===========================================================================
# T3 — >1 distinct canonical coordinator tree (no explicit override) → FAIL
# ===========================================================================
_setup_base
# Create a second coordinator tree in a different location
_EXTRA_TREE="${_TMPDIR}/extra/coordinator-claude"
mkdir -p "$_EXTRA_TREE"

# Write the registry live_path pointing at the extra tree's coordinator/ subdir
mkdir -p "${_EXTRA_TREE}/coordinator"
cat > "$_REG_FILE" <<TOML
[plugin.mirrors.coordinator-claude]
live_path = "${_EXTRA_TREE}/coordinator"
TOML

_run_sut
if [[ "$__RC__" -ne 0 ]]; then
  _pass "T3: >1 distinct canonical tree (no override) → exit non-zero"
else
  _fail "T3: >1 distinct canonical tree (no override)" "expected non-zero exit, got 0"
fi
if printf '%s' "$__OUT__" | grep -qi 'remediation'; then
  _pass "T3b: >1 distinct canonical tree → remediation message present"
else
  _fail "T3b: >1 distinct canonical tree" "expected 'remediation' in output, got: ${__OUT__}"
fi

# ===========================================================================
# T4 — Two PRESENT settings files with different coordinator paths → FAIL
# ===========================================================================
_setup_base

# settings.json has one path, known_marketplaces.json has a different path
_CANONICAL_PATH="${_FLAT_PATH}"
_OTHER_PATH="${_TMPDIR}/other/coordinator-claude"
mkdir -p "$_OTHER_PATH"

# Write settings.json with coordinator-claude at the canonical path
cat > "${_FAKE_HOME}/.claude/settings.json" <<JSON
{
  "extraKnownMarketplaces": {
    "coordinator-claude": {
      "source": {
        "source": "directory",
        "path": "${_CANONICAL_PATH}"
      }
    }
  }
}
JSON

# Write known_marketplaces.json with coordinator-claude at a DIFFERENT path
mkdir -p "${_FAKE_HOME}/.claude/plugins"
cat > "${_FAKE_HOME}/.claude/plugins/known_marketplaces.json" <<JSON
{
  "coordinator-claude": {
    "source": {
      "source": "directory",
      "path": "${_OTHER_PATH}"
    }
  }
}
JSON

_run_sut
if [[ "$__RC__" -ne 0 ]]; then
  _pass "T4: two PRESENT settings files disagree → exit non-zero"
else
  _fail "T4: two PRESENT settings files disagree" "expected non-zero exit, got 0"
fi
if printf '%s' "$__OUT__" | grep -qi 'remediation'; then
  _pass "T4b: two PRESENT settings files disagree → remediation message present"
else
  _fail "T4b: two PRESENT settings files disagree" "expected 'remediation' in output, got: ${__OUT__}"
fi

# ===========================================================================
# T5 — registry live_path == flat path (same canonical dir) → exit 0
#       False-FAIL guard (a): dedupe prevents >1 count
# ===========================================================================
_setup_base
# live_path is the coordinator/ subdir of the flat coordinator-claude dir.
# After _to_plugin_root strips /coordinator, it resolves to the SAME canonical
# dir as the flat path. So dedupe leaves exactly 1 tree.
mkdir -p "${_FLAT_PATH}/coordinator"
cat > "$_REG_FILE" <<TOML
[plugin.mirrors.coordinator-claude]
live_path = "${_FLAT_PATH}/coordinator"
TOML

_run_sut
if [[ "$__RC__" -eq 0 ]]; then
  _pass "T5: registry live_path == flat path (same canonical dir after dedupe) → exit 0"
else
  _fail "T5: registry live_path == flat path" "expected exit 0, got ${__RC__}; output: ${__OUT__}"
fi

# ===========================================================================
# T6 — single distinct coordinator tree (no other trees) → exit 0
#       False-FAIL guard (b): passes because _tree_count==1, not .git detection.
#       Review: code-reviewer — B-F5: renamed from "source_is_live" which implied
#       .git-detection logic; exit-0 is purely because only one tree exists.
# ===========================================================================
_setup_base
# Note: .git creation here is incidental — the check passes due to _tree_count==1,
# not because it detects .git. Left for historical context only.

_run_sut
if [[ "$__RC__" -eq 0 ]]; then
  _pass "T6: single distinct tree (_tree_count==1, no other distinct tree) → exit 0"
else
  _fail "T6: single distinct tree" "expected exit 0, got ${__RC__}; output: ${__OUT__}"
fi

# ===========================================================================
# T7 — settings.local.json absent → concordant, not a disagreement → exit 0
#       False-FAIL guard (c)
# ===========================================================================
_setup_base
# settings.json has an entry; settings.local.json is ABSENT; known_marketplaces
# agrees with settings.json. Only one PRESENT path → concordant.
mkdir -p "${_FAKE_HOME}/.claude/plugins"
cat > "${_FAKE_HOME}/.claude/settings.json" <<JSON
{
  "extraKnownMarketplaces": {
    "coordinator-claude": {
      "source": {
        "source": "directory",
        "path": "${_FLAT_PATH}"
      }
    }
  }
}
JSON
cat > "${_FAKE_HOME}/.claude/plugins/known_marketplaces.json" <<JSON
{
  "coordinator-claude": {
    "source": {
      "source": "directory",
      "path": "${_FLAT_PATH}"
    }
  }
}
JSON
# settings.local.json intentionally NOT created
if [[ ! -f "${_FAKE_HOME}/.claude/settings.local.json" ]]; then
  _pass "T7-precond: settings.local.json is absent (test precondition)"
else
  _fail "T7-precond: settings.local.json absent" "file unexpectedly exists"
fi

_run_sut
if [[ "$__RC__" -eq 0 ]]; then
  _pass "T7: settings.local.json absent → concordant → exit 0"
else
  _fail "T7: settings.local.json absent → concordant" "expected exit 0, got ${__RC__}; output: ${__OUT__}"
fi

# ===========================================================================
# T8 — Single COORDINATOR_CLONE → .git-backed clone → exit 0 (AC5)
# ===========================================================================
_setup_base
# Create a .git-backed clone in a separate dir (different from the flat path)
_CLONE_DIR="${_TMPDIR}/clone/coordinator-claude"
mkdir -p "${_CLONE_DIR}/.git"

# Without the exemption, the flat path AND the clone would be 2 trees → FAIL.
# With the single COORDINATOR_CLONE set to a .git-backed dir → EXEMPT → exit 0.
_run_sut "COORDINATOR_CLONE=${_CLONE_DIR}"
if [[ "$__RC__" -eq 0 ]]; then
  _pass "T8: single COORDINATOR_CLONE (.git-backed) → exempt → exit 0"
else
  _fail "T8: single COORDINATOR_CLONE (.git-backed) → exempt" "expected exit 0, got ${__RC__}; output: ${__OUT__}"
fi
if printf '%s' "$__OUT__" | grep -qi 'dev-loop override'; then
  _pass "T8b: single COORDINATOR_CLONE → INFO 'dev-loop override' emitted"
else
  _fail "T8b: single COORDINATOR_CLONE → INFO line" "expected 'dev-loop override' in INFO, got: ${__OUT__}"
fi

# ===========================================================================
# T9 — Single COORDINATOR_ROOT → parent has .git → exit 0 (AC5)
# ===========================================================================
_setup_base
_ROOT_PLUGIN="${_TMPDIR}/root_plugin/coordinator-claude"
mkdir -p "${_ROOT_PLUGIN}/.git"
mkdir -p "${_ROOT_PLUGIN}/coordinator"

# COORDINATOR_ROOT points to the coordinator/ content subdir; its parent has .git
_run_sut "COORDINATOR_ROOT=${_ROOT_PLUGIN}/coordinator"
if [[ "$__RC__" -eq 0 ]]; then
  _pass "T9: single COORDINATOR_ROOT (parent .git-backed) → exempt → exit 0"
else
  _fail "T9: single COORDINATOR_ROOT (parent .git-backed)" "expected exit 0, got ${__RC__}; output: ${__OUT__}"
fi

# ===========================================================================
# T10 — CLAUDE_PLUGIN_ROOT set + >1 trees + no COORDINATOR_CLONE/ROOT → FAIL
#        (AC5b: CLAUDE_PLUGIN_ROOT is harness-injected, NOT exempt)
# ===========================================================================
_setup_base
# The flat path exists (1 tree). CLAUDE_PLUGIN_ROOT points to a different dir (2nd tree).
_CPR_DIR="${_TMPDIR}/cpr/coordinator-claude"
mkdir -p "$_CPR_DIR"

# CLAUDE_PLUGIN_ROOT brings in a second tree; no COORDINATOR_CLONE/ROOT set
_run_sut "CLAUDE_PLUGIN_ROOT=${_CPR_DIR}"
if [[ "$__RC__" -ne 0 ]]; then
  _pass "T10: CLAUDE_PLUGIN_ROOT (harness-injected) + >1 trees + no override → FAIL (non-zero)"
else
  _fail "T10: CLAUDE_PLUGIN_ROOT + >1 trees (no override)" "expected non-zero exit, got 0; output: ${__OUT__}"
fi

# ===========================================================================
# T11 — Portability: python3 syntax clean
#        Review: retargeted 2026-07-21 (DR-079 bash-kill campaign) — SUT is
#        now Python, so the portability floor is a compile check, not
#        "bash -n".
# ===========================================================================
if python3 -m py_compile "$SUT" 2>/dev/null; then
  _pass "T11: python3 -m py_compile syntax check passes"
else
  _fail "T11: python3 -m py_compile syntax check" "script has syntax errors"
fi

# ===========================================================================
# T12 — CLAUDE_HOME already .claude-suffixed (sentinel leakage shape) → exit 1
#        Review: code-reviewer — B-F6: regression for B-F1 fix.
#        When the sentinel script reassigns CLAUDE_HOME to a .claude-suffixed value
#        and passes it to this check, CHECK 1 must reject it (not false-pass).
#        The fix (B-F1) passes _ORIGINAL_CLAUDE_HOME to avoid this; this test locks
#        the expected behavior when the suffixed value IS passed.
# ===========================================================================
_setup_base
set +e
# Simulate what would happen if sentinel leaks its local CLAUDE_HOME (ending in .claude)
# into the subprocess. The check must FAIL with a remediation message.
_t12_out="$(CLAUDE_HOME="${_FAKE_HOME}/.claude/.claude" python3 "$SUT" 2>&1)"
_t12_rc=$?
set -e
if [[ "$_t12_rc" -ne 0 ]]; then
  _pass "T12: .claude-suffixed CLAUDE_HOME (sentinel leakage shape) → non-zero (CHECK 1 fires)"
else
  _fail "T12: .claude-suffixed CLAUDE_HOME" "expected non-zero exit (CHECK 1); got 0; output: ${_t12_out}"
fi

# ===========================================================================
# Final verdict
# ===========================================================================
printf '\n'
printf 'Results: %d passed, %d failed\n' "$PASS" "$FAIL"
if [[ "$FAIL" -eq 0 ]]; then
  printf 'ALL TESTS PASSED\n'
  exit 0
else
  printf 'TESTS FAILED\n'
  exit 1
fi
