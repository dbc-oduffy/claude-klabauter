#!/usr/bin/env bash
# verify-no-console-flash: file-allow — test scaffolding; interpreter spawns run in the CI/local test harness, never the Windows interactive coordinator hot-path
# test-resolve-python-pin.sh — unit tests for the pinned-interpreter tier in
# lib/resolve-python.sh.
#
# Spec backlink: docs/plans/2026-06-20-whoami-durable-install-surface.md § C1 / test
#
# Table-driven tests:
#   T1  COORDINATOR_PYTHON set to a valid interpreter -> PYTHON_BIN equals it
#   T2  COORDINATOR_PYTHON set to a non-executable path -> fail-loud (return 1)
#   T3  COORDINATOR_PYTHON set to a file that exists but 'import sys' fails
#       (dangling venv shim) -> fail-loud (return 1)
#   T4  No pin set -> falls through to real python3; PYTHON_BIN non-empty
#
# Exit 0 == all tests passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/../resolve-python.sh"

PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL+1)); }

echo "=== test-resolve-python-pin.sh ==="

if [[ ! -f "$LIB" ]]; then
  echo "FATAL: lib not found at $LIB" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Temp directory for fake interpreters; cleaned up on exit.
# ---------------------------------------------------------------------------
TMPDIR_TEST="$(mktemp -d)"
_cleanup() { rm -rf "$TMPDIR_TEST"; }
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Helper: source the lib in a subshell with a clean environment.
# Prints PYTHON_BIN on stdout, or __FAIL__ if source returned non-zero.
# ---------------------------------------------------------------------------
_source_lib() {
  (
    unset COORDINATOR_PYTHON || true
    for _assignment in "$@"; do
      export "${_assignment?}"
    done
    if source "$LIB"; then
      printf '%s' "${PYTHON_BIN:-}"
    else
      printf '__FAIL__'
    fi
  )
}

# ---------------------------------------------------------------------------
# T1: Valid pin via COORDINATOR_PYTHON -> PYTHON_BIN equals the pin.
# ---------------------------------------------------------------------------
echo "--- T1: valid COORDINATOR_PYTHON pin"

_REAL_PY=""
if command -v python3 &>/dev/null; then
  _REAL_PY="$(command -v python3)"
elif command -v python &>/dev/null; then
  _REAL_PY="$(command -v python)"
fi

if [[ -z "$_REAL_PY" ]]; then
  echo "  SKIP T1 — no real python on PATH; cannot test valid pin"
else
  T1_OUT="$(_source_lib "COORDINATOR_PYTHON=$_REAL_PY" || true)"
  if [[ "$T1_OUT" == "$_REAL_PY" ]]; then
    _pass "T1 valid pin: PYTHON_BIN == $_REAL_PY"
  else
    _fail "T1 valid pin" "expected '$_REAL_PY', got '$T1_OUT'"
  fi
fi

# ---------------------------------------------------------------------------
# T2: COORDINATOR_PYTHON set to a non-existent path -> fail-loud.
# ---------------------------------------------------------------------------
echo "--- T2: non-existent COORDINATOR_PYTHON -> fail-loud"

_NONEXEC="$TMPDIR_TEST/nonexistent-python"
# The `|| true` below is load-bearing: the subshell exits 1 for a broken pin
# and the harness runs under `set -e`, so without `|| true` the test file itself
# would abort on the expected non-zero exit rather than capturing __FAIL__.
T2_OUT="$(_source_lib "COORDINATOR_PYTHON=$_NONEXEC" || true)"
if [[ "$T2_OUT" == "__FAIL__" ]]; then
  _pass "T2 non-existent pin: source returned non-zero"
else
  _fail "T2 non-existent pin" "expected __FAIL__ (non-zero), got '$T2_OUT'"
fi

# ---------------------------------------------------------------------------
# T3: Dangling pin — file exists and is executable, but invocation fails.
# Simulates a dangling venv interpreter whose base was removed.
# ---------------------------------------------------------------------------
echo "--- T3: dangling pin (exists but fails import sys) -> fail-loud"

_DANGLING="$TMPDIR_TEST/dangling-py"
# Write a shell stub that always exits 1, simulating a broken interpreter.
printf '#!/bin/sh\nexit 1\n' > "$_DANGLING"
chmod +x "$_DANGLING"

T3_OUT="$(_source_lib "COORDINATOR_PYTHON=$_DANGLING" || true)"
if [[ "$T3_OUT" == "__FAIL__" ]]; then
  _pass "T3 dangling pin: source returned non-zero (fail-loud)"
else
  _fail "T3 dangling pin" "expected __FAIL__ (non-zero), got '$T3_OUT'"
fi

# ---------------------------------------------------------------------------
# T4: No pin set -> falls through to real python3; PYTHON_BIN is non-empty.
# ---------------------------------------------------------------------------
echo "--- T4: no pin -> falls through to real interpreter"

T4_OUT="$(_source_lib || true)"
if [[ "$T4_OUT" == "__FAIL__" ]]; then
  _fail "T4 no-pin fallthrough" "lib returned non-zero with no pin set"
elif [[ -z "$T4_OUT" ]]; then
  if [[ -z "$_REAL_PY" ]]; then
    echo "  SKIP T4 — no python on PATH; fallthrough cannot produce a result"
  else
    _fail "T4 no-pin fallthrough" "PYTHON_BIN is empty but python is on PATH"
  fi
else
  _pass "T4 no-pin fallthrough: PYTHON_BIN='$T4_OUT' (non-empty)"
fi

# ---------------------------------------------------------------------------
# T5: machine-local pin -> PYTHON_BIN equals the pin.
# Inject a fake `machine-local` CLI onto PATH that prints a valid interpreter
# path on `get coordinator.python`. Unset COORDINATOR_PYTHON so the env-var
# tier is skipped and the machine-local tier fires.
# ---------------------------------------------------------------------------
echo "--- T5: machine-local pin -> PYTHON_BIN equals pin"

if [[ -z "$_REAL_PY" ]]; then
  echo "  SKIP T5 — no real python on PATH; cannot build a valid machine-local pin"
else
  _ML_BIN_DIR="$TMPDIR_TEST/fake-machine-local-bin"
  mkdir -p "$_ML_BIN_DIR"
  _ML_CLI="$_ML_BIN_DIR/machine-local"
  # Stub: when called as `machine-local get coordinator.python`, print the real
  # interpreter path; for any other invocation exit 1 so misuse is detectable.
  cat > "$_ML_CLI" <<STUB_EOF
#!/bin/sh
if [ "\$1" = "get" ] && [ "\$2" = "coordinator.python" ]; then
  printf '%s' "$_REAL_PY"
  exit 0
fi
exit 1
STUB_EOF
  chmod +x "$_ML_CLI"

  # Inject the fake CLI onto PATH and source the lib with COORDINATOR_PYTHON unset.
  T5_OUT="$(
    unset COORDINATOR_PYTHON || true
    PATH="$_ML_BIN_DIR:$PATH"
    if source "$LIB"; then
      printf '%s' "${PYTHON_BIN:-}"
    else
      printf '__FAIL__'
    fi
  )"

  if [[ "$T5_OUT" == "$_REAL_PY" ]]; then
    _pass "T5 machine-local pin: PYTHON_BIN == $_REAL_PY"
  elif [[ "$T5_OUT" == "__FAIL__" ]]; then
    _fail "T5 machine-local pin" "lib returned non-zero; fake machine-local not consulted or pin rejected"
  else
    _fail "T5 machine-local pin" "expected '$_REAL_PY', got '$T5_OUT'"
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
