#!/usr/bin/env bash
# run-lineage-dag-suites.sh — AC10 aggregator for the handoff-lineage-DAG +
# chain-review-coverage workstreams. Runs all suites that exercise the DAG
# primitive, the live-children guard, and the review-coverage gate, AND-combined
# (any suite failing fails the whole run). Single entry point cited by
# docs/plans/2026-06-30-chain-review-coverage-dag-consumer.md AC10.
#
# Suites:
#   1. node    — bin/lib/walk-handoff-dag.test.js          (referencedBy/walkForward + exclude)
#   2. python  — bin/tests/test_handoff_has_live_children.py   (exclude + edge-kinds + exit-2)
#   3. bats    — bin/tests/test-review-coverage-gate.bats        (DAG-mode walk/topology)
#   4. bats    — bin/tests/test-review-coverage-gate-derivation.bats (segment derivation/guards/failure)
#
# bats is invoked via `npx bats` (no global install required; bats >= 1.x). Suite 2 was
# ported off bats to native Python in the 2026-07-19 Windows de-bash campaign (W1b) —
# handoff-has-live-children.sh (its subject) was deleted, no bash left on this suite's path.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # bin/tests
BIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"                     # bin

rc=0
run() {
  local label="$1"; shift
  echo "=== ${label} ==="
  if "$@"; then
    echo "--- ${label}: PASS"
  else
    echo "--- ${label}: FAIL" >&2
    rc=1
  fi
}

run "node: walk-handoff-dag" node --test "${BIN_DIR}/lib/walk-handoff-dag.test.js"
run "python: handoff-has-live-children" python3 "${SCRIPT_DIR}/test_handoff_has_live_children.py"
run "bats: review-coverage-gate" npx bats "${SCRIPT_DIR}/test-review-coverage-gate.bats"
run "bats: review-coverage-gate-derivation" npx bats "${SCRIPT_DIR}/test-review-coverage-gate-derivation.bats"

if [[ $rc -eq 0 ]]; then
  echo "########## run-lineage-dag-suites: ALL SUITES GREEN ##########"
else
  echo "########## run-lineage-dag-suites: ONE OR MORE SUITES FAILED ##########" >&2
fi
exit $rc
