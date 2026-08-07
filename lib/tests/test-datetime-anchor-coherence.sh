#!/usr/bin/env bash
# lib/tests/test-datetime-anchor-coherence.sh — tests for local-day anchor coherence.
#
# Purpose: verifies AC1 (the coordinator-daily-day.sh helper works correctly) and
# AC2 (no anchor-sensitive day-key site still uses `date -u` for a day-anchor
# assignment). AC2 asserts the desired post-fix state and is EXPECTED RED until
# chunks C-A2/C-A3 of docs/plans/2026-06-26-datetime-handling-coherence.md land.
#
# Spec backlink: docs/plans/2026-06-26-datetime-handling-coherence.md § C-A1, AC1, AC2.
#
# Run: bash plugins/coordinator/lib/tests/test-datetime-anchor-coherence.sh
# (from any directory — script resolves its own location)
#
# Exit codes:
#   0  All cases PASS
#   1  One or more cases FAIL

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve the coordinator $D root (two levels up from tests/)
D="${SCRIPT_DIR}/../.."

# ---------------------------------------------------------------------------
# LIB retargeted (de-bash campaign, 2026-07-21): coordinator-daily-day.sh is
# retired. AC1 below used to source+exec the bash lib directly; it now shells
# out to the native peer, coordinator_core.daily_day.local_day (the claude-klabauter-side
# Python port — see coordinator_core/daily_day.py and its pytest coverage
# coordinator_core/test_daily_day.py, which already fully covers format/TZ
# correctness). Resolution mirrors the CLAUDE_KLABAUTER_ROOT bootstrap idiom used
# elsewhere in this test tree (coordinator/tests/test_scaffold_canonical_structure.py's
# _bootstrap_claude_klabauter_root).
# ---------------------------------------------------------------------------
source "${D}/lib/resolve-python.sh"

_native_claude_klabauter_root() {
    "$PYTHON_BIN" "${PYTHON_ARGS[@]}" <<'PYEOF'
import os
import shutil
import subprocess
import sys


def _bootstrap_root():
    root = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if root:
        return root
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"),
        ".coordinator-claude-settings",
    )
    pointer_path = os.path.join(settings_home, "machine-local", ".claude-klabauter-root")
    try:
        with open(pointer_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass
    ml_bin = shutil.which("machine-local")
    if ml_bin:
        result = subprocess.run(
            [ml_bin, "get", "repos.claude_klabauter"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


root = _bootstrap_root()
if not root:
    print("coordinator_claude_klabauter_root: cannot bootstrap CLAUDE_KLABAUTER_ROOT for native import", file=sys.stderr)
    sys.exit(1)
print(root)
PYEOF
}

_CLAUDE_KLABAUTER_ROOT_RESOLVE_FAILED=0
if [[ -z "$PYTHON_BIN" ]]; then
    _CLAUDE_KLABAUTER_ROOT_RESOLVE_FAILED=1
elif ! CLAUDE_KLABAUTER_ROOT="$(_native_claude_klabauter_root)"; then
    _CLAUDE_KLABAUTER_ROOT_RESOLVE_FAILED=1
fi
export CLAUDE_KLABAUTER_ROOT
unset -f _native_claude_klabauter_root

# coordinator_local_day_native — invoke the native peer, capturing stdout+exit.
coordinator_local_day_native() {
    PYTHONPATH="${CLAUDE_KLABAUTER_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
        "$PYTHON_BIN" "${PYTHON_ARGS[@]}" -c \
        "from coordinator_core.daily_day import local_day; print(local_day())"
}

# ---------------------------------------------------------------------------
# Test harness (mirrors sibling test files in this directory)
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$(( PASS + 1 )); }
fail() { echo "  FAIL: $1" >&2; FAIL=$(( FAIL + 1 )); }

assert_eq() {
    local label="$1" actual="$2" expected="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$label"
    else
        fail "$label — expected $(printf '%q' "$expected"), got $(printf '%q' "$actual")"
    fi
}

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$label"
    else
        fail "$label — expected output to contain '$needle', got: '$haystack'"
    fi
}

assert_match() {
    local label="$1" value="$2" pattern="$3"
    if [[ "$value" =~ $pattern ]]; then
        pass "$label"
    else
        fail "$label — '$value' does not match pattern '$pattern'"
    fi
}

assert_file_exists() {
    local label="$1" path="$2"
    if [[ -f "$path" ]]; then
        pass "$label"
    else
        fail "$label — file not found: $path"
    fi
}

