"""
coordinator_core.ops.read_frontmatter_field — bare YAML frontmatter scalar extraction CLI.

Purpose: reads the first occurrence of a named YAML scalar field from a markdown
artifact and returns the clean, unquoted value — no trailing newline. Designed for
deliverable-spine id/FK fields (deliverable_id, initiative) where values are simple
strings that never contain '#'.

Port of: read-frontmatter-field.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (deliberate fidelity to the bash oracle — do NOT "fix"):
    - Does NOT do a bounded frontmatter-fence read. Like the oracle, this searches
      the ENTIRE file text for the first line matching `^{field}:` — correct for
      coordinator artifacts where frontmatter is at file-top, incorrect (but
      faithfully reproduced) for files whose body contains a matching key above
      any frontmatter fence. Do not "fix" this to be fence-bounded; that changes
      the CLI contract callers already depend on.
    - Comment-stripping is NOT space-prefix-bounded despite the oracle's own inline
      comment claiming otherwise — the regex (and this port) strip from the FIRST
      bare `#` anywhere in the value, with zero or more immediately-preceding
      whitespace, not only a space-delimited ` #comment`. A value containing `#`
      with no preceding space (e.g. `foo#bar`) still gets truncated to `foo`.
    - Only ONE layer of surrounding quotes (double or single) is stripped — a
      doubly-quoted value keeps its inner quote layer.
    - `null` (bare or quoted) always normalizes to empty string, same as an
      absent field or unreadable file — callers cannot distinguish these cases
      from stdout alone (by design; see the oracle's own docstring).
"""

from __future__ import annotations

import sys
from typing import List, Optional


def _extract(text: str, field: str) -> Optional[str]:
    """Return the raw matched frontmatter line's value-bearing suffix, or None if absent.

    Mirrors the oracle's `grep -m1 "^${field}:"` — first line anywhere in the file
    (not fence-bounded) whose text begins with `{field}:`.
    """
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _normalize(raw: str) -> str:
    """Apply the oracle's 5-step normalization pipeline to a raw matched-line suffix."""
    # Step 1: strip leading whitespace (key itself already stripped by the caller).
    value = raw.lstrip()

    # Step 2: strip from the first '#' (plus any immediately-preceding whitespace)
    # to end of line. Zero-width space-class match — a '#' with no preceding
    # space is still a strip point (see module negative-spec).
    hash_idx = value.find("#")
    if hash_idx != -1:
        value = value[:hash_idx].rstrip(" \t")

    # Step 3: trim surrounding whitespace.
    value = value.strip()

    # Step 4: strip ONE layer of surrounding quotes (double or single).
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]

    # Step 5: literal 'null' or empty → empty string.
    if value == "null" or value == "":
        return ""

    return value


def read_frontmatter_field(file_path: str, field: str) -> str:
    """Extract a bare frontmatter scalar value from ``file_path`` for ``field``.

    Returns the empty string when the file path is missing/empty/unreadable, the
    field is absent, or the value normalizes to the literal token ``null``.
    Never raises — mirrors the oracle's ``set -euo pipefail`` combined with its
    own always-0-exit contract (input problems are data, not errors).
    """
    if not file_path or not field:
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: read_frontmatter_field: with open(file_path, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    raw = _extract(text, field)
    if raw is None:
        return ""
    return _normalize(raw)


def main(argv: List[str]) -> int:
    """CLI entry point: ``read-frontmatter-field.sh <file> <field>``.

    Always exits 0 — callers pass optional paths; absence is not an error
    (faithful to the oracle's documented always-0 exit contract).
    """
    file_path = argv[0] if len(argv) > 0 else ""
    field = argv[1] if len(argv) > 1 else ""
    sys.stdout.write(read_frontmatter_field(file_path, field))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
