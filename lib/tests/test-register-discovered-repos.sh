#!/usr/bin/env bash
# test-register-discovered-repos.sh — regression net for F16 (install discovers
# working repos but never registers them into the machine-local repos.*
# registry that cross-repo addressing depends on).
#
# Exercises register-discovered-repos.py end-to-end against a SANDBOX registry
# (MACHINE_LOCAL_REGISTRY_DIR override — never the operator's real registry)
# AND a sandboxed $HOME (real Tier B dev-folder probe against fixture repos —
# see the retarget note in the Fixture section below; the discovery module's
# own tier-gating is separately covered by its own
# coordinator_core/ops/test_discover_working_repos.py).
#
# Run under bash>=4 per DR-148: /opt/homebrew/bin/bash test-register-discovered-repos.sh

set -euo pipefail

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_COORD_ROOT="$(cd "$_LIB_DIR/.." && pwd)"
_REAL_SCRIPT="$_LIB_DIR/register-discovered-repos.py"

_TMP="$(mktemp -d)"
trap 'rm -rf "$_TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# --- Fixture: sandbox lib dir with a copy of the real
#     register-discovered-repos.py. --------------------------------------
#
# Retargeted for the Python port (2026-07-21, de-bash chunk I-a): the bash
# oracle's register-discovered-repos.sh subprocess-shelled a sibling
# discover-working-repos.sh, so the original fixture stubbed that sibling
# file with a fixed candidate list. The ported
# coordinator_core.ops.register_discovered_repos.main() no longer shells
# out at all -- it directly imports and calls
# coordinator_core.ops.discover_working_repos.main() (see that module's own
# negative-spec: "does NOT reimplement discover-working-repos.sh's
# tier-gating logic ... retired the `bash discover-working-repos.sh`
# subprocess bridge"). A sibling-script stub is therefore dead weight: the
# real discovery module runs unconditionally, so this fixture drives ITS
# Tier B (common dev-folder layout probe) hermetically instead, via a
# fixture-scoped HOME override + real `.git` markers -- Tier B's candidate
# list (~/dev, ~/code, ... — see discover_working_repos.py
# _TIER_B_CANDIDATES) is expanded against $HOME, which this test pins to
# $_TMP for the duration of the run.
# register-discovered-repos.py is now a thin Python trampoline whose
# cc_invoke._resolve_claude_klabauter_root() ladder itself reads repos.claude_klabauter
# out of the machine-local registry (rung 2+) -- the very registry this test
# sandboxes to REGISTRY_DIR below to isolate the F16 registration assertions
# from the operator's real machine-local state. Resolve CLAUDE_KLABAUTER_ROOT from the
# REAL (unsandboxed) registry NOW, before $HOME gets pinned to the fixture
# dir below (which would also blind this lookup), and pin it via the env-var
# fast path (rung 1 of the ladder, checked before any registry read).
if [[ -z "${CLAUDE_KLABAUTER_ROOT:-}" ]]; then
    _real_claude_klabauter_root="$(command machine-local get repos.claude_klabauter 2>/dev/null || true)"
    if [[ -n "$_real_claude_klabauter_root" ]]; then
        export CLAUDE_KLABAUTER_ROOT="$_real_claude_klabauter_root"
    fi
fi

FIX_LIB="$_TMP/lib"
mkdir -p "$FIX_LIB"
cp "$_REAL_SCRIPT" "$FIX_LIB/register-discovered-repos.py"
chmod +x "$FIX_LIB/register-discovered-repos.py"

# register-discovered-repos.py is now a thin Python trampoline that resolves
# its cc_invoke helper at "<own-dir>/../bin/lib" -- since the fixture copies
# the script into $_TMP/lib, symlink that sibling path back to the real
# bin/lib so the copied script's relative resolution still finds
# cc_invoke.py without needing the whole coordinator/bin tree copied in.
mkdir -p "$_TMP/bin"
ln -s "$_COORD_ROOT/bin/lib" "$_TMP/bin/lib"

