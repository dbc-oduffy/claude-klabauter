#!/usr/bin/env bash
# test-audit-roadmap-dependency-order.sh — Regression net for audit-roadmap.py Audit 5
# (dependency-order invariant gate). Backfills first test coverage for audit-roadmap.py.
#
# Purpose: asserts EXIT CODE, not just grep-on-output, so the old pipe-into-subshell
# bug (fail() in a subshell, EXIT_CODE lost, dead gate) would have been caught here.
# The process-substitution fix is the subject; this test is the net.
#
# Spec backlink: docs/plans/2026-06-28-roadmap-stub-numbering-dependency-order.md § C6, AC4
#
# Fixture strategy: real-tree with trap cleanup — stub files are written to
# state/handoffs/ under PID-scoped roadmap_id values to avoid collision with live data,
# then deleted by the trap on EXIT regardless of assertion outcome. git status must
# be clean after the test runs.
#
# Run: bash ~/.claude/plugins/coordinator-claude/coordinator/bin/tests/test-audit-roadmap-dependency-order.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT="${SCRIPT_DIR}/../audit-roadmap.py"

# ---------------------------------------------------------------------------
# Preflight: node and git root required
# ---------------------------------------------------------------------------

if ! command -v node >/dev/null 2>&1; then
  echo "SKIP: node not found — audit-roadmap.py requires node; skipping."
  exit 0
fi

ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) || {
  echo "SKIP: could not resolve git root from $SCRIPT_DIR; skipping."
  exit 0
}

HANDOFFS_DIR="${ROOT}/state/handoffs"
ROADMAP_DIR="${ROOT}/state/roadmap"

if [ ! -d "$HANDOFFS_DIR" ]; then
  echo "SKIP: ${HANDOFFS_DIR} does not exist — not a coordinator repo; skipping."
  exit 0
fi

# audit-roadmap.py queries its DATA root (default $(coordinator_claude_klabauter_root)) for stubs, but
# accepts --root <dir> to override it (strang-11 follow-up). This test writes its fixtures into
# $ROOT/state/handoffs and reconciliation.md into $(coordinator_state_root)/roadmap (== $ROOT/state
# here), so we run the audit with --root "$ROOT" to point its stub query at the local fixture tree.

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

PASS_COUNT=0
FAIL_COUNT=0

pass_t() { echo "  PASS: $1"; (( PASS_COUNT++ )) || true; }
fail_t() { echo "  FAIL: $1" >&2; (( FAIL_COUNT++ )) || true; }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    pass_t "$label"
  else
    fail_t "$label — expected='$expected' got='$actual'"
  fi
}

assert_contains() {
  local label="$1" pattern="$2" content="$3"
  if printf '%s' "$content" | grep -qF "$pattern"; then
    pass_t "$label"
  else
    fail_t "$label — pattern not found: '$pattern'"
  fi
}

# ---------------------------------------------------------------------------
# Fixtures — real-tree strategy with PID-scoped run-ids + trap cleanup
# ---------------------------------------------------------------------------

PID="$$"
GOOD_ID="zzz-c6b-dep-good-${PID}"
INV_ID="zzz-c6b-dep-inv-${PID}"
CYC_ID="zzz-c6b-dep-cyc-${PID}"

TMPOUT=$(mktemp)
TMPERR=$(mktemp)

