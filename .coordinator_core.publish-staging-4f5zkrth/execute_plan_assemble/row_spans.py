"""Pure line-span parsing for a plan's `## Tasks` spine rows.

Extracted (2026-08-06, C1) out of `close_out_and_stamp.py` to break an
import cycle: `close_out_and_stamp` imports six `coordinator_core.ops.*`
modules at top level, and `coordinator_core.ops.cascade_retract` imported
`_find_row_spans_in_plan` back from `close_out_and_stamp` -- so importing
`close_out_and_stamp` before `cascade_retract` raised `ImportError:
cannot import name '_find_row_spans_in_plan' from partially initialized
module`, and `coordinator_core/ops/__init__.py`'s registration loop
SWALLOWS that ImportError, so `deliverable.cascade_retract` silently
failed to register depending on import order.

`_parse_spine_rows` joined this module (2026-08-06, same-day follow-up):
`cascade_retract.py` also imported it from `close_out_and_stamp.py`, a
second live instance of the same cycle shape (surfaced only once the
first was fixed, in the pathological order "import close_out_and_stamp
standalone, no follow-up cascade_retract import" -- see this repo's
2026-08-06 run-report sidecar for the reproduction). It is pure --
`locate_fenced_block`/`LocateStatus`/`yaml.safe_load` only, no
`coordinator_core.ops` dependency -- so it fits this leaf cleanly.

This module is a LEAF: it imports nothing from `coordinator_core.ops`
(directly or transitively) -- only `re`, `typing`, `yaml`, and
`coordinator_core.frontmatter.body_blocks` (itself leaf-only). Both
`close_out_and_stamp` and `cascade_retract` import the row-span helpers
from here instead of from each other; `close_out_and_stamp` re-exports
the names so its existing callers keep working unchanged.
"""

from __future__ import annotations

import re
from typing import Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

_ROW_START_RE = re.compile(r"^(?P<indent>[ \t]*)-\s+id:\s*(?P<id>.+?)\s*$")


def _unquote_row_id(raw: str) -> str:
    """Strips a single layer of matching quotes off a YAML scalar token --
    a spine `id:` value is legal either bare (`C1`) or quoted (`"C1"`/
    `'C1'`); this normalizes both to the same comparison key the rest of
    this module already uses (`row.get("id")` values are never quoted,
    since `yaml.safe_load` already stripped them there)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _find_row_spans(lines: list[str]) -> list[tuple[int, int, str]]:
    """Locates every TOP-LEVEL row's line-range within a fenced spine
    body's already-split `lines`. A row starts at a line matching `- id:
    <value>` (the `- id: C1` shape `yaml.safe_dump`'s own list-of-dicts
    default output always produces -- the sequence marker and the row's
    first key share one line) and its extent runs up to (but excluding)
    the next row's start line, or end-of-body for the last row.

    Only the FIRST matching indent level is treated as "top-level" -- a
    `- id:` occurrence at a deeper indent (which this spine format never
    legitimately produces, since rows are a flat list of flat dicts) is
    ignored rather than mis-read as a sibling row, so a stray nested match
    can never fragment a real row's span."""
    indent: Optional[str] = None
    starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _ROW_START_RE.match(line)
        if not match:
            continue
        if indent is None:
            indent = match.group("indent")
        if match.group("indent") != indent:
            continue
        starts.append((idx, _unquote_row_id(match.group("id"))))

    spans: list[tuple[int, int, str]] = []
    for i, (start_idx, chunk_id) in enumerate(starts):
        end_idx = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        spans.append((start_idx, end_idx, chunk_id))
    return spans


def _find_row_spans_in_plan(old_lines: list[str], old_text: str) -> list[tuple[int, int, str]]:
    """`_find_row_spans`, but bounded to the plan's `## Tasks` FENCE BODY
    while still returning WHOLE-FILE line indices (so callers like
    `_row_span_containing` and `_measure_row_content_indent` can be fed
    `old_lines` unchanged).

    Why bounding matters (defect fix, 2026-08-01): `_find_row_spans` ends
    the LAST row's span at `len(lines)`, so running it over the whole plan
    text gave the final spine row a span running from its own start line to
    the END OF THE DOCUMENT -- past the closing fence, across every
    section of prose that follows. `_measure_row_content_indent` then
    measured that row's "content indent" over all of it, and any ordinary
    markdown line shaped like an indented `key: value` (a nested bullet, a
    YAML snippet in an example fence) could win the `min()` and produce a
    bogus `expected_indent`, false-rejecting a perfectly correct stamp on
    the last row.

    Degrades to today's whole-file behavior when the fenced block cannot
    be located (ABSENT/MALFORMED spine), or when its body does not start
    on a line boundary -- a bounded gate is an improvement, never a new
    crash surface."""
    located = locate_fenced_block(old_text)
    if located.status != LocateStatus.LOCATED or located.span is None:
        return _find_row_spans(old_lines)
    start, end = located.span
    prefix = old_text[:start]
    if prefix and not prefix.endswith(("\n", "\r")):
        return _find_row_spans(old_lines)
    offset = len(prefix.splitlines())
    body_lines = old_text[start:end].splitlines(keepends=True)
    return [
        (row_start + offset, row_end + offset, chunk_id)
        for row_start, row_end, chunk_id in _find_row_spans(body_lines)
    ]


def _parse_spine_rows(
    plan_text: str, plan_path_rel: str
) -> tuple[Optional[list], Optional[str]]:
    """Locates + parses the plan's `## Tasks` spine. Returns `(rows,
    error)`: `rows` is `None` with `error` set on a MALFORMED spine or
    unparseable/non-list body; `rows` is `[]` (D7's absent-spine ==
    full-shipped posture) on an ABSENT spine; `rows` is the parsed row
    list on a LOCATED spine. Factored out of `_determine_shipped` (C7) so
    there is exactly one locate/parse implementation shared by the
    shipped/halted verdict AND the AC8 auto-resolve step in
    `close_out_and_stamp`, which also needs the raw rows -- not just the
    id-list `_determine_shipped` reduces them to."""
    located = locate_fenced_block(plan_text)

    if located.status == LocateStatus.MALFORMED:
        return None, (
            f"{plan_path_rel}: malformed ## Tasks spine (more than one fenced "
            "`yaml plan-tasks` block, or a fence not directly under the "
            "heading) -- cannot determine chunk-completion mechanically; fix "
            "the spine before close-out-and-stamp can run"
        )

    if located.status == LocateStatus.ABSENT:
        return [], None

    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError as exc:
        return None, f"{plan_path_rel}: ## Tasks spine is not parseable YAML ({exc})"
    if not isinstance(rows, list):
        return None, f"{plan_path_rel}: ## Tasks spine body is not a YAML list"
    return rows, None
