#!/usr/bin/env bash
# test-terminator-suite-green.sh — faithful AC6 oracle for the
# session-terminator-mechanism-unification plan.
#
# AC6's intent is "no session-terminator skill REGRESSES" — NOT "the entire repo
# run-all-checks suite is green". The full suite is hostage to unrelated state
# (e.g. a pre-existing `validate-references` failure on broken links in other
# projects' memory files and stale doc links, none touched by this plan). This
# scoped oracle runs exactly the session-terminator surface this plan owns +
# the lifecycle-regression guards, and asserts every one exits 0.
#
# Spec: docs/plans/2026-06-30-session-terminator-mechanism-unification.md C8 (AC6).
set -uo pipefail

_cc_root="${CLAUDE_PLUGIN_ROOT:-$(cat "${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/machine-local/.doe-root" 2>/dev/null || cat "${CLAUDE_HOME:-$HOME}/.claude/.doe-root" 2>/dev/null)/coordinator}"
_cc_doe="$(cat "${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/machine-local/.doe-root" 2>/dev/null || true)"
if [ -z "$_cc_doe" ]; then
  _cc_doe="$(cat "${CLAUDE_HOME:-$HOME}/.claude/.doe-root" 2>/dev/null || true)"
fi
_cc_doe="${_cc_doe%/}"   # trailing-slash normalization: prevents //* false-reject on stale/hand-edited .doe-root
_cc_trusted=0
case "$_cc_root" in
  "${CLAUDE_HOME:-$HOME}/.claude/"*) _cc_trusted=1 ;;
esac
[ -n "$_cc_doe" ] && case "$_cc_root" in "$_cc_doe"/*) _cc_trusted=1 ;; esac
case "$_cc_root" in *"/.."*) _cc_trusted=0 ;; esac   # load-bearing traversal check; dotdot-prefixed name (e.g. ..cache) is accepted false-reject edge
[ "${COORDINATOR_PLUGIN_ROOT_TRUSTED:-}" = 1 ] && _cc_trusted=1   # sanctioned --plugin-dir spike opt-out
[ "$_cc_trusted" = 1 ] || { echo "ERROR: coordinator root '$_cc_root' outside trusted prefix — refusing to source; re-run coordinator:install (or set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a sanctioned --plugin-dir spike)" >&2; exit 1; }
[ -d "$_cc_root" ] || { echo "coordinator root missing (doe-root unset?)" >&2; exit 1; }
ROOT="$_cc_root"
cd "$ROOT" || { echo "FAIL: cannot cd to plugin root $ROOT" >&2; exit 2; }

# Each entry is a test this plan must keep green. pytest members run under
# python -m pytest; the rest bash.
# Membership pruned 2026-07-22 (shell-suite liveness triage): test-dirty-tree-gate.sh
# and test-extract-scope-paths.sh re-homed to coordinator/tests/ pytest modules
# (bash-kill campaign); test-handoff-transition-consume.js deleted with its dead
# subject handoff-transition.js at 6c979c1f (depolyglot: dead JS oracles).
BASH_TESTS=(
  bin/tests/test-resolve-session-id.sh        # C1 / AC1
  bin/tests/test-d1-same-commit.sh            # C1 / AC2
  bin/tests/test-step-number-stability.sh     # C8 / AC7
  bin/tests/test-no-residual-divergence.sh    # C8 / AC8
)
PYTEST_TESTS=(
  tests/test_dirty_tree_gate.py               # C2 / AC3 (re-homed from test-dirty-tree-gate.sh)
  tests/test_extract_scope_paths.py           # C4 / AC5 (re-homed from test-extract-scope-paths.sh)
)

fails=0
for t in "${BASH_TESTS[@]}"; do
  if [[ ! -f "$t" ]]; then echo "  FAIL: missing $t"; fails=$((fails+1)); continue; fi
  if bash "$t" >/dev/null 2>&1; then echo "  PASS: $t"; else echo "  FAIL: $t (exit $?)"; fails=$((fails+1)); fi
done
_py="$(command -v python3 || command -v python)"
for t in "${PYTEST_TESTS[@]}"; do
  if [[ ! -f "$t" ]]; then echo "  FAIL: missing $t"; fails=$((fails+1)); continue; fi
  if [ -z "$_py" ]; then echo "  FAIL: $t (no python3/python in PATH)"; fails=$((fails+1)); continue; fi
  if "$_py" -m pytest -q "$t" >/dev/null 2>&1; then echo "  PASS: $t"; else echo "  FAIL: $t (exit $?)"; fails=$((fails+1)); fi
done

echo ""
if [[ $fails -gt 0 ]]; then
  echo "Session-terminator suite: $fails FAILED — regression in this plan's surface."
  exit 1
fi
echo "Session-terminator suite: all green (no regression in this plan's surface)."
exit 0
