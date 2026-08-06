#!/usr/bin/env bash
# run-plan-tasks-spine-suites.sh — aggregator for the machine-parseable ## Tasks
# task-spine + PM-gated deferral harvest workstream (C1-C7). Runs all
# coordinator-side suites that exercise the pinned task-spine contract and the
# harvest CLI, AND-combined (any suite failing fails the whole run). Single
# entry point for this deliverable's C7 test surface — mirrors the
# run-lineage-dag-suites.sh precedent (topic-scoped multi-suite aggregator).
#
# Suites:
#   1. bash    — test-plan-tasks-schema-enum-parity.sh      (C1 — enum-parity
#                guard: spine change_kind == universal enum; harvest-eligible
#                slice subset-of improvement-queue enum; SSOT citation)
#   2. python3 — test_plan_tasks_spine_and_harvest.py        (C7 — spine parse
#                + parser-locate error states, ledger-derivation exclusion +
#                expansion floor, coverage-checker deferral-ratification
#                schema-conditional proxy, harvest call-site project/central/
#                doctrine-edit routing + idempotency, malformed-row disposition)
#   3. python3 — test_harvest_idempotency_env_override.py    (env-override
#                scan-root/write-root precedence-parity regression test —
#                mismatched-cwd reproduction of the confirmed double-write defect)
#   4. python3 — test_harvest_doe_root_machine_local_leg.py  (machine-local
#                doe_root() registry-leg regression test — Review: code-reviewer
#                slice2 Finding 1 fix coverage)
#
# PORTABILITY: bash >= 4 (DR-148). No GNU-only sed/date/grep flags used.
#
# Spec backlink: docs/plans/2026-07-09-plan-full-coverage-and-deferred-harvest.md § C7
#
# Run: bash coordinator/bin/tests/run-plan-tasks-spine-suites.sh

set -uo pipefail

if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "ERROR: bash >= 4 required. Stock macOS /bin/bash is 3.2 (unsupported)." >&2
  echo "Remediation: brew install bash && ensure /usr/local/bin/bash appears first in PATH." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # coordinator/bin/tests

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

run "bash: plan-tasks-schema-enum-parity (C1)" bash "${SCRIPT_DIR}/test-plan-tasks-schema-enum-parity.sh"
# Invoked via pytest, not `python3 <file>`: the 2026-07-25 collectability migration
# removed this file's __main__ harness, so direct execution imports the module, asserts
# nothing, and exits 0 — this leg reported green while running none of its 17 tests.
# The sibling harvest legs below still carry their own __main__ and run directly.
run "python3: plan-tasks-spine-and-harvest (C7)" python3 -m pytest "${SCRIPT_DIR}/test_plan_tasks_spine_and_harvest.py" -q
run "python3: harvest-idempotency-env-override" python3 "${SCRIPT_DIR}/test_harvest_idempotency_env_override.py"
run "python3: harvest-doe-root-machine-local-leg" python3 "${SCRIPT_DIR}/test_harvest_doe_root_machine_local_leg.py"

if [[ $rc -eq 0 ]]; then
  echo "########## run-plan-tasks-spine-suites: ALL SUITES GREEN ##########"
else
  echo "########## run-plan-tasks-spine-suites: ONE OR MORE SUITES FAILED ##########" >&2
fi
exit $rc
