"""
coordinator_core.housekeeping.head_scan — the declining frontmatter head-scan.

Cite (BINDING source, per plan contract 8): docs/research/spike-verdicts/
2026-08-29-frontmatter-head-scan-for-the-gate-index-walk.md, which measured
8,505 key-instances against `dag._read_meta` at 0 mismatches with 68 declines.
This module is the mechanism that spike proved — a narrow, decline-on-doubt
byte-level scan of a file's frontmatter block for a caller-named set of
top-level scalar keys, replacing `dag._read_meta`'s full read+hash+YAML parse
on the common case.

Contract 8 (`docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md`,
chunk C2): a reader that cannot parse a value PLAINLY must DECLINE, never
guess. The decline list is CLOSED:
  - no leading `---`
  - no closing delimiter within the read budget
  - a tab anywhere in the frontmatter block's indentation
  - a value that is quoted, block (`|`/`>`), flow (`[`/`{`),
    anchored/aliased/tagged (`&`/`*`/`!`), or contains `#`
  - a duplicate occurrence of a requested key

A decline means "uncertain" for the WHOLE file, never a per-key partial
answer — `head_scan` returns `None` for the file and the caller must fall
through to a full parse of that one file (`scan_keys` below does this).
`head_scan` never falls through to a missing value: an absent requested key
is legitimately omitted from the returned dict without triggering a decline.

Negative-spec, load-bearing (plan contract 8): a variant that returns a
value in place of one of the six decline triggers above is a DIFFERENT,
unproven mechanism. Do NOT widen the decline list to be "helpful" (e.g.
declining on a leading `-` list marker, or on a numeric/bool/null-looking
scalar) — the spike's 0-mismatches/68-declines result was measured against
exactly this list, not a superset or subset of it.

This is a general-purpose "give me these top-level scalar keys or tell me
you can't" primitive, with no domain verdict logic baked in — callers
(C3's live-corpus read, C4's archive index) supply their own key set and
decide what the values mean.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Union

from coordinator_core.dag import _read_meta

#: Bounded read for the head-scan's own file open — independent of, and
#: never a substitute for, `dag._read_meta`'s full `read_bytes()` (which a
#: declined file still pays in full via `scan_keys`'s fall-through). Sized
#: generously above a normal handoff's frontmatter block; a block-scalar or
#: long-frontmatter file that doesn't fit inside this budget simply finds no
#: closing delimiter and declines (never guesses). Matches the sibling
#: mechanism's own budget (`_prefilter_scan_disqualifies`'s
#: `_PREFILTER_READ_BYTES`) so the two stay commensurable.
_READ_BUDGET_BYTES = 4096

#: Leading characters that make a scalar value non-plain per contract 8:
#: single/double quote (quoted), `|`/`>` (block), `[`/`{` (flow),
#: `&`/`*`/`!` (anchor/alias/tag). A `#` anywhere in the value (possible
#: inline comment) is checked separately since it is not a leading-char
#: condition.
_NON_PLAIN_LEADING_CHARS = "'\"|>[{&*!"


def _plain_scalar_or_none(raw: str) -> Optional[str]:
    """Return `raw` unchanged iff it is a plain, single-line scalar per the
    contract 8 decline list — else None (ambiguous; caller must decline the
    whole file).

    Deliberately does NOT reject a value `dag._parse_scalar` would coerce to
    a non-string (int/float/bool/null) or a value starting with `-` — the
    decline list is closed to the six triggers named in this module's
    docstring, and widening it is out of scope (contract 8: "Do not widen
    the decline list to be helpful").
    """
    if "#" in raw:
        return None
    if raw and raw[0] in _NON_PLAIN_LEADING_CHARS:
        return None
    return raw


def head_scan(path: Union[str, Path], keys: Iterable[str]) -> Optional[Dict[str, str]]:
    """Scan `path`'s frontmatter block for the given top-level scalar `keys`,
    reading at most `_READ_BUDGET_BYTES` bytes.

    Returns a dict mapping each FOUND, plainly-parseable key to its raw
    string value — a key absent from the frontmatter is simply absent from
    the returned dict, not a decline. Returns None (decline) for the whole
    file when any of contract 8's six closed triggers fires: no leading
    `---`; no closing delimiter within the read budget; a tab in the
    frontmatter block's indentation; a requested key's value that is not a
    plain single-line scalar; or a requested key repeated.

    A key nested under indentation (not a top-level line) is not a match
    and is silently skipped, mirroring `_prefilter_scan_disqualifies`.
    """
    keys = set(keys)
    try:
        with open(path, "rb") as f:
            chunk = f.read(_READ_BUDGET_BYTES)
    except OSError:
        return None

    if not chunk.startswith(b"---"):
        return None  # covers a leading BOM too — the BOM byte(s) shift the
        # decoded first line away from a literal "---" match below.

    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        return None

    close_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            close_idx = i
            break
    if close_idx is None:
        return None  # closing delimiter not found inside the read budget

    block_lines = lines[1:close_idx]
    if any("\t" in ln for ln in block_lines):
        return None

    found: Dict[str, str] = {}
    for ln in block_lines:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent != 0:
            continue  # nested line — not a top-level key, irrelevant here
        colon_idx = stripped.find(":")
        if colon_idx == -1:
            continue
        key = stripped[:colon_idx].strip()
        if key not in keys:
            continue
        if key in found:
            return None  # duplicate key — ambiguous, decline the file
        raw_value = stripped[colon_idx + 1:].strip()
        plain = _plain_scalar_or_none(raw_value)
        if plain is None:
            return None
        found[key] = plain

    return found


def scan_keys(path: Union[str, Path], keys: Iterable[str]) -> Dict[str, Any]:
    """Return values for `keys` from `path`'s frontmatter, preferring the
    cheap `head_scan`; falls through to a full parse of THAT ONE FILE
    (`dag._read_meta`) when `head_scan` declines — never to a missing
    value, per contract 8.

    The fallen-through result may carry richer types than `head_scan`'s
    always-string values (`_read_meta` type-coerces via `_parse_scalar`);
    callers reading a mix of head-scanned and fallen-through files should
    not assume a uniform value type across the corpus.
    """
    keys_set: Set[str] = set(keys)
    scanned = head_scan(path, keys_set)
    if scanned is not None:
        return scanned
    meta = _read_meta(str(path)) or {}
    return {k: meta[k] for k in keys_set if k in meta}
