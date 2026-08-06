#!/usr/bin/env bash
# test-coordinator-session-loe.sh — Exercise coordinator-session-loe.sh read helper.
#
# Spec backlink: docs/plans/2026-05-19-completion-log-phase2-loe-and-handoff-ledger.md
# § Chunk 2 — AC: correct three numbers + t-shirt; missing token metadata degrades
# gracefully; YAML-frontmatter output paste-ready; concurrency declarations present.
#
# Run: bash ~/.claude/plugins/coordinator-claude/coordinator/bin/tests/test-coordinator-session-loe.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOE="${SCRIPT_DIR}/../coordinator-session-loe.py"

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

PASS=0
FAIL=0
FAIL_MSGS=()

pass() { echo "  PASS: $1"; (( PASS++ )) || true; }
fail() {
  echo "  FAIL: $1"
  FAIL_MSGS+=("$1")
  (( FAIL++ )) || true
}

run_test() {
  local name="$1"
  local fn="$2"
  echo "--- $name"
  if "$fn"; then
    pass "$name"
  else
    fail "$name"
  fi
}

# ---------------------------------------------------------------------------
# Fixture setup
# ---------------------------------------------------------------------------

TMPDIR_BASE=$(mktemp -d 2>/dev/null || mktemp -d -t loe-test)
trap 'rm -rf "$TMPDIR_BASE"' EXIT

# Create a fake git repo so git rev-parse works
FAKE_REPO="${TMPDIR_BASE}/repo"
mkdir -p "${FAKE_REPO}/.git"
git -C "$FAKE_REPO" init -q 2>/dev/null || true

SESSIONS_BASE="${FAKE_REPO}/.git/coordinator-sessions"
mkdir -p "$SESSIONS_BASE"

# Create a fixture session
make_session() {
  local sid="$1"
  local session_dir="${SESSIONS_BASE}/${sid}"
  mkdir -p "$session_dir"
  echo "$sid" > "${SESSIONS_BASE}/.current-session-id"
  echo "$session_dir"
}

# Write fixture dispatched-agents.txt with tab-delimited records
write_agents() {
  local session_dir="$1"
  shift
  local agents_file="${session_dir}/dispatched-agents.txt"
  : > "$agents_file"
  for rec in "$@"; do
    echo -e "$rec" >> "$agents_file"
  done
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_xs_session() {
  local sid
  sid="fixture-xs-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # 2 dispatches, 0 opus, no tokens.
  # NOTE: XS is structurally unreachable under "any-criterion" semantics because
  # S.opus_dispatches=0, so 0>=0 qualifies every session for S.  The spec worked
  # example claims XS here but the threshold table makes it impossible — see plan F8.
  # This test verifies XS (the correct result given the threshold table).
  write_agents "$sdir" \
    "aaaaaa0000000001\tsonnet\texecutor" \
    "aaaaaa0000000002\tsonnet\texecutor"

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": 2'* ]] || { echo "    agent_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 0'* ]] || { echo "    opus_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"em_tokens": null'* ]]     || { echo "    em_tokens should be null: $out"; return 1; }
  [[ "$out" == *'"tshirt": "XS"'* ]]        || { echo "    tshirt should be XS (2<5 agent, 0<1 opus, null<150k tokens): $out"; return 1; }
}

test_m_session() {
  local sid
  sid="fixture-m-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # 18 dispatches, 2 opus -> M (18>=15 on agent_dispatches, 2>=1 on opus)
  write_agents "$sdir" \
    "aaaaaa0000000001\topus\texecutor" \
    "aaaaaa0000000002\topus-3-5\texecutor" \
    "aaaaaa0000000003\tsonnet\texecutor" \
    "aaaaaa0000000004\tsonnet\texecutor" \
    "aaaaaa0000000005\tsonnet\texecutor" \
    "aaaaaa0000000006\tsonnet\texecutor" \
    "aaaaaa0000000007\tsonnet\texecutor" \
    "aaaaaa0000000008\tsonnet\texecutor" \
    "aaaaaa0000000009\tsonnet\texecutor" \
    "aaaaaa0000000010\tsonnet\texecutor" \
    "aaaaaa0000000011\tsonnet\texecutor" \
    "aaaaaa0000000012\tsonnet\texecutor" \
    "aaaaaa0000000013\tsonnet\texecutor" \
    "aaaaaa0000000014\tsonnet\texecutor" \
    "aaaaaa0000000015\tsonnet\texecutor" \
    "aaaaaa0000000016\tsonnet\texecutor" \
    "aaaaaa0000000017\tsonnet\texecutor" \
    "aaaaaa0000000018\tsonnet\texecutor"

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": 18'* ]] || { echo "    agent_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 2'* ]]   || { echo "    opus_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"tshirt": "M"'* ]]          || { echo "    tshirt should be M: $out"; return 1; }
}

