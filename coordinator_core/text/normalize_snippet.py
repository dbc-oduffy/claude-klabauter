"""normalize_snippet — byte-parity port of normalize-snippet.sh::normalize().

Used by example-doctrine-repo-side verify-*-sync.sh scripts (via the
coordinator/bin/normalize-snippet CLI trampoline) to canonicalize both the
on-disk snippet body and the extracted sentinel block before diffing them
(snippet-sync verification).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Port recipe: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-normalize-snippet.md
Port of: normalize-snippet.sh (example-doctrine-repo 67202df6, 2026-07-16)

Original bash:
    normalize() {
        printf '%s' "$1" | sed 's/[[:space:]]*$//' | \
            awk 'NF{last=NR} {a[NR]=$0} END{s=1; while(s<=NR && a[s]==""){s++} for(i=s;i<=last;i++){print a[i]}}'
    }

Negative-spec: this is a PURE string->string transform (no I/O, no globals, no
side effects) — do not add filesystem/env reads to this module.
"""
from __future__ import annotations

# POSIX [[:space:]] under GNU/BSD sed == {space, tab, newline, vertical-tab,
# form-feed, carriage-return}. Newline cannot appear mid-line after
# text.split("\n"), so stripping it here is a no-op — kept in the strip set
# for exact class parity with the sed oracle.
_TRAILING_WHITESPACE = " \t\n\v\f\r"


def normalize_snippet(text: str) -> str:
    """Strip leading/trailing blank lines and per-line trailing whitespace.

    Byte-parity port of normalize-snippet.sh::normalize() (example-doctrine-repo 67202df6,
    2026-07-16) — whitespace-only lines count as blank (matches the sed
    pre-pass before the bash awk pass). No trailing newline in the return
    value (mirrors printf '%s' "$1", which never appends one).
    """
    lines = [line.rstrip(_TRAILING_WHITESPACE) for line in text.split("\n")]

    first = next((i for i, line in enumerate(lines) if line != ""), None)
    if first is None:
        return ""
    last = max(i for i, line in enumerate(lines) if line != "")
    return "\n".join(lines[first : last + 1])
