"""plan.merge id-collision detection and delta-supplied re-key. Pure functions, no I/O.

Re-key splices the `id:` line and the `depends_on[].chunk` lines of the original row text; rows are never
re-serialised, so every byte outside those scalars survives.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Union

from coordinator_core.ops._plan_merge_contract import Refusal, SourceRow

_ID_LINE = re.compile(r"^(?P<pre>\s*-\s+id:\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]+)(?P<post>.*)$")
_DEPENDS_KEY = re.compile(r"^(?P<ind>\s*)depends_on:\s*(?:#.*)?$")
_CHUNK_LINE = re.compile(r"^(?P<pre>\s*(?:-\s+)?chunk:\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]+)(?P<post>.*)$")


def _unquote(val: str) -> str:
    return val[1:-1] if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'" else val


def _requote(old_val: str, new_id: str) -> str:
    return f"{old_val[0]}{new_id}{old_val[0]}" if old_val[0] in "\"'" else new_id


def detect_collisions(rows: List[SourceRow]) -> List[Tuple[str, Tuple[str, ...]]]:
    """Every row id found in two or more distinct sources, with all of those source paths, in first-seen order."""
    by_id: Dict[str, List[str]] = {}
    for r in rows:
        paths = by_id.setdefault(r.row_id, [])
        if r.source not in paths:
            paths.append(r.source)
    return [(rid, tuple(p)) for rid, p in by_id.items() if len(p) >= 2]


def _rewrite_row(text: str, mapping: Dict[str, str]) -> str:
    lines = text.split("\n")
    key_indent = None
    for i, line in enumerate(lines):
        m = _ID_LINE.match(line)
        if m:
            old = _unquote(m.group("val"))
            if old in mapping:
                lines[i] = m.group("pre") + _requote(m.group("val"), mapping[old]) + m.group("post")
            key_indent = len(m.group("pre")) - len(m.group("pre").lstrip()) + 2
            break
    if key_indent is None:
        return text
    in_deps = False
    for i, line in enumerate(lines):
        if not in_deps:
            d = _DEPENDS_KEY.match(line)
            if d and len(d.group("ind")) == key_indent:
                in_deps = True
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(line) - len(line.lstrip()) <= key_indent:
            if not stripped.startswith("- "):
                break
        c = _CHUNK_LINE.match(line)
        if c:
            old = _unquote(c.group("val"))
            if old in mapping:
                lines[i] = c.group("pre") + _requote(c.group("val"), mapping[old]) + c.group("post")
    return "\n".join(lines)


def apply_rekey(rows: List[SourceRow], rekey: Optional[Dict[str, Dict[str, str]]]) -> Union[List[SourceRow], Refusal]:
    """Apply a `{source: {old: new}}` id mapping to `rows`.

    Rewrites each re-keyed row's `id:` line and every `depends_on[].chunk` naming a re-keyed id within the
    same source. Refuses REKEY_DANGLING when a mapping names a source or id absent from the rows, or when the
    re-keyed rows still collide.
    """
    mappings = rekey or {}
    if not mappings:
        return list(rows)
    present: Dict[str, set] = {}
    for r in rows:
        present.setdefault(r.source, set()).add(r.row_id)
    for src, ids in mappings.items():
        if src not in present:
            return Refusal("REKEY_DANGLING", f"rekey names source {src!r} that is not among the sources",
                           (src,), tuple(ids))
        missing = tuple(i for i in ids if i not in present[src])
        if missing:
            return Refusal("REKEY_DANGLING",
                           f"rekey names id(s) {', '.join(missing)} absent from {src}", (src,), missing)
    out: List[SourceRow] = []
    for r in rows:
        mapping = mappings.get(r.source)
        if not mapping:
            out.append(r)
            continue
        out.append(SourceRow(r.source, mapping.get(r.row_id, r.row_id), _rewrite_row(r.text, mapping)))
    still = detect_collisions(out)
    if still:
        rid, paths = still[0]
        return Refusal("REKEY_DANGLING", f"rekey leaves id {rid} in more than one source: {', '.join(paths)}",
                       paths, tuple(i for i, _ in still))
    return out