test_l_session_via_opus() {
  local sid
  sid="fixture-l-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # 10 dispatches, 4 opus -> L (4>=3 on opus, but 10<30 on agent_dispatches)
  write_agents "$sdir" \
    "aaaaaa0000000001\topus\texecutor" \
    "aaaaaa0000000002\topus\texecutor" \
    "aaaaaa0000000003\topus\texecutor" \
    "aaaaaa0000000004\topus\texecutor" \
    "aaaaaa0000000005\tsonnet\texecutor" \
    "aaaaaa0000000006\tsonnet\texecutor" \
    "aaaaaa0000000007\tsonnet\texecutor" \
    "aaaaaa0000000008\tsonnet\texecutor" \
    "aaaaaa0000000009\tsonnet\texecutor" \
    "aaaaaa0000000010\tsonnet\texecutor"

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"opus_dispatches": 4'* ]] || { echo "    opus_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"tshirt": "L"'* ]]        || { echo "    tshirt should be L: $out"; return 1; }
}

test_legacy_format() {
  # Legacy bare-agentId records (no tabs) — must parse without error and
  # count as dispatches with 0 Opus (model field absent -> unknown, not opus).
  local sid
  sid="fixture-legacy-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  cat > "${sdir}/dispatched-agents.txt" <<'EOF'
a1234567890abcdef
a9876543210fedcba
afedcba9876543210
a0123456789abcdef
a0987654321fedcba
a0f1e2d3c4b5a6a7
EOF

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": 6'* ]] || { echo "    agent_dispatches wrong (legacy): $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 0'* ]]  || { echo "    opus_dispatches should be 0 (legacy): $out"; return 1; }
  [[ "$out" == *'"tshirt": "S"'* ]]         || { echo "    tshirt should be S (6>=5): $out"; return 1; }
}

test_empty_session() {
  local sid
  sid="fixture-empty-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # No dispatched-agents.txt at all.
  # C3 null-honesty (2026-06-30): absent file → null (not 0), tshirt = "null" (not "XS").
  # A present-but-empty file → 0 (see test_present_empty_file_zero below).
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": null'* ]] || { echo "    agent_dispatches should be null (absent file, C3): $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": null'* ]]  || { echo "    opus_dispatches should be null (absent file, C3): $out"; return 1; }
  [[ "$out" == *'"tshirt": "null"'* ]]         || { echo "    tshirt should be null (absent file = no data, C3): $out"; return 1; }
}

# AC4 (null honesty) — present-but-EMPTY file → integer 0, not null
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4
test_present_empty_file_zero() {
  local sid
  sid="fixture-empty-file-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # Present-but-empty dispatched-agents.txt (0 bytes): real zero, not null
  : > "${sdir}/dispatched-agents.txt"
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": 0'* ]] || { echo "    agent_dispatches should be 0 (empty file): $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 0'* ]]  || { echo "    opus_dispatches should be 0 (empty file): $out"; return 1; }
}

# AC4 (null honesty) — absent file emits literal null in yaml-frontmatter
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4
test_absent_file_yaml_null() {
  local sid
  sid="fixture-absent-yaml-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format yaml-frontmatter 2>/dev/null)
  [[ "$out" == *"agent_dispatches: null"* ]] || { echo "    agent_dispatches should be null in yaml: $out"; return 1; }
  [[ "$out" == *"opus_dispatches: null"* ]]  || { echo "    opus_dispatches should be null in yaml: $out"; return 1; }
  [[ "$out" == *'tshirt: "null"'* ]]         || { echo "    tshirt should be null in yaml: $out"; return 1; }
}

