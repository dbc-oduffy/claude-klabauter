

# Purpose: Guards the POST-RETIREMENT invocation contract of cross-repo-memo.


set -uo pipefail


TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$(cd "${TESTS_DIR}/.." && pwd)"

CROSS_REPO_MEMO="${BIN_DIR}/cross-repo-memo"

if [ ! -f "$CROSS_REPO_MEMO" ]; then
    printf 'FAIL: %s not found\n' "$CROSS_REPO_MEMO" >&2
    exit 1
fi


SCRATCH=""
_cleanup() {
    if [ -n "$SCRATCH" ] && [ -d "$SCRATCH" ]; then
        rm -rf "$SCRATCH"
    fi
}
trap _cleanup EXIT

SCRATCH="$(mktemp -d)"


PYTHON3_BIN="$(command -v python3 2>/dev/null)" || {
    
    
    printf 'SKIP: python3 not found on PATH — cannot construct fixture\n'
    exit 0
}


ln -sf "$PYTHON3_BIN" "${SCRATCH}/python3"


if ! PATH="${SCRATCH}" command -v python3 >/dev/null 2>&1; then
    printf 'FAIL: fixture broken — python3 symlink not resolvable in SCRATCH\n' >&2
    exit 1
fi


OUTPUT=""
EXIT_CODE=0
OUTPUT="$(PATH="${SCRATCH}:/usr/bin:/bin" "$CROSS_REPO_MEMO" --help 2>&1)" || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    printf 'FAIL: cross-repo-memo --help exited %d under python3-only PATH\n' "$EXIT_CODE" >&2
    printf 'Output:\n%s\n' "$OUTPUT" >&2
    exit 1
fi


case "$OUTPUT" in
    *usage*|*Usage*|*VERB*|*cross-repo-memo*)
        
        ;;
    *)
        printf 'FAIL: --help output did not look like usage text\n' >&2
        printf 'Output:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac

printf 'PASS: cross-repo-memo --help exited 0 under python3-only PATH (python3 shebang resolves correctly)\n'


FIRST_LINE="$(head -1 "$CROSS_REPO_MEMO")"
if [ "$FIRST_LINE" != '#!/usr/bin/env python3' ]; then
    printf 'FAIL: cross-repo-memo shebang drifted — expected `#!/usr/bin/env python3`, got `%s`\n' "$FIRST_LINE" >&2
    printf 'The sh/python polyglot trampoline was retired (commit 7228f35e, DR-076); do not reintroduce it or swap the shebang.\n' >&2
    exit 1
fi


# pre-docstring header so prose MENTIONS of the trampoline in the module

HEADER="$(sed -n '1,5p' "$CROSS_REPO_MEMO")"
case "$HEADER" in
    *'#!/bin/sh'*|*'command -v python'*|*"exec python"*)
        printf 'FAIL: sh trampoline machinery reintroduced in cross-repo-memo header — retired by commit 7228f35e (DR-076)\n' >&2
        exit 1
        ;;
esac

printf 'PASS: cross-repo-memo header is trampoline-free with a python3 shebang (de-polyglot retirement holds)\n'
exit 0
