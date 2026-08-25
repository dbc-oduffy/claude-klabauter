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

The disposition/stamping/git helpers joined this module (2026-08-23): the SAME
cycle shape resurfaced a third time, now through
`coordinator_core.ops.cascade_baton_rows`, which imported thirteen private
names (`_OPEN`, `_run_git`, `_stamp_rows_in_body`, ...) back out of
`close_out_and_stamp`. Running `close-out-and-stamp` as the ENTRY POINT made
that fatal: the module begins initializing, its top-level
`coordinator_core.ops.*` imports fire `_eager_import_all`, that reaches
`cascade_baton_rows`, and `_OPEN` does not exist yet -- so
`deliverable_cascade` and `cascade_backstop_sweep` failed to register on every
close-out run, printing a traceback and then succeeding. Every name involved
was already pure (text/span/git-subprocess only, no `coordinator_core.ops`
dependency), so they belong on this leaf rather than behind a lazy import.

This module is a LEAF: it imports nothing from `coordinator_core.ops`
(directly or transitively) -- only `re`, `typing`, `yaml`, and
`coordinator_core.frontmatter.body_blocks` (itself leaf-only). Both
`close_out_and_stamp` and `cascade_retract` import the row-span helpers
from here instead of from each other; `close_out_and_stamp` re-exports
the names so its existing callers keep working unchanged.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    serialize_yaml_scalar,
    split_frontmatter,
)

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


#: Sized against the machine load norm (50-70 concurrent LLM sessions, this
#: repo's own CLAUDE.md): every `git` call in this module is single-object
#: plumbing work, so a breach here is a wedged process, not a slow one --
#: same rationale `check-install-divergence.py`'s own `_GIT_TIMEOUT_SECS`
#: records. Absence was previously load-bearing-but-unbounded (this
#: module's own fan-out site, `_dispatch_ledger_delivered`, had none) --
#: see `state/audits/2026-08-15-fleet-composed-op-spawn-census.md` row 18.
_GIT_TIMEOUT_SECS = 20.0


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Runs one `git` subprocess, never raising -- a timeout degrades to a
    synthetic non-zero-returncode result (never `subprocess.TimeoutExpired`
    escaping), the SAME "never raises, every failure degrades to a skip/
    false" posture every reader of this function's result already assumes
    throughout this module (e.g. `_dispatch_ledger_delivered`'s own
    docstring)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=1,
            stdout="",
            stderr=f"git command timed out after {_GIT_TIMEOUT_SECS:g}s",
        )


_OPEN = "open"


_CODED = "coded"


_COMMIT_REQUIRED_DISPOSITIONS = frozenset({_OPEN, _CODED})


def _row_disposition(row: dict) -> str:
    """Row's disposition, defaulting to 'open' per the schema default (D1).
    A missing/blank/non-string value degrades to 'open' -- the same
    tolerant-read posture `plan_tasks_render.py`'s own `_disposition`
    helper uses for the identical rule (restated here, not imported --
    that helper is private to its own module and the rule is one line)."""
    value = row.get("disposition")
    return value if isinstance(value, str) and value else _OPEN


def _commit_required_chunk_ids(spine_rows: list[Any]) -> list[str]:
    """Chunk-ids requiring a matching commit under the widened
    completeness oracle (AC9, D8): a row's disposition must be `open` or
    `coded` -- `spun_off`/`backlogged`/`wont_do` are excluded exactly the
    way legacy `deferred: true` always has been. `deferred: true` STAYS
    excluded independently of any disposition it may also carry (D8's
    legacy-equivalence -- a deferred row is backlogged-equivalent, never
    commit-required)."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) not in _COMMIT_REQUIRED_DISPOSITIONS:
            continue
        chunk_id = row.get("id")
        if chunk_id:
            ids.append(str(chunk_id))
    return ids


def _plan_deliverable_id(plan_text: str) -> Optional[str]:
    """Reads the plan's own `deliverable_id:` frontmatter field, unquoted
    and comment-stripped (`read_fm_field_unquoted` -- the comparison-safe
    reader, since this value is compared against a git trailer value
    below, not echoed or rewritten verbatim). Returns `None` when the
    plan has no parseable frontmatter, or no `deliverable_id:` field at
    all -- callers treat `None` as "cannot scope the commit search to
    this plan" (see this module's docstring § Deliverable scoping), never
    as "scope to nothing" or "fall back to unscoped"."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "deliverable_id")