# AC4 (null honesty) — absent file emits literal null in tsv (all three absent-data columns)
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4
test_absent_file_tsv_null() {
  local sid
  sid="fixture-absent-tsv-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format tsv 2>/dev/null)
  col2=$(printf '%s' "$out" | cut -f2)
  col3=$(printf '%s' "$out" | cut -f3)
  col5=$(printf '%s' "$out" | cut -f5)
  [[ "$col2" == "null" ]] || { echo "    TSV col2 (agent_dispatches) should be null, got: '$col2' from: $out"; return 1; }
  [[ "$col3" == "null" ]] || { echo "    TSV col3 (opus_dispatches) should be null, got: '$col3' from: $out"; return 1; }
  [[ "$col5" == "null" ]] || { echo "    TSV col5 (tshirt) should be null, got: '$col5' from: $out"; return 1; }
}

test_yaml_frontmatter_format() {
  local sid
  sid="fixture-yaml-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  write_agents "$sdir" \
    "aaaaaa0000000001\tsonnet\texecutor" \
    "aaaaaa0000000002\tsonnet\texecutor" \
    "aaaaaa0000000003\tsonnet\texecutor" \
    "aaaaaa0000000004\tsonnet\texecutor" \
    "aaaaaa0000000005\tsonnet\texecutor"

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format yaml-frontmatter 2>/dev/null)
  # Must start with "loe:" and contain the expected keys
  [[ "$out" == loe:* ]]                        || { echo "    must start with loe:: $out"; return 1; }
  [[ "$out" == *"agent_dispatches: 5"* ]]      || { echo "    agent_dispatches missing: $out"; return 1; }
  [[ "$out" == *"opus_dispatches: 0"* ]]       || { echo "    opus_dispatches missing: $out"; return 1; }
  [[ "$out" == *"em_tokens: null"* ]]          || { echo "    em_tokens should be null: $out"; return 1; }
  [[ "$out" == *"tshirt:"* ]]                  || { echo "    tshirt missing: $out"; return 1; }
}

test_tsv_format() {
  local sid
  sid="fixture-tsv-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  write_agents "$sdir" \
    "aaaaaa0000000001\tsonnet\texecutor"

  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format tsv 2>/dev/null)
  # TSV: session_id  agent_dispatches  opus_dispatches  em_tokens  tshirt
  fields=$(echo "$out" | awk -F'\t' '{print NF}')
  [[ "$fields" -eq 5 ]] || { echo "    TSV should have 5 fields, got $fields: $out"; return 1; }
  col1=$(echo "$out" | cut -f1)
  [[ "$col1" == "$sid" ]] || { echo "    col1 should be session_id: $col1"; return 1; }
}

test_concurrency_declarations_present() {
  # Verify the script header contains required concurrency/idempotency/resume declarations
  grep -q "Concurrency posture:" "$LOE" || { echo "    Missing 'Concurrency posture:' declaration"; return 1; }
  grep -q "Idempotency posture:" "$LOE" || { echo "    Missing 'Idempotency posture:' declaration"; return 1; }
  grep -q "Resume strategy:" "$LOE"     || { echo "    Missing 'Resume strategy:' declaration"; return 1; }
}