export HOME="$_TMP"
# Fixture dir is "$_TMP/repos", NOT "$_TMP/dev": Tier B's own candidate list
# (discover_working_repos.py _TIER_B_CANDIDATES) includes BOTH "~/dev" and
# "~/Dev" -- on a case-insensitive filesystem (default macOS/Windows) those
# two candidates alias the SAME physical directory, so a fixture under
# "~/dev" gets scanned twice (once per candidate string) and emitted twice
# under two differently-cased paths, corrupting the "registered exactly
# once" assertions below with a spurious duplicate-write. "~/repos" has no
# case-duplicate sibling in the candidate list, so it scans exactly once.
REPO_A="$_TMP/repos/repo-alpha"
REPO_B="$_TMP/repos/repo-beta"
NOT_QUALIFIED="$_TMP/repos/not-qualified-repo"
mkdir -p "$REPO_A" "$REPO_B" "$NOT_QUALIFIED"
# discover_working_repos._gate_and_dedup()'s `_is_git_root` gate shells out to
# `git rev-parse --show-toplevel` -- a bare `.git` directory (no refs/HEAD)
# fails that gate, so the fixture repos need a REAL (if empty) git repo, not
# just a `.git`-named directory.
git -C "$REPO_A" init --quiet
git -C "$REPO_B" init --quiet

# --- Sandbox registry + machine-local resolution -----------------------------
REGISTRY_DIR="$_TMP/registry"
mkdir -p "$REGISTRY_DIR"
export MACHINE_LOCAL_REGISTRY_DIR="$REGISTRY_DIR"
export COORDINATOR_SETTINGS_HOME="$_COORD_ROOT/templates"   # supplies bin/_machine_local.py
export PATH="$_COORD_ROOT/templates/bin:$PATH"               # supplies `machine-local` on PATH

_ml() { command machine-local "$@"; }

SCRIPT="$FIX_LIB/register-discovered-repos.py"

# --- 1. --check-only: reports would-register, writes nothing -----------------
OUT1="$("$SCRIPT" --check-only)"
if echo "$OUT1" | grep -q "repos.repo_alpha = $REPO_A" && echo "$OUT1" | grep -q "repos.repo_beta = $REPO_B"; then
    ok "1a: --check-only reports both candidates"
else
    bad "1a: --check-only missing expected candidates: [$OUT1]"
fi
if _ml has repos.repo_alpha >/dev/null 2>&1; then
    bad "1b: --check-only wrote repos.repo_alpha (should write nothing)"
else
    ok "1b: --check-only wrote nothing"
fi

# --- 2. --non-interactive: registers absent keys -----------------------------
"$SCRIPT" --non-interactive
VAL_A="$(_ml get repos.repo_alpha 2>/dev/null || true)"
VAL_B="$(_ml get repos.repo_beta 2>/dev/null || true)"
if [[ "$VAL_A" == "$REPO_A" ]]; then
    ok "2a: repos.repo_alpha registered with correct path"
else
    bad "2a: repos.repo_alpha expected [$REPO_A], got [$VAL_A]"
fi
if [[ "$VAL_B" == "$REPO_B" ]]; then
    ok "2b: repos.repo_beta registered with correct path"
else
    bad "2b: repos.repo_beta expected [$REPO_B], got [$VAL_B]"
fi

# --- 3. Tier-gating: the non-qualifying dir was never registered -------------
_ALL_KEYS="$(_ml keys 2>/dev/null || true)"
if echo "$_ALL_KEYS" | grep -q "not-qualified"; then
    bad "3a: non-qualifying repo was registered (tier-gating breach)"
else
    ok "3a: non-qualifying repo (no .git marker, absent from Tier B discovery) was never registered"
fi

# --- 4. Only-if-absent: re-run must NOT clobber a manually-set override ------
_ml set repos.repo_alpha "/manually/overridden/path"
"$SCRIPT" --non-interactive
VAL_A2="$(_ml get repos.repo_alpha 2>/dev/null || true)"
if [[ "$VAL_A2" == "/manually/overridden/path" ]]; then
    ok "4a: re-run preserved manually-overridden repos.repo_alpha (only-if-absent honored)"
else
    bad "4a: re-run clobbered manual override — got [$VAL_A2]"
fi
# repo-beta was already registered in step 2 — re-run must leave it untouched too.
VAL_B2="$(_ml get repos.repo_beta 2>/dev/null || true)"
if [[ "$VAL_B2" == "$REPO_B" ]]; then
    ok "4b: re-run left already-registered repos.repo_beta untouched"
else
    bad "4b: re-run altered repos.repo_beta — got [$VAL_B2]"
fi

# --- 5. Idempotent no-op on a fully-registered set: --check-only reports none -
OUT5="$("$SCRIPT" --check-only)"
if [[ -z "$OUT5" ]]; then
    ok "5a: --check-only reports nothing once everything is registered"
else
    bad "5a: --check-only unexpectedly reported: [$OUT5]"
fi

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