assert_not_grep() {
    local label="$1" pattern="$2" file="$3"
    if grep -qE "$pattern" "$file" 2>/dev/null; then
        local hit
        hit="$(grep -nE "$pattern" "$file" | head -3)"
        fail "$label — pattern '$pattern' found in $file: $hit"
    else
        pass "$label"
    fi
}

# ---------------------------------------------------------------------------
# AC1: coordinator_core.daily_day.local_day (native peer of the now-retired
# coordinator-daily-day.sh) is importable and returns YYYY-MM-DD.
#
# Retargeted (de-bash campaign, 2026-07-21): coordinator-daily-day.sh is
# retired — its sourcing-guard (AC1-d) and header-doc (AC1-e) checks were
# bash-lib-specific concepts with no Python analog and are dropped, not
# ported; full format/TZ coverage of the native peer already lives in
# coordinator_core/test_daily_day.py (claude-klabauter-side pytest). This AC1 is now a
# thin on-disk regression proving the example-doctrine-repo side can still reach the peer.
# ---------------------------------------------------------------------------
echo ""
echo "=== AC1: coordinator_core.daily_day.local_day (native peer) ==="

if [[ "$_CLAUDE_KLABAUTER_ROOT_RESOLVE_FAILED" -eq 1 ]]; then
    fail "AC1-a: CLAUDE_KLABAUTER_ROOT not resolvable — coordinator_core.daily_day unavailable"
else
    # AC1-a: native peer is importable and callable.
    _source_result="$(coordinator_local_day_native 2>&1)"
    _source_exit=$?
    if [[ "$_source_exit" -eq 0 ]]; then
        pass "AC1-a: coordinator_core.daily_day.local_day() succeeds (exit 0)"
    else
        fail "AC1-a: local_day() failed (exit $_source_exit): $_source_result"
    fi

    # AC1-b: output matches YYYY-MM-DD
    assert_match "AC1-b: local_day() returns YYYY-MM-DD" \
        "$_source_result" \
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
fi

# ---------------------------------------------------------------------------
# AC2: no anchor-sensitive day-key site still uses `date -u +%Y-%m-%d` or
#      `date -u +%F` for a day-anchor assignment.
#
# Activated as stale-TODO cleanup per docs/plans/2026-07-07-workday-complete-local-day-and-targeted-wrap.md § C1.
# C-A2 and C-A3 of docs/plans/2026-06-26-datetime-handling-coherence.md have landed;
# all 6 anchor sites now use coordinator_local_day() for day-anchor assignments.
# Note: this AC tests day-anchor ASSIGNMENTS (= lines); it does NOT guard the
# commit-window --after/--before bounds (C1's regression net is T5/T6 in
# coordinator/tests/test_workday_evening_tz_coherence.py).
# Coverage note: emit-cockpit-snapshot.py (C-A4) is intentionally omitted —
# it uses coordinator_local_day but is not one of the original 6 day-anchor sites.
# ---------------------------------------------------------------------------
echo ""
echo "=== AC2: anchor-sensitive sites do NOT use date -u for day-anchor assignments ==="

_ANCHOR_SITES=(
    "$D/bin/workday-complete-step9-append-changelog.py"
    "$D/bin/workday-complete-step3-consolidate.py"
    "$D/bin/workday-complete-backfill-scan.py"
    "$D/bin/workday-start-step0.py"
    "$D/hooks/scripts/plan-persistence-check.sh"
    "$D/bin/workweek-trail-scope.py"
)

_DAY_ANCHOR_PATTERN='=.*date -u \+(%Y-%m-%d|%F)'

for _site in "${_ANCHOR_SITES[@]}"; do
    _basename="$(basename "$_site")"
    if [[ ! -f "$_site" ]]; then
        fail "AC2: site file not found — $_basename (path: $_site)"
        continue
    fi
    _hits="$(grep -nE "$_DAY_ANCHOR_PATTERN" "$_site" 2>/dev/null || true)"
    if [[ -z "$_hits" ]]; then
        pass "AC2: ${_basename} — no day-anchor date -u assignment found"
    else
        fail "AC2: ${_basename} — still contains date -u day-anchor assignment(s):"
        while IFS= read -r _hit_line; do
            echo "      $_hit_line" >&2
        done <<< "$_hits"
    fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test-datetime-anchor-coherence.sh summary"
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo "========================================"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