def _all_spine_ids(spine_rows: list[Any]) -> list[str]:
    """Every id the plan's own spine names, regardless of `disposition`/
    `deferred`. Kept as a standalone reader (used by `cascade_baton_rows.py`
    and `plan_tasks_spine_drift_check.py`'s own private local copies of the
    C4-owned join machinery) even though this module's own `_determine_
    shipped` no longer needs the full candidate set itself (C3, 2026-08-21
    -- see that function's docstring). Deliberately WIDER than
    `_commit_required_chunk_ids`'s own filtered subset: a
    `spun_off`/`backlogged`/legacy-`deferred` row is still a REAL spine id
    that a commit subject may legitimately reference (e.g. alongside
    commit-required ids in the same compound subject), and excluding it
    from the bounding set would only reintroduce a narrower version of the
    same false-negative this fix closes, for no false-positive benefit --
    a row not in `_commit_required_chunk_ids` is already never consulted
    for the missing/shipped verdict regardless of whether it appears here."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        chunk_id = row.get("id")
        if chunk_id:
            ids.append(str(chunk_id))
    return ids


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def _row_key_line_indices(
    lines: list[str], start: int, end: int, content_indent: int
) -> dict[str, int]:
    """Within row-span `[start, end)`, finds the line index of each of
    this row's own `disposition:` / `disposition_ref:` / `disposition_detail:`
    / `deferred:` keys -- matched ONLY at exactly `content_indent` (the
    row's own top-level key indent, never a deeper nested line) so a
    `body: |` block scalar's continuation text that happens to contain
    one of these words can never be mistaken for the key itself.

    `disposition_detail` is listed BEFORE the bare `disposition` alternative
    for readability only -- this is defensive, not correctness-load-bearing.
    Keeps only the FIRST occurrence of each key (a
    well-formed row never repeats a key; a duplicate is not this
    function's problem to police)."""
    key_re = re.compile(
        r"^"
        + re.escape(" " * content_indent)
        + r"(disposition_detail|disposition_ref|disposition|deferred):[ \t]"
    )
    found: dict[str, int] = {}
    for idx in range(start, end):
        match = key_re.match(lines[idx])
        if match:
            found.setdefault(match.group(1), idx)
    return found


_ROW_KEY_LINE_RE = re.compile(r"^([ \t]*)[A-Za-z_][A-Za-z0-9_]*:([ \t]|$)")


def _measure_row_content_indent(
    lines: list[str], start: int, end: int, dash_indent: int
) -> int:
    """Measures a row's actual child-key indent from its own body, rather
    than assuming `yaml.safe_dump`'s `dash_indent + 2` default -- the
    formatting this fix exists to STOP imposing, since the file is no
    longer re-dumped and a row's real indent may be whatever a human (or a
    different emitter) left there.

    Review: code-reviewer -- F4: `content_indent = dash_indent + 2` was
    assumed, not measured, so a non-default child-key indent made every
    key read as absent and both stamp lines landed at the wrong indent.

    Scans the row's span (excluding the dash line itself, since `id:`
    shares that line and does not establish a sibling-key indent) for any
    YAML mapping-key line strictly deeper than `dash_indent`, and returns
    the SHALLOWEST such indent -- the row's own top-level sibling keys sit
    at the shallowest indent among the row's lines; anything deeper is
    nested content (a `body: |` block scalar's continuation, etc). Falls
    back to `dash_indent + 2` only when the row has no other key line at
    all to measure against."""
    indents = [
        len(match.group(1))
        for idx in range(start + 1, end)
        if (match := _ROW_KEY_LINE_RE.match(lines[idx])) and len(match.group(1)) > dash_indent
    ]
    return min(indents) if indents else dash_indent + 2


def _stamp_rows_in_body(
    body: str,
    updates: dict[str, str],
    details: Optional[dict[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Line-level (never round-tripping) stamp of `disposition: coded` /
    `disposition_ref: <sha>` / `disposition_detail: <prose>` onto every row
    named in `updates` (chunk-id -> covering commit sha), leaving every
    other line of `body` byte-identical -- comments, blank lines, quoting,
    key order, and block scalars all survive untouched, unlike the prior
    `yaml.safe_dump` round-trip this replaces (see this module's docstring
    § the original defect this fixes: a fence-body comment or `|` block
    scalar silently lost or reformatted by a full re-dump of a lossy
    `yaml.safe_load`).

    `details` (chunk-id -> `disposition_detail` prose, e.g. a covering
    commit's own subject line) is OPTIONAL and independent of `updates`'
    own key set: an id present in `updates` but absent from (or not passed
    at all in) `details` gets no `disposition_detail` line written for it
    at all -- this function's own direct unit tests rely on that to
    exercise the disposition/disposition_ref-only shape without also
    needing a detail fixture. This module's own former caller
    (`_auto_resolve_committed_open_rows`, deleted C3 2026-08-21) always
    paired every `updates` id with a `details` entry (DR-103: "
    disposition_detail holds prose and is required on every non-open row"),
    since a `coded` row it stamped was never `open` -- `cascade_baton_
    rows.py` (this function's current live caller) preserves that same
    pairing discipline. The value is written
    through `serialize_yaml_scalar` -- a commit subject routinely carries
    `:`, `#`, or quote characters that are YAML-structural if emitted bare.

    Per row: if `disposition:` / `disposition_ref:` / `disposition_detail:`
    already exist at the row's own key indent, their lines are REPLACED in
    place (never duplicated). Any key still missing is INSERTED as a new
    line, positioned immediately after the row's `deferred:` key if
    present, otherwise at the very end of the row's own span (i.e. after
    any trailing `body: |` block-scalar continuation, never spliced into
    the middle of one).

    Trailing-newline preservation (defect fix, 2026-08-01 -- the
    false-positive fidelity refusal reported by example-cockpit-repo-em):
    `locate_fenced_block(...).span` hands this function a body whose FINAL
    LINE TERMINATOR LIVES OUTSIDE THE SPAN -- for a real plan, `body` ends
    `'  deferred: false'` with no `\\n`, and the `'\\n'` that logically
    terminated it is the first character of `plan_text[end:]`. When the
    last row's stamp lines are inserted at the very end of the body, the
    insertion fixup below has to newline-terminate that previously-final
    line; without the compensating strip at the return, the caller's
    `plan_text[:start] + new_body + plan_text[end:]` reassembly then
    emits BOTH that added newline and the span-external one, planting a
    bare blank line between the last `disposition_detail:` line and the
    closing fence -- which `_assert_stamp_fidelity` correctly refuses
    (deterministically, on every retry). So: whether `body` ended with a
    line terminator is captured up front and restored at the return. A
    body that already ended with one is unaffected -- the strip is a
    no-op there, so no pre-existing behavior changes.

    Returns `(new_body, error)`. `error` is set only when `updates` names
    a chunk-id this scan cannot locate a row for -- a caller/oracle
    mismatch that must fail loud rather than silently stamp nothing."""
    details = details or {}
    body_ended_with_newline = body.endswith(("\n", "\r"))
    lines = body.splitlines(keepends=True)
    spans = _find_row_spans(lines)
    span_by_id = {chunk_id: (start, end) for start, end, chunk_id in spans}

    missing = sorted(set(updates) - set(span_by_id))
    if missing:
        return None, f"could not locate a row for chunk-id(s) {missing!r} to stamp"

    # Process rows in REVERSE row-order so an earlier row's insertion never
    # shifts a later row's already-computed line indices out from under it.
    for start, end, chunk_id in sorted(spans, key=lambda s: s[0], reverse=True):
        if chunk_id not in updates:
            continue
        sha = updates[chunk_id]
        detail = details.get(chunk_id)

        dash_line = lines[start]
        dash_indent = len(dash_line) - len(dash_line.lstrip(" \t"))
        # Review: code-reviewer -- F4: measure the row's actual sibling-key
        # indent instead of assuming yaml.safe_dump's `dash_indent + 2`
        # default, which this fix exists to stop imposing on the file.
        content_indent = _measure_row_content_indent(lines, start, end, dash_indent)
        newline = _line_ending(dash_line)

        keys = _row_key_line_indices(lines, start + 1, end, content_indent)
        pad = " " * content_indent
        disposition_line = f"{pad}disposition: {_CODED}{newline}"
        # `numeric_quoting=True` is load-bearing, not defensive. An abbreviated
        # commit sha is hex, so ~2.3% of them ((10/16)**8) are all-digit --
        # roughly one commit in 43. Emitted bare, YAML parses such a sha as an
        # INT, and the row then fails plan-tasks.schema.json's `type: string`
        # on disposition_ref. That is not hypothetical: a real auto-resolve run
        # (1576648b) wrote `disposition_ref: 17519732` into
        # docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md, and the
        # write-time spine guard flags it to this day. Same reasoning, same
        # flag, as `execution_authorized_sha` in review_assemble/exec_auth_stamp.py.
        disposition_ref_line = (
            f"{pad}disposition_ref: {serialize_yaml_scalar(sha, numeric_quoting=True)}{newline}"
        )
        disposition_detail_line = (
            f"{pad}disposition_detail: {serialize_yaml_scalar(detail)}{newline}"
            if detail is not None
            else None
        )

        if "disposition" in keys:
            lines[keys["disposition"]] = disposition_line
        if "disposition_ref" in keys:
            lines[keys["disposition_ref"]] = disposition_ref_line
        if disposition_detail_line is not None and "disposition_detail" in keys:
            lines[keys["disposition_detail"]] = disposition_detail_line

        to_insert = []
        if "disposition" not in keys:
            to_insert.append(disposition_line)
        if "disposition_ref" not in keys:
            to_insert.append(disposition_ref_line)
        if disposition_detail_line is not None and "disposition_detail" not in keys:
            to_insert.append(disposition_detail_line)

        if to_insert:
            insert_at = keys["deferred"] + 1 if "deferred" in keys else end
            if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r\n")):
                # The line we are about to insert after has no trailing
                # newline (only possible when it is the body's last line
                # with no final newline) -- add one so the new line does
                # not get glued onto the end of it.
                lines[insert_at - 1] += newline
            lines[insert_at:insert_at] = to_insert

    new_body = "".join(lines)
    if not body_ended_with_newline and new_body.endswith(("\n", "\r")):
        # Restore the body's own trailing-newline property (see this
        # function's docstring § Trailing-newline preservation): strip
        # exactly ONE line terminator, `\r\n` before `\n` so a CRLF body
        # loses the pair rather than being left with a dangling `\r`.
        if new_body.endswith("\r\n"):
            new_body = new_body[:-2]
        else:
            new_body = new_body[:-1]
    return new_body, None


def _row_span_containing(
    spans: list[tuple[int, int, str]], idx: int
) -> Optional[tuple[int, int]]:
    """Finds the `(start, end)` row-span (as returned by `_find_row_spans`)
    that contains line-index `idx`, or `None` if `idx` falls outside every
    row (e.g. the body has no rows at all)."""
    for start, end, _chunk_id in spans:
        if start <= idx < end:
            return start, end
    return None
