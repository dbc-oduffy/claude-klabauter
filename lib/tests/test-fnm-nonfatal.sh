#!/usr/bin/env bash
# test-fnm-nonfatal.sh — regression net for issue #7 (C-R4).
# A FAILING fnm install must NOT abort install-substrate.py: it WARNs and the
# script still exits 0 (fnm is an optional per-repo Node convenience; core
# substrate already succeeded). Runs the real script in a sandbox with fnm
# absent and a curl stub that exits non-zero.

set -euo pipefail  # the deliberate set +e/set -e around the script-under-test invocation stays

_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_SCRIPT="$_ROOT/lib/install-substrate.py"

_TMP="$(mktemp -d)"
trap 'rm -rf "$_TMP"' EXIT

# Stub PATH: a curl that always fails + the minimal real tools. Excludes brew/fnm
# (homebrew dirs are deliberately not on PATH) so the curl branch is exercised and
# its failure is the path under test.
_STUB="$_TMP/stub-bin"
mkdir -p "$_STUB"
cat > "$_STUB/curl" <<'EOF'
#!/usr/bin/env bash
# Failing curl stub — simulates the fnm installer "Could not infer shell type"
# non-zero exit in non-interactive Git Bash.
echo "stub-curl: simulated failure" >&2
exit 1
EOF
chmod +x "$_STUB/curl"

# Run the real substrate install with fnm absent + curl failing.
# CLAUDE_KLABAUTER_ROOT pass-through: a fresh sandboxed CLAUDE_HOME has no machine-local
# registry entry for repos.claude_klabauter, so cc_invoke's resolution ladder
# would fail before ever reaching the fnm-nonfatal path under test — resolve
# the REAL machine's repos.claude_klabauter once and forward it (same pattern as
# coordinator/tests/test_install_substrate.sh's _REAL_CLAUDE_KLABAUTER_ROOT).
_REAL_CLAUDE_KLABAUTER_ROOT="$(machine-local get repos.claude_klabauter 2>/dev/null || true)"
set +e
OUT="$(CLAUDE_PLUGIN_ROOT="$_ROOT" CLAUDE_HOME="$_TMP" COORDINATOR_NON_INTERACTIVE=1 \
       CLAUDE_KLABAUTER_ROOT="$_REAL_CLAUDE_KLABAUTER_ROOT" \
       PATH="$_STUB:/usr/bin:/bin" \
       python3 "$_SCRIPT" 2>&1)"
RC=$?
set -e

PASS=0; FAIL=0
if [[ "$RC" -eq 0 ]]; then
    echo "  PASS: install-substrate exits 0 despite fnm install failure"; PASS=$((PASS+1))
else
    echo "  FAIL: expected exit 0, got $RC"; FAIL=$((FAIL+1))
    echo "----- output -----"; echo "$OUT" | tail -20; echo "------------------"
fi

if echo "$OUT" | grep -qiE "WARNING:.*fnm"; then
    echo "  PASS: emitted a non-fatal WARNING about fnm"; PASS=$((PASS+1))
else
    echo "  FAIL: expected a 'WARNING ... fnm' line on stderr/stdout"; FAIL=$((FAIL+1))
fi

if echo "$OUT" | grep -qiE "ERROR:.*fnm"; then
    echo "  FAIL: still emits a fatal-style ERROR for fnm (should be WARNING)"; FAIL=$((FAIL+1))
else
    echo "  PASS: no fatal ERROR for fnm"; PASS=$((PASS+1))
fi

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