test_token_degrade_graceful() {
  # When env vars absent (default test env), em_tokens should be null, not an error
  local sid
  sid="fixture-degrade-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  write_agents "$sdir" "aaaaaa0000000001\tsonnet\texecutor"

  # Explicitly unset the speculative env vars to simulate their absence
  out=$(cd "$FAKE_REPO" || exit 1; unset CLAUDE_SESSION_INPUT_TOKENS CLAUDE_SESSION_OUTPUT_TOKENS 2>/dev/null; \
        python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  exit_code=$?
  [[ $exit_code -eq 0 ]]                  || { echo "    non-zero exit on missing tokens: $exit_code"; return 1; }
  [[ "$out" == *'"em_tokens": null'* ]]   || { echo "    em_tokens should degrade to null: $out"; return 1; }
}

# AC8 — opus-matcher fix: grep -ci opus matches real claude-opus-4-8[1m] string (C6 fix)
# A ^opus prefix-match never fires on a real model string; -ci opus (substring) does.
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C6/AC8
test_opus_real_model_string() {
  local sid
  sid="fixture-opus-real-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  write_agents "$sdir" \
    "aaaaaa0000000001\tclaude-opus-4-8[1m]\tcoordinator:executor"
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null)
  [[ "$out" == *'"agent_dispatches": 1'* ]] || { echo "    agent_dispatches wrong: $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 1'* ]]  || { echo "    opus_dispatches should be 1 for claude-opus-4-8[1m] (AC8): $out"; return 1; }
}

# AC7 (consumer-tolerance) — teammate-id row (name@session-<short>) in dispatched-agents.txt
# does not break wc -l (total count) or cut -f2 (model column extraction).
# The @ and - chars in column-1 are id-agnostic for the LOE counter.
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4/AC7
test_teammate_id_row_tolerance() {
  local sid
  sid="fixture-teammate-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  # Real probe fixture: probe2-teammate@session-8a71114e dispatched with claude-opus-4-8[1m]
  write_agents "$sdir" \
    "probe2-teammate@session-8a71114e\tclaude-opus-4-8[1m]\tcoordinator:executor"
  local exit_code=0
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$sid" --format json 2>/dev/null) || exit_code=$?
  [[ $exit_code -eq 0 ]] || { echo "    LOE counter exited $exit_code on teammate-id row; should exit 0"; return 1; }
  [[ "$out" == *'"agent_dispatches": 1'* ]] || { echo "    agent_dispatches should be 1 (teammate row counted by wc -l): $out"; return 1; }
  [[ "$out" == *'"opus_dispatches": 1'* ]]  || { echo "    opus_dispatches should be 1 (cut -f2 extracted claude-opus-4-8[1m]): $out"; return 1; }
}

# AC7 (consumer-tolerance extension) — classify-dispatch-shape.py tolerates a
# dispatched-agents.txt containing teammate-id rows (if invocable from this test).
# Exit 0 = no error even with @/- chars in column-1.
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4/AC7
test_classify_dispatch_teammate_tolerance() {
  local CLASSIFY_SCRIPT="${SCRIPT_DIR}/../classify-dispatch-shape.py"
  if [[ ! -f "$CLASSIFY_SCRIPT" ]]; then
    echo "    classify-dispatch-shape.py not found — skipping (not invocable)"
    return 0
  fi

  local sid
  sid="fixture-classify-tm-$(date +%s)"
  local sdir; sdir=$(make_session "$sid")
  write_agents "$sdir" \
    "probe2-teammate@session-8a71114e\tclaude-opus-4-8[1m]\tcoordinator:executor" \
    "aaaaaa0000000002\tclaude-sonnet-4-5\tcoordinator:executor"

  # Dummy plan with >=2 non-deferred ## Tasks spine rows so classify proceeds to read dispatched-agents.txt.
  # Repointed from a ## Dispatch Ledger table to the ## Tasks spine when classify-dispatch-shape.py's input
  # source moved off the retired plan-body ledger (plan 2026-07-13-retire-plan-body-dispatch-ledger, C8/C12).
  # Placed inside FAKE_REPO so git -C "$(dirname PLAN_FILE)" resolves to FAKE_REPO's .git.
  local plan_file="${FAKE_REPO}/dummy-classify-plan.md"
  cat > "$plan_file" <<'PLAN_EOF'
# Dummy Plan (classify-dispatch tolerance fixture)

## Tasks

```yaml plan-tasks
- id: C1
  deferred: false
- id: C2
  deferred: false
```
PLAN_EOF

  local exit_code=0
  # CLAUDE_CODE_SESSION_ID directs classify to our test session dir (F4 in classify spec)
  (cd "$FAKE_REPO" || exit 1; CLAUDE_CODE_SESSION_ID="$sid" python "$CLASSIFY_SCRIPT" --plan-file "$plan_file" 2>/dev/null) \
    || exit_code=$?
  # classify-dispatch-shape.py exits 0 on all paths (advisory tool — OFFER SHAPE: exit 0 always)
  [[ $exit_code -eq 0 ]] || { echo "    classify-dispatch-shape.py exited $exit_code on teammate-id row (should exit 0)"; return 1; }
}

# the Staff Engineer F4 (include-children absent-parent) — a parent session with an ABSENT
# dispatched-agents.txt summed via --include-children with present children:
# the absent parent is NOT coerced to 0; children's count is correctly reflected.
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C4/AC4
test_include_children_absent_parent() {
  local ts
  ts=$(date +%s)
  local parent_sid="fixture-f4parent-${ts}"
  local child_sid="fixture-f4child-${ts}"

  # Parent session: directory exists but NO dispatched-agents.txt (absent)
  make_session "$parent_sid" > /dev/null
  # (make_session writes .current-session-id and creates the dir; no agents file)

  # Child EM session: has 3 dispatches
  local child_dir="${SESSIONS_BASE}/${child_sid}"
  mkdir -p "$child_dir"
  write_agents "$child_dir" \
    "aaaaaa000f400001\tsonnet\tcoordinator:executor" \
    "aaaaaa000f400002\tsonnet\tcoordinator:executor" \
    "aaaaaa000f400003\tsonnet\tcoordinator:executor"

  # Backpointer: .agents/<backptr-id>/em-session-id.txt = child_sid
  # This simulates: child EM session (child_sid) dispatched some agent, so the
  # --include-children loop finds child_sid via the backpointer and sums its counts.
  local agents_dir="${SESSIONS_BASE}/.agents"
  local backptr_dir="${agents_dir}/backptr-f4-${ts}"
  mkdir -p "$backptr_dir"
  echo "${child_sid}" > "${backptr_dir}/em-session-id.txt"

  local exit_code=0
  out=$(cd "$FAKE_REPO" || exit 1; python3 "$LOE" --session-id "$parent_sid" --include-children --format json 2>/dev/null) \
    || exit_code=$?
  [[ $exit_code -eq 0 ]] || { echo "    LOE exited $exit_code with absent parent + children: $out"; return 1; }
  # Absent parent contributes null (not 0); children contribute 3 dispatches.
  # Result must be 3 (children's sum), not null (children present) and not 0 (parent absent, not coerced).
  [[ "$out" == *'"agent_dispatches": 3'* ]] || { echo "    agent_dispatches should be 3 (child sum; absent parent not coerced to 0, F4): $out"; return 1; }
  # Review: code-reviewer Slice-C — (F2/F3) opus_dispatches assertion proves symmetric OPUS accumulation ran with absent parent;
  # without it both the bug (parent coerced to 0 → 0+0=0 opus) and the fix (parent null → child-only 0) produce 0, so the
  # assertion alone is discriminating only via the agent_dispatches above.  Adding it validates the full accumulation path.
  [[ "$out" == *'"opus_dispatches": 0'* ]] || { echo "    opus_dispatches should be 0 (child has no opus; validates symmetric accumulation with absent parent, F2/F3): $out"; return 1; }
  # Tshirt must not be "null" (children qualify)
  [[ "$out" != *'"tshirt": "null"'* ]] || { echo "    tshirt should not be null (children have dispatches, F4): $out"; return 1; }
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

echo "=== coordinator-session-loe.sh tests ==="
run_test "XS session (2 dispatches, 0 opus)"     test_xs_session
run_test "M session (18 dispatches, 2 opus)"     test_m_session
run_test "L session via opus count (4 opus)"     test_l_session_via_opus
run_test "Legacy bare-agentId format"            test_legacy_format
run_test "Empty session (absent agents file → null)" test_empty_session
run_test "yaml-frontmatter output format"        test_yaml_frontmatter_format
run_test "tsv output format"                     test_tsv_format
run_test "Concurrency declarations present"      test_concurrency_declarations_present
run_test "Token degrade graceful (em_tokens=null)" test_token_degrade_graceful
# C4 additions (AC4 null-honesty, AC8 opus-matcher, AC7 consumer-tolerance, F4 include-children)
run_test "AC4 present-but-empty file → 0 (not null)"         test_present_empty_file_zero
run_test "AC4 absent file → null in yaml-frontmatter"        test_absent_file_yaml_null
run_test "AC4 absent file → null in tsv"                     test_absent_file_tsv_null
run_test "AC8 claude-opus-4-8[1m] model string → opus=1"    test_opus_real_model_string
run_test "AC7 teammate-id row (@/-) tolerated by LOE counter" test_teammate_id_row_tolerance
run_test "AC7 classify-dispatch-shape tolerates teammate-id" test_classify_dispatch_teammate_tolerance
run_test "F4 include-children absent-parent not coerced to 0" test_include_children_absent_parent

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ ${#FAIL_MSGS[@]} -gt 0 ]]; then
  echo "Failed tests:"
  for msg in "${FAIL_MSGS[@]}"; do
    echo "  - $msg"
  done
  exit 1
fi
exit 0
