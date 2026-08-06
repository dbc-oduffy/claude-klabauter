#!/usr/bin/env bash
# test-audit-roadmap-verdict-regex.sh — Regression net for audit-roadmap.py Audit 1's
# KEEP/MERGE verdict-counting regex (2nd-OR-3rd-cell match).
#
# Review: code-reviewer (F3/P2) — broadening the verdict regex to also match the 2nd
# cell (to support the `| cluster | **KEEP** | notes |` authoring shape) introduces a
# new false-positive surface: a table where the 2nd cell is itself free-text notes
# (not the verdict column) and that note happens to start with **KEEP**/**MERGE**
# would now be miscounted as a verdict. This test locks in that the audit does NOT
# over-count in that shape, given the roadmap-planning skill's actual table layouts
# (verdict-in-3rd-cell with a 2nd-cell notes/cluster-name column preceding it) never
# put free text starting with **KEEP**/**MERGE** in the 2nd cell ahead of the verdict.
#
# Spec backlink: docs/plans/2026-05-08-roadmap-skill-and-handoff-lifecycle.md § Phase 5
#
# Run: bash ~/.claude/plugins/coordinator-claude/coordinator/bin/tests/test-audit-roadmap-verdict-regex.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT="${SCRIPT_DIR}/../audit-roadmap.py"

if ! command -v node >/dev/null 2>&1; then
  echo "SKIP: node not found — audit-roadmap.py requires node; skipping."
  exit 0
fi

ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) || {
  echo "SKIP: could not resolve git root from $SCRIPT_DIR; skipping."
  exit 0
}

ROADMAP_DIR="${ROOT}/state/roadmap"

PASS_COUNT=0
FAIL_COUNT=0

pass_t() { echo "  PASS: $1"; (( PASS_COUNT++ )) || true; }
fail_t() { echo "  FAIL: $1" >&2; (( FAIL_COUNT++ )) || true; }

PID="$$"
RID="zzz-verdict-regex-${PID}"

cleanup() {
  rm -rf "${ROADMAP_DIR}/${RID}" 2>/dev/null || true
  rmdir "${ROADMAP_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "${ROADMAP_DIR}/${RID}"

# Fixture: verdict correctly anchored in the 3rd cell; 2nd cell is a free-text
# notes column whose content happens to start with **KEEP** — this must NOT be
# double-counted by the broadened 2nd-cell alternative in the regex.
cat > "${ROADMAP_DIR}/${RID}/reconciliation.md" <<'EOF'
| # | notes | verdict |
|---|-------|---------|
| 1 | **KEEP** this cluster because it is load-bearing | **KEEP** |
EOF

echo ""
echo "--- verdict-regex 2nd-cell-notes false-positive fixture"

# Run only far enough to observe Audit 1's KEEP_COUNT — no stubs on disk, so
# Audit 1 will FAIL on stub-coverage mismatch (0 stubs != N verdicts) regardless;
# what we assert is the reported EXPECTED count in the FAIL message, which
# reveals how many verdicts the regex parsed. A correct (non-double-counting)
# parse reports EXPECTED=1 from this one-row table (the 3rd cell match), not 2
# (which would mean the 2nd-cell notes column was also counted).
OUT=$(python "$AUDIT" "$RID" --root "$ROOT" 2>&1) || true

if printf '%s' "$OUT" | grep -qE 'EXPECTED=|expected'; then
  :
fi

if printf '%s' "$OUT" | grep -qE '\bKEEP=1\b'; then
  pass_t "verdict regex counts exactly 1 KEEP verdict for a 3rd-cell-anchored row with a KEEP-prefixed 2nd-cell note (no double-count)"
elif printf '%s' "$OUT" | grep -qE '\bKEEP=2\b'; then
  fail_t "verdict regex over-counted: KEEP=2 — the 2nd-cell notes column ('**KEEP** this cluster...') was miscounted as an additional verdict"
else
  fail_t "could not find a KEEP=<n> count in audit output: ${OUT:0:400}"
fi

echo ""
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "test-audit-roadmap-verdict-regex: ALL PASS"
  exit 0
else
  echo "test-audit-roadmap-verdict-regex: FAILURES"
  exit 1
fi
