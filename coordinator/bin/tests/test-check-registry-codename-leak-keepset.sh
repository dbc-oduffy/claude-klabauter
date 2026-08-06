#!/usr/bin/env bash
# test-check-registry-codename-leak-keepset.sh — regression test for the
# 'experiments' keep-set entry in check-registry-codename-leak.py.
#
# Verifies:
#   1. A legitimate 'experiments' doc mention does NOT trip the guard
#      (exit 0) — 'experiments' is private but documentation-allowed (PM
#      resolution, C4).
#   2. A genuinely-private synthetic codename injected via
#      COORDINATOR_CODENAME_REGISTRY_KEYS still trips the guard
#      (exit non-zero) — proves the keep-set addition didn't neuter the
#      guard generally.
#
# Spec backlink: docs/plans/2026-06-27-genericize-provenance-sweeper.md § C4

set -euo pipefail

if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: this test requires bash 4.0+. Run via: /opt/homebrew/bin/bash $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/../check-registry-codename-leak.py"

fail=0

# --- Test 1: 'experiments' keep-set entry — legitimate mention passes clean ---
tmpdir1="$(mktemp -d)"
trap 'rm -rf "$tmpdir1"' EXIT

cat > "$tmpdir1/notes.md" <<'EOF'
We ran a batch of experiments this week to validate the new pipeline.
See docs/experiments/2026-07-09-batch-results.md for details.
EOF

set +e
out1=$(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.experiments" python "$GUARD" "$tmpdir1" 2>&1)
rc1=$?
set -e

if [[ $rc1 -ne 0 ]]; then
  echo "FAIL: Test 1 — 'experiments' keep-set entry should exit 0 on a legitimate doc mention, got exit $rc1" >&2
  echo "$out1" >&2
  fail=1
else
  echo "PASS: Test 1 — 'experiments' keep-set entry exits 0 on legitimate doc mention."
fi

# --- Test 2: genuinely-private synthetic codename still trips the guard ---
tmpdir2="$(mktemp -d)"
trap 'rm -rf "$tmpdir1" "$tmpdir2"' EXIT

cat > "$tmpdir2/notes.md" <<'EOF'
Internal reference to project_zolithane must never leak into the public tree.
EOF

set +e
out2=$(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.experiments repos.project_zolithane" python "$GUARD" "$tmpdir2" 2>&1)
rc2=$?
set -e

if [[ $rc2 -eq 0 ]]; then
  echo "FAIL: Test 2 — synthetic private codename 'project_zolithane' should trip the guard (non-zero exit), got exit 0" >&2
  echo "$out2" >&2
  fail=1
else
  echo "PASS: Test 2 — synthetic private codename 'project_zolithane' still trips the guard (exit $rc2)."
fi

if [[ $fail -ne 0 ]]; then
  echo "test-check-registry-codename-leak-keepset.sh: FAILED" >&2
  exit 1
fi

echo "test-check-registry-codename-leak-keepset.sh: all tests passed."
exit 0