cleanup() {
  rm -f \
    "${HANDOFFS_DIR}/${GOOD_ID}-1.md" \
    "${HANDOFFS_DIR}/${GOOD_ID}-2.md" \
    "${HANDOFFS_DIR}/${INV_ID}-1.md" \
    "${HANDOFFS_DIR}/${INV_ID}-2.md" \
    "${HANDOFFS_DIR}/${CYC_ID}-1.md" \
    "${HANDOFFS_DIR}/${CYC_ID}-2.md" \
    "$TMPOUT" "$TMPERR" \
    2>/dev/null || true
  rm -rf \
    "${ROADMAP_DIR}/${GOOD_ID}" \
    "${ROADMAP_DIR}/${INV_ID}" \
    "${ROADMAP_DIR}/${CYC_ID}" \
    2>/dev/null || true
  # Review: code-reviewer (F7) — if state/roadmap/ didn't exist before the test, rm -rf
  # of the subdirs leaves an empty directory behind (untracked in git status).  rmdir
  # fails silently when non-empty (pre-existing populated dir is safe), so this is always safe.
  rmdir "${ROADMAP_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

# write_stub <file> <roadmap_id> <stub_id> <sprint> <wave> [blocked_by_ids...]
# Creates a minimal spinoff-roadmap stub for Audit 5 consumption.
# Review: code-reviewer (F6) — production stubs carry an explicit `number:` field; Audit 5
# reads fm.number as Number(fm.number).  Without it the test exercised the derived-from-stub_id
# path only.  Extract the trailing -<N> from stub_id and emit it as the explicit `number:` so
# Scenarios A/B/C exercise the same production code path.
write_stub() {
  local file="$1" rid="$2" sid="$3" sprint="$4" wave="$5"
  shift 5
  local num="${sid##*-}"  # trailing integer after last '-', e.g. "zzz-c6b-dep-good-12345-2" → "2"
  {
    printf -- '---\n'
    printf 'title: "Test stub %s"\n' "$sid"
    printf 'created: 2026-07-02\n'
    printf 'status: active\n'
    printf 'kind: spinoff-roadmap\n'
    printf 'roadmap_id: %s\n' "$rid"
    printf 'stub_id: %s\n' "$sid"
    printf 'number: %s\n' "$num"
    printf 'sprint: %s\n' "$sprint"
    printf 'wave: %s\n' "$wave"
    if [ "$#" -eq 0 ]; then
      printf 'blocked_by: []\n'
    else
      printf 'blocked_by:\n'
      for dep in "$@"; do
        printf '  - %s\n' "$dep"
      done
    fi
    printf -- '---\n'
    printf '\n'
  } > "$file"
}

# write_reconciliation <run_id> <stub_count>
# Supplies the minimal reconciliation.md Audit 1 needs so it does not fail
# and contaminate the exit code. Each stub counts as one KEEP verdict.
write_reconciliation() {
  local rid="$1" count="$2" i=0
  mkdir -p "${ROADMAP_DIR}/${rid}"
  {
    while [ "$i" -lt "$count" ]; do
      printf 'Verdict: KEEP\n'
      (( i++ )) || true
    done
  } > "${ROADMAP_DIR}/${rid}/reconciliation.md"
}

# run_audit <run_id> — executes the audit, captures stdout+stderr separately,
# returns the exit code via the AUDIT_RC variable (not subshell capture, which
# would mask the exit code).
run_audit() {
  local run_id="$1"
  # Invoke via `bash` (not direct exec) — audit-roadmap.py is mode 100644 in git; a direct
  # "$AUDIT" call depends on a working-tree exec bit git does not preserve across clones.
  python "$AUDIT" "$run_id" --root "$ROOT" > "$TMPOUT" 2> "$TMPERR"
  AUDIT_RC=$?
  AUDIT_STDOUT=$(cat "$TMPOUT")
  AUDIT_STDERR=$(cat "$TMPERR")
}

# ---------------------------------------------------------------------------
# Scenario A: Good roadmap — dependency-order invariant satisfied
#
#   stub ${GOOD_ID}-1  N=1, sprint=1, wave=1  (no blocked_by)
#   stub ${GOOD_ID}-2  N=2, sprint=1, wave=2  blocked_by [${GOOD_ID}-1]
#
#   Checks: number(dep=1) < number(dep'ent=2) AND (s=1,w=1) <_lex (s=1,w=2) — both pass.
#   Expected: Audit 5 PASS, overall exit 0.
#
# Note: reconciliation.md (2 KEEP verdicts) is provided so Audit 1 also passes
# and the clean exit-0 is entirely attributable to Audit 5 (not Audit 1 masking it).
# ---------------------------------------------------------------------------

echo ""
echo "--- Scenario A: good roadmap (dependency-ordered, should exit 0)"

write_stub "${HANDOFFS_DIR}/${GOOD_ID}-1.md" "$GOOD_ID" "${GOOD_ID}-1" 1 1
write_stub "${HANDOFFS_DIR}/${GOOD_ID}-2.md" "$GOOD_ID" "${GOOD_ID}-2" 1 2 "${GOOD_ID}-1"
write_reconciliation "$GOOD_ID" 2

AUDIT_RC=0 AUDIT_STDOUT="" AUDIT_STDERR=""
run_audit "$GOOD_ID"

# AC4.1 — exit code 0 on a clean roadmap
assert_eq "A-1: exit code 0 on good roadmap" "0" "$AUDIT_RC"
# AC4.1 — Audit 5 emits its PASS line to stdout
assert_contains "A-2: stdout contains Audit 5 PASS" "PASS: Audit 5:" "$AUDIT_STDOUT"

# ---------------------------------------------------------------------------
# Scenario B: Inverted roadmap — lower-number stub blocked_by higher-number stub
#
#   stub ${INV_ID}-1  N=1, sprint=1, wave=2  blocked_by [${INV_ID}-2]
#   stub ${INV_ID}-2  N=2, sprint=1, wave=3  (no blocked_by)
#
#   Violations:
#     number-order:         B._num(2) >= A._num(1)          → violation
#     same-or-inverted-slot: (s=1,w=3) NOT <_lex (s=1,w=2)  → violation
#   Expected: Audit 5 FAIL naming the edge, overall exit 1.
#
#   This is the regression case: the old pipe-into-subshell bug printed FAIL
#   text but returned exit 0 (fail() ran in a subshell, EXIT_CODE lost).
#   The test asserts the EXIT CODE — grepping output alone would pass the bug.
# ---------------------------------------------------------------------------

echo ""
echo "--- Scenario B: inverted roadmap (lower-number blocked_by higher-number, should exit 1)"

write_stub "${HANDOFFS_DIR}/${INV_ID}-1.md" "$INV_ID" "${INV_ID}-1" 1 2 "${INV_ID}-2"
write_stub "${HANDOFFS_DIR}/${INV_ID}-2.md" "$INV_ID" "${INV_ID}-2" 1 3
write_reconciliation "$INV_ID" 2

AUDIT_RC=0 AUDIT_STDOUT="" AUDIT_STDERR=""
run_audit "$INV_ID"

# AC4.2 — exit code 1 on inverted roadmap (must be exit code assertion, not just text)
assert_eq "B-1: exit code 1 on inverted roadmap" "1" "$AUDIT_RC"
# AC4.2 — Audit 5 emits a FAIL line naming the violation
assert_contains "B-2: stderr has Audit 5 violation message" \
  "Audit 5: dependency-order violation" "$AUDIT_STDERR"
# AC4.2 — the FAIL message names the offending edge (from stub present in stderr)
assert_contains "B-3: stderr names the inverted stub id" "${INV_ID}-1" "$AUDIT_STDERR"

# ---------------------------------------------------------------------------
# Scenario C: Cyclic roadmap — blocked_by graph contains a cycle
#
#   stub ${CYC_ID}-1  blocked_by [${CYC_ID}-2]
#   stub ${CYC_ID}-2  blocked_by [${CYC_ID}-1]
#
#   Cycle: ${CYC_ID}-1 → ${CYC_ID}-2 → ${CYC_ID}-1
#   Expected: Audit 5 reports cycle, overall exit 1.
# ---------------------------------------------------------------------------

echo ""
echo "--- Scenario C: cyclic roadmap (mutual blocked_by, should exit 1)"

write_stub "${HANDOFFS_DIR}/${CYC_ID}-1.md" "$CYC_ID" "${CYC_ID}-1" 1 1 "${CYC_ID}-2"
write_stub "${HANDOFFS_DIR}/${CYC_ID}-2.md" "$CYC_ID" "${CYC_ID}-2" 1 2 "${CYC_ID}-1"
write_reconciliation "$CYC_ID" 2

AUDIT_RC=0 AUDIT_STDOUT="" AUDIT_STDERR=""
run_audit "$CYC_ID"

# AC4.3 — exit code 1 on cyclic roadmap
assert_eq "C-1: exit code 1 on cyclic roadmap" "1" "$AUDIT_RC"
# AC4.3 — Audit 5 reports the cycle with the canonical phrase
assert_contains "C-2: stderr has cycle detection message" \
  "dependency cycle detected" "$AUDIT_STDERR"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "test-audit-roadmap-dependency-order: ALL PASS"
  exit 0
else
  echo "test-audit-roadmap-dependency-order: FAIL" >&2
  exit 1
fi
