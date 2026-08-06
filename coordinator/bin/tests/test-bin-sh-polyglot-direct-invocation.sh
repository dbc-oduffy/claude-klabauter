#!/usr/bin/env bash
# verify-no-console-flash: file-allow — test scaffolding; interpreter spawns run in the CI/local test harness, never the Windows interactive coordinator hot-path
# tests/test-bin-sh-polyglot-direct-invocation.sh
#
# Purpose: Guards the POST-RETIREMENT invocation contract of cross-repo-memo.
# The sh/python polyglot trampoline this suite originally exercised was
# deliberately retired (commit 7228f35e, 2026-07-21 — PM's named first exemplar
# of the de-polyglot direction). The current contract per DR-076
# cross-platform invocation parity is: `#!/usr/bin/env python3` shebang +
# `.cmd` launcher on Windows — no sh trampoline, no `python`/`py` shebang
# fallback probing.
#
# Test contract:
#   1. Construct a scratch dir containing a symlink to python3 ONLY and run
#      ./cross-repo-memo --help with PATH="$SCRATCH:/usr/bin:/bin"; assert
#      exit 0 and usage-shaped output. This validates direct shebang
#      invocation without relying on ambient PATH.
#   2. Negative-spec drift guard: assert line 1 of cross-repo-memo IS
#      `#!/usr/bin/env python3` and the sh trampoline has NOT been
#      reintroduced. The trampoline regressed once before its retirement
#      (see the subject's git log); this pins the retirement decision.
#
# NOTE: the historical `python`-only-PATH fallback leg was removed with the
# trampoline — on Windows (where python3 may be absent) invocation goes via
# the `.cmd` launcher, not shebang fallback. Testing a python-only POSIX PATH
# is testing a mechanism that intentionally no longer exists.
#
# Spec backlink: docs/plans/2026-06-18-bin-cli-sh-shebang-polyglot.md (original
#   trampoline spec — superseded by the de-polyglot retirement, commit 7228f35e)
# Doctrine: docs/wiki/cross-platform-invocation-parity.md; DR-076
#
# Exit codes:
#   0 — PASS
#   1 — FAIL (with diagnostic output)
#
# Portability: bash 3.2 + BSD coreutils (DR-148). No mapfile/readarray/declare -A.

set -uo pipefail

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------

# $0 might be invoked from any cwd — resolve BIN_DIR relative to this script.
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$(cd "${TESTS_DIR}/.." && pwd)"

CROSS_REPO_MEMO="${BIN_DIR}/cross-repo-memo"

if [ ! -f "$CROSS_REPO_MEMO" ]; then
    printf 'FAIL: %s not found\n' "$CROSS_REPO_MEMO" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: direct invocation under a python3-only stripped PATH
# ---------------------------------------------------------------------------

SCRATCH=""
_cleanup() {
    if [ -n "$SCRATCH" ] && [ -d "$SCRATCH" ]; then
        rm -rf "$SCRATCH"
    fi
}
trap _cleanup EXIT

SCRATCH="$(mktemp -d)"

# Locate the real python3 binary so we can symlink to it.
PYTHON3_BIN="$(command -v python3 2>/dev/null)" || {
    # Print to STDOUT (not stderr) so the skip is visible in CI logs and test
    # runner summaries that only capture stdout. Skipping is legitimate when
    # python3 is genuinely absent — not a failure.
    # Review: code-reviewer — SKIP messages should be visible on stdout (Finding E)
    printf 'SKIP: python3 not found on PATH — cannot construct fixture\n'
    exit 0
}

# Create symlink: $SCRATCH/python3 -> real python3
ln -sf "$PYTHON3_BIN" "${SCRATCH}/python3"

# Confirm fixture: python3 resolves under SCRATCH only.
if ! PATH="${SCRATCH}" command -v python3 >/dev/null 2>&1; then
    printf 'FAIL: fixture broken — python3 symlink not resolvable in SCRATCH\n' >&2
    exit 1
fi

# Run cross-repo-memo --help under the stripped PATH.
# /usr/bin and /bin are included so env(1) itself is reachable for the shebang,
# but only SCRATCH's python3 satisfies the interpreter lookup deterministically.
OUTPUT=""
EXIT_CODE=0
OUTPUT="$(PATH="${SCRATCH}:/usr/bin:/bin" "$CROSS_REPO_MEMO" --help 2>&1)" || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    printf 'FAIL: cross-repo-memo --help exited %d under python3-only PATH\n' "$EXIT_CODE" >&2
    printf 'Output:\n%s\n' "$OUTPUT" >&2
    exit 1
fi

# Sanity: confirm the output looks like a usage message (not an interpreter error).
case "$OUTPUT" in
    *usage*|*Usage*|*VERB*|*cross-repo-memo*)
        # Looks like a real help output — good.
        ;;
    *)
        printf 'FAIL: --help output did not look like usage text\n' >&2
        printf 'Output:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac

printf 'PASS: cross-repo-memo --help exited 0 under python3-only PATH (python3 shebang resolves correctly)\n'

# ---------------------------------------------------------------------------
# Test 2: de-polyglot retirement drift guard
#
# The trampoline retirement (commit 7228f35e) replaced the #!/bin/sh polyglot
# header with a bare #!/usr/bin/env python3 shebang. The subject's own history
# shows shebang drift happened once before; pin both directions:
#   - shebang must be exactly `#!/usr/bin/env python3`
#   - no sh trampoline block may reappear in the executable header region
# ---------------------------------------------------------------------------

FIRST_LINE="$(head -1 "$CROSS_REPO_MEMO")"
if [ "$FIRST_LINE" != '#!/usr/bin/env python3' ]; then
    printf 'FAIL: cross-repo-memo shebang drifted — expected `#!/usr/bin/env python3`, got `%s`\n' "$FIRST_LINE" >&2
    printf 'The sh/python polyglot trampoline was retired (commit 7228f35e, DR-076); do not reintroduce it or swap the shebang.\n' >&2
    exit 1
fi

# The retired trampoline lived in the first ~15 lines as sh code (`exec`
# probing python3/python/py) before the Python docstring. Scan only the
# pre-docstring header so prose MENTIONS of the trampoline in the module
# docstring (which legitimately document its retirement) don't false-positive.
HEADER="$(sed -n '1,5p' "$CROSS_REPO_MEMO")"
case "$HEADER" in
    *'#!/bin/sh'*|*'command -v python'*|*"exec python"*)
        printf 'FAIL: sh trampoline machinery reintroduced in cross-repo-memo header — retired by commit 7228f35e (DR-076)\n' >&2
        exit 1
        ;;
esac

printf 'PASS: cross-repo-memo header is trampoline-free with a python3 shebang (de-polyglot retirement holds)\n'
exit 0
