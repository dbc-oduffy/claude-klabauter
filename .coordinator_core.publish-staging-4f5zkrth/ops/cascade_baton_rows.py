"""
coordinator_core.ops.cascade_baton_rows — AC6g baton-row depth for
`deliverable.cascade_terminal` (`coordinator_core/ops/deliverable_cascade.py`).

Purpose: R1a's cascade depth is NOT satisfied by flipping a roadmap-baton
handoff terminal alone — the baton carries its own `## Tasks` row spine
(same fenced-YAML shape and parser `execute_plan_assemble/close_out_and_stamp.py`
already established for a plan's spine, see `docs/wiki/writing-plans.md`
§ Machine-Parseable Task Spine), and a row inside it can be genuinely
uncommitted, deferred, or ruled-out even after the baton itself ships. This
module closes the gap `deliverable_cascade.py`'s own docstring names and
disclaims ("Does NOT implement AC6g's baton-row depth").
Spec backlink: pln-terminal-state-propagation-giv-c85539
§ C6g (AC6g).

WRITE RULE (evidence-joined, never blanket — corrects an earlier
over-application of R1a, plan finding 2): this module flips a row ONLY
where git already says the covering chunk committed, joined via the SAME
`_committed_chunk_shas`/`_committed_id_covers_spine_id` path
`_auto_resolve_committed_open_rows` (`close_out_and_stamp.py`, AC8) already
uses for a plan's own spine — mirrored here, not reimplemented, per this
chunk's explicit instruction. A row with NO commit evidence is left
`open` and named in the returned `unresolved` list; a baton flipped
terminal with unresolved rows is a legitimate outcome (some rows are
deferred or ruled-out on purpose — this very plan carries D1) as long as
every such row is named, which is exactly what `unresolved` gives the
caller (`deliverable_cascade._handler`) to fold into AC6e's provenance
record.

Row-level provenance: every row this module advances also gets
`advanced_by: <deliverable_id>` / `advanced_at: <timestamp>` written onto
the ROW ITSELF (not just the handoff's own frontmatter) — the same two
field names AC6e/C6 already established at handoff-frontmatter depth,
carried down to row depth per this chunk's explicit HARD DEPENDENCY note:
C6d's retraction (AC6f) runs immediately after this chunk and indexes off
these exact markers to know which rows IT wrote versus which a human or
another process touched since. Thin row provenance would make row-depth
retraction unbuildable.

Live substrate note (verified at authorship time, 2026-08-04): zero live
`state/handoffs/*.md` records of `kind: roadmap-baton` currently carry a
`## Tasks` fenced spine in their own body — every live roadmap-baton
handoff today is flat (gate-narrative frontmatter only, no row content).
This does NOT make the mechanism unbuildable (a prior dispatch of this
chunk reported the absence and stopped there) — `locate_fenced_block`
returning `LocateStatus.ABSENT` is this module's ordinary, honest "nothing
to resolve" outcome (mirrors D7's absent-spine-is-fully-shipped posture in
`_parse_spine_rows`), and the mechanism is proven here with a constructed
fixture in this chunk's own test module, exactly as C11 constructs
opticon's four ground-truth instances rather than waiting for a live one.

Negative-spec:
  - Does NOT re-derive commit-coverage matching. `_commit_required_chunk_ids`/
    `_parse_spine_rows` are still imported from `close_out_and_stamp.py`
    verbatim. `_committed_chunk_shas`/`_committed_id_covers_spine_id` are
    RELOCATED here as private local copies (C4, 2026-08-20 plan "the close
    ceremony stops paying for the join") — this module is their one
    remaining consumer once C3, same plan, removes the close path's own
    copies, which no longer have a caller. Still not reimplemented: same
    bodies, same rules, moved rather than rewritten. Open question logged
    to the kill-ledger for a later audit: whether AC6g's chunk-to-commit
    join is required at all.
  - Does NOT touch a row already at a non-`open` disposition, or a row
    carrying `deferred: true` — mirrors `_auto_resolve_committed_open_rows`'s
    own skip rules exactly; those rows are not this module's business.
  - Does NOT decide whether the OWNING baton itself advances — that is
    `deliverable_cascade.py`'s existing per-target predicate (AC6h). This
    module is called ONLY for a candidate `deliverable_cascade` has
    already decided to advance, as the row-depth half of that same
    decision.
  - Does NOT scan `archive/handoffs/` — same live-only containment
    discipline as `deliverable_cascade.py`'s own candidate collection.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
    _OPEN,
    _all_spine_ids,
    _commit_required_chunk_ids,
    _find_row_spans,
    _find_row_spans_in_plan,
    _line_ending,
    _measure_row_content_indent,
    _parse_spine_rows,
    _plan_deliverable_id,
    _row_disposition,
    _row_span_containing,
    _run_git,
    _stamp_rows_in_body,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    serialize_yaml_scalar,
    split_frontmatter,
)
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

#: Sibling of `close_out_and_stamp._SINGLE_LETTER_SUFFIX_RE`/
#: `_DASH_TAG_SUFFIX_RE`/`_TRAILING_DIGITS_SUFFIX_RE`/`_ADJACENCY_DASH_TAGS`,
#: kept as a private local copy per this chunk's explicit relocation
#: instruction (C4, 2026-08-20 plan "the close ceremony stops paying for the
#: join"): `_committed_chunk_shas`/`_committed_id_covers_spine_id` are the
#: two join primitives this module's own docstring names as its sole
#: dependency on `close_out_and_stamp.py`'s AC8 machinery, and that module is
#: losing both (C3, same plan) once the close path stops paying for a join
#: it no longer needs. This module still needs them for AC6g baton-row
#: depth (a DIFFERENT requirement in a DIFFERENT op -- see this module's own
#: docstring), so it now carries its own copies rather than importing a
#: symbol about to disappear. Gap 1 (C4, 2026-08-21 second pass, measured
#: against disk): the relocated bodies below themselves reached back into
#: `close_out_and_stamp.py` for `_extract_chunk_ids`, `DeliverableJoinStats`
#: and `_chunk_evidence_log_lines` -- all three are on C3's deletion list
#: too, so those move here as well, along with THEIR OWN transitive closure
#: (`_chunk_evidence_log_range`, `_deliverable_log_records`,
#: `_resolve_deliverable_id`, `_first_deliverable_commit_range_base`,
#: `_plan_execution_authorized_sha`, the `_LOG_RECORD_SEP`/`_LOG_FIELD_SEP`
#: record shape and `_DELIVERABLE_ID_BODY_LINE_RE`) -- none of which is used
#: outside this doomed evidence-join closure per an AST call-site check
#: against the whole module, so none of it survives C3 either.
#: `_plan_deliverable_id` alone stays IMPORTED, not copied: it has other
#: live callers inside `close_out_and_stamp.py` untouched by C3's deletion
#: list, so it is not going away.
_SINGLE_LETTER_SUFFIX_RE = re.compile(r"^[a-z]$")
_DASH_TAG_SUFFIX_RE = re.compile(r"^-[a-z][a-z0-9]*$")
_TRAILING_DIGITS_SUFFIX_RE = re.compile(r"^\d+$")

#: Verbatim copy of `close_out_and_stamp._ADJACENCY_DASH_TAGS` -- see that
#: constant's own docstring for why `pre`/`prep`/`post` are excluded from
#: the dash-tag-suffix covers-check (adjacency, not variance).
_ADJACENCY_DASH_TAGS = frozenset({"pre", "prep", "post"})


def _committed_id_covers_spine_id(committed_id: str, spine_id: str) -> bool:
    """Private local copy of `close_out_and_stamp._committed_id_covers_
    spine_id`, relocated here (not reimplemented -- same body, same rules)
    per this chunk's brief: this module is the join primitive's one
    remaining consumer once C3 removes the close path's own copy. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full three-suffix-shape rationale this copy preserves exactly:
    a single trailing lowercase letter (sub-chunk expansion, `C1a` covers
    `C1`), a trailing dash-tag (`C8a-doe` covers `C8a`, except the
    adjacency-tag exclusion set `_ADJACENCY_DASH_TAGS`), or trailing digits
    on a non-digit-ending base id (`C6a1` covers `C6a`, `C11` never covers
    `C1`)."""
    if committed_id == spine_id:
        return True
    if not committed_id.startswith(spine_id):
        return False
    suffix = committed_id[len(spine_id):]
    if _SINGLE_LETTER_SUFFIX_RE.match(suffix):
        return True
    if _DASH_TAG_SUFFIX_RE.match(suffix):
        return suffix[1:].lower() not in _ADJACENCY_DASH_TAGS
    if spine_id and not spine_id[-1].isdigit() and _TRAILING_DIGITS_SUFFIX_RE.match(suffix):
        return True
    return False


#: Private local copy of `close_out_and_stamp._CHUNK_ID_LIST_GRAMMAR` --
#: relocated (Gap 1, C4 second pass) as part of `_extract_chunk_ids`'s own
#: transitive closure. See the original's docstring (module history,
#: `close_out_and_stamp.py`) for the full corpus-derived separator-set
#: rationale (`,`/`+`/`/`, optional paren-suffix per token).
_CHUNK_ID_LIST_GRAMMAR = (
    r"[A-Za-z0-9._'-]+(?:\([^()]*\))?"
    r"(?:(?:,\s*|\s*[+/]\s*)[A-Za-z0-9._'-]+(?:\([^()]*\))?)*"
)

#: Private local copy of `close_out_and_stamp._CHUNK_SUBJECT_RE`.
_CHUNK_SUBJECT_RE = re.compile(rf"^({_CHUNK_ID_LIST_GRAMMAR}):\s")

#: Private local copy of `close_out_and_stamp._CHUNK_SUBJECT_PREFIXED_RE` --
#: see the original's docstring for the contiguity bound (id-list sits
#: immediately before the subject's `: `, with only whitespace-separated
#: prefix words ahead of it).
_CHUNK_SUBJECT_PREFIXED_RE = re.compile(
    rf"^(?:\S+\s+)+?({_CHUNK_ID_LIST_GRAMMAR}):\s"
)

_CHUNK_ID_PAREN_SUFFIX_RE = re.compile(r"\([^()]*\)$")


def _strip_chunk_id_paren_suffix(token: str) -> str:
    """Private local copy of
    `close_out_and_stamp._strip_chunk_id_paren_suffix`. Strips one trailing,
    non-nested `(...)` group off an already-extracted id token
    (`C16(composition-invocation-budgets)` -> `C16`); a token with no such
    suffix is returned unchanged."""
    return _CHUNK_ID_PAREN_SUFFIX_RE.sub("", token)


#: Private local copy of `close_out_and_stamp._CHUNK_ID_SHAPE_RE` --
#: fallback-only shape gate `_extract_chunk_ids`'s multi-id split uses ONLY
#: when no `spine_ids` context is supplied at all.
_CHUNK_ID_SHAPE_RE = re.compile(r"^C\d")


def _extract_chunk_ids(
    subject: str, spine_ids: Optional[Iterable[str]] = None
) -> list[str]:
    """Private local copy of `close_out_and_stamp._extract_chunk_ids`,
    relocated here (Gap 1, C4 second pass) as part of this module's own
    join-primitive transitive closure -- same body, same rules, not
    reimplemented. See that function's original docstring (module history,
    `close_out_and_stamp.py`) for the full separator-grammar, bounding, and
    known-false-negative rationale this copy preserves exactly."""
    match = _CHUNK_SUBJECT_RE.match(subject)
    if not match:
        match = _CHUNK_SUBJECT_PREFIXED_RE.match(subject)
    if not match:
        return []
    raw = match.group(1)
    tokens = [
        _strip_chunk_id_paren_suffix(token)
        for token in re.findall(r"[A-Za-z0-9._'-]+(?:\([^()]*\))?", raw)
    ]
    if len(tokens) == 1:
        bare = tokens[0]
        if spine_ids is not None:
            if any(_committed_id_covers_spine_id(bare, spine_id) for spine_id in spine_ids):
                return [bare]
            return []
        return [bare]
    if spine_ids is not None:
        spine_id_list = list(spine_ids)
        return [
            token
            for token in tokens
            if any(_committed_id_covers_spine_id(token, spine_id) for spine_id in spine_id_list)
        ]
    return [token for token in tokens if _CHUNK_ID_SHAPE_RE.match(token)]


@dataclasses.dataclass(frozen=True)
class DeliverableJoinStats:
    """Private local copy of `close_out_and_stamp.DeliverableJoinStats`,
    relocated here (Gap 1, C4 second pass) -- same four fields, same
    meaning. See that class's original docstring (module history,
    `close_out_and_stamp.py`) for the full `attempted`/`trailered_commit_
    count`/`matched_commit_count`/`trailer_matched_no_chunk_id_count`
    rationale this copy preserves exactly."""

    attempted: bool
    trailered_commit_count: int
    matched_commit_count: int
    trailer_matched_no_chunk_id_count: int


#: Private local copy of `close_out_and_stamp._LOG_RECORD_SEP`/
#: `_LOG_FIELD_SEP` -- relocated (Gap 2, C4 second pass) as part of
#: `_chunk_evidence_log_lines`'s own transitive closure. See the originals'
#: docstring (module history, `close_out_and_stamp.py`) for why ASCII
#: RS/US, not a newline, delimit this `git log` shape's records/fields.
_LOG_RECORD_SEP = "\x1e"
_LOG_FIELD_SEP = "\x1f"

#: Private local copy of `close_out_and_stamp._DELIVERABLE_ID_BODY_LINE_RE`.
_DELIVERABLE_ID_BODY_LINE_RE = re.compile(
    r"^Deliverable-Id:[ \t]*(\S[^\r\n]*?)[ \t]*$", re.MULTILINE
)


def _resolve_deliverable_id(trailer_block: str, body: str) -> str:
    """Private local copy of `close_out_and_stamp._resolve_deliverable_id`,
    relocated here (Gap 2, C4 second pass) -- same trailer-first, body-
    fallback join-key resolution. See that function's original docstring
    (module history, `close_out_and_stamp.py`) for the full trailer-
    demotion-defect rationale this copy preserves exactly."""
    for candidate in trailer_block.splitlines():
        value = candidate.strip()
        if value:
            return value
    matches = _DELIVERABLE_ID_BODY_LINE_RE.findall(body)
    if matches:
        return matches[-1].strip()
    return ""


def _deliverable_log_records(
    repo_root: Path, log_args: Sequence[str], full_sha: bool = False
) -> tuple:
    """Private local copy of `close_out_and_stamp._deliverable_log_records`,
    relocated here (Gap 2, C4 second pass) -- same single-producer `git log`
    shape, same record parse, same message-line fallback. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full `full_sha`/record-separator rationale this copy preserves
    exactly. Returns `(query_ok, [(sha, subject, deliverable_id)])`."""
    sha_atom = "%H" if full_sha else "%h"
    result = _run_git(
        [
            "log",
            "--format="
            + _LOG_RECORD_SEP
            + sha_atom
            + _LOG_FIELD_SEP
            + "%s"
            + _LOG_FIELD_SEP
            + "%(trailers:key=Deliverable-Id,valueonly)"
            + _LOG_FIELD_SEP
            + "%B",
            *log_args,
        ],
        repo_root,
    )
    if result.returncode != 0:
        return False, []
    records: list = []
    for raw_record in (result.stdout or "").split(_LOG_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        records.append((sha, fields[1], _resolve_deliverable_id(fields[2], fields[3])))
    return True, records


def _plan_execution_authorized_sha(plan_text: str) -> Optional[str]:
    """Private local copy of
    `close_out_and_stamp._plan_execution_authorized_sha`, relocated here
    (Gap 2, C4 second pass) -- reads the plan's own
    `execution_authorized_sha:` frontmatter field, unquoted. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full content-binding-witness rationale this copy preserves
    exactly. `None` when the plan has no parseable frontmatter or no such
    field."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "execution_authorized_sha")


def _first_deliverable_commit_range_base(
    repo_root: Path, deliverable_id: Optional[str]
) -> Optional[str]:
    """Private local copy of
    `close_out_and_stamp._first_deliverable_commit_range_base`, relocated
    here (Gap 2, C4 second pass) -- same earliest-commit-for-this-
    deliverable lookup, same parent-sha return. See that function's
    original docstring (module history, `close_out_and_stamp.py`) for the
    full range rationale this copy preserves exactly."""
    if not deliverable_id:
        return None
    query_ok, records = _deliverable_log_records(repo_root, ["--reverse", "HEAD"], full_sha=True)
    if not query_ok:
        return None
    for commit_sha, _subject, trailer_value in records:
        if not trailer_value:
            continue
        if trailer_value != deliverable_id:
            continue
        parent_result = _run_git(["rev-parse", "--verify", "--quiet", f"{commit_sha}^"], repo_root)
        parent_sha = (parent_result.stdout or "").strip()
        if parent_result.returncode == 0 and parent_sha:
            return parent_sha
        return ""
    return None


def _chunk_evidence_log_range(
    repo_root: Path, plan_text: Optional[str] = None
) -> list[str]:
    """Private local copy of `close_out_and_stamp._chunk_evidence_log_range`,
    relocated here (Gap 2, C4 second pass) -- same rung ladder
    (`execution_authorized_sha:` literal, earliest-deliverable-commit base,
    `merge-base origin/main HEAD`, bare `HEAD`). See that function's
    original docstring (module history, `close_out_and_stamp.py`) for the
    full range-fix rationale this copy preserves exactly."""
    if plan_text is not None:
        sha = _plan_execution_authorized_sha(plan_text)
        if sha:
            resolved = _run_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], repo_root)
            resolved_sha = (resolved.stdout or "").strip()
            if resolved.returncode == 0 and resolved_sha:
                return [f"{resolved_sha}..HEAD"]

        deliverable_id = _plan_deliverable_id(plan_text)
        base = _first_deliverable_commit_range_base(repo_root, deliverable_id)
        if base is not None:
            if base == "":
                return ["HEAD"]
            return [f"{base}..HEAD"]

    merge_base_result = _run_git(["merge-base", "origin/main", "HEAD"], repo_root)
    base_sha = (merge_base_result.stdout or "").strip()
    if merge_base_result.returncode == 0 and base_sha:
        return [f"{base_sha}..HEAD"]
    return ["HEAD"]


def _chunk_evidence_log_lines(
    repo_root: Path, plan_text: Optional[str] = None
) -> tuple:
    """Private local copy of `close_out_and_stamp._chunk_evidence_log_lines`,
    relocated here (Gap 2, C4 second pass) -- same single `git log` query
    every chunk-evidence caller needs, same tab-separated
    `<short-sha>\\t<subject>\\t<deliverable-id>` line shape. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full rationale this copy preserves exactly. Returns
    `(query_ok, lines, log_range)`."""
    log_range = _chunk_evidence_log_range(repo_root, plan_text)
    query_ok, records = _deliverable_log_records(repo_root, log_range)
    if not query_ok:
        return False, [], log_range
    return True, [f"{sha}\t{subject}\t{deliverable_id}" for sha, subject, deliverable_id in records], log_range


def _committed_chunk_shas(
    repo_root: Path,
    deliverable_id: Optional[str],
    spine_ids: Optional[list] = None,
    plan_text: Optional[str] = None,
) -> tuple:
    """Private local copy of `close_out_and_stamp._committed_chunk_shas`,
    relocated here per this chunk's brief -- same `Deliverable-Id:` trailer
    join, same query, same range, same `_extract_chunk_ids` convention (the
    sha is the leading `%h` token on each matching line). See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full join-semantics rationale.

    NARROWED, not reimplemented: `close_out_and_stamp._committed_chunk_shas`
    also carries a `plan_path_rel`-gated Session-Id fallback leg (2026-08-10,
    plan C6, finding 0) for its OWN caller, `_auto_resolve_committed_open_
    rows`. This module's one call site (`resolve_baton_rows`, below) never
    passes a `plan_path_rel` -- the fallback is dead code at this call site
    in the original too (`if matched_commit_count == 0 and plan_path_rel:`
    never fires on `plan_path_rel=None`) -- so it is not carried over here;
    resurrecting it would require also relocating `_plan_claim_holder_
    session_id`/`_session_id_fallback_evidence`, machinery this module has
    no use for. Returns `(query_ok, committed_ids, committed_shas,
    join_stats)`, identical shape to the original."""
    query_ok, log_lines, _log_range = _chunk_evidence_log_lines(repo_root, plan_text)
    if not query_ok:
        return (
            False,
            set(),
            {},
            DeliverableJoinStats(
                attempted=bool(deliverable_id),
                trailered_commit_count=0,
                matched_commit_count=0,
                trailer_matched_no_chunk_id_count=0,
            ),
        )

    committed: set = set()
    committed_shas: Dict[str, str] = {}
    trailered_commit_count = 0
    matched_commit_count = 0
    trailer_matched_no_chunk_id_count = 0
    for line in log_lines:
        parts = line.split("\t", 2)
        if len(parts) < 2 or not parts[0]:
            continue
        sha = parts[0]
        subject = parts[1]
        trailer_value = parts[2].strip() if len(parts) > 2 else ""
        if trailer_value:
            trailered_commit_count += 1
        if not deliverable_id or trailer_value != deliverable_id:
            continue
        subject_chunk_ids = _extract_chunk_ids(subject, spine_ids)
        if not subject_chunk_ids:
            trailer_matched_no_chunk_id_count += 1
            continue
        matched_commit_count += 1
        for chunk_id in subject_chunk_ids:
            committed.add(chunk_id)
            committed_shas.setdefault(chunk_id, sha)

    join_stats = DeliverableJoinStats(
        attempted=bool(deliverable_id),
        trailered_commit_count=trailered_commit_count,
        matched_commit_count=matched_commit_count,
        trailer_matched_no_chunk_id_count=trailer_matched_no_chunk_id_count,
    )
    return True, committed, committed_shas, join_stats

#: Widened sibling of `close_out_and_stamp._STAMP_LINE_RE` — this module's
#: fidelity gate must additionally tolerate the two row-provenance fields
#: it writes that the plan-spine stamper never touches.
_ROW_STAMP_LINE_RE = re.compile(
    r"^[ \t]*(disposition(?:_ref|_detail)?|advanced_by|advanced_at):[ \t]"
)


def _row_provenance_key_line_indices(
    lines: list[str], start: int, end: int, content_indent: int
) -> Dict[str, int]:
    """`close_out_and_stamp._row_key_line_indices`'s sibling for this
    module's own two provenance keys — same exactly-`content_indent`
    matching discipline (never a deeper nested `body: |` continuation
    line), kept as a local twin rather than widening that function's own
    hardcoded key set for a field it has no other reason to know about."""
    key_re = re.compile(
        r"^" + re.escape(" " * content_indent) + r"(advanced_by|advanced_at):[ \t]"
    )
    found: Dict[str, int] = {}
    for idx in range(start, end):
        match = key_re.match(lines[idx])
        if match:
            found.setdefault(match.group(1), idx)
    return found


def _stamp_row_provenance(
    body: str, chunk_ids: set, deliverable_id: str, advanced_at: str
) -> str:
    """Inserts/replaces `advanced_by: <deliverable_id>` / `advanced_at:
    <advanced_at>` onto every row in `chunk_ids`, leaving every other line
    of `body` byte-identical — same line-level-splice, never-round-trip
    discipline as `_stamp_rows_in_body`, applied as a SECOND pass over a
    body that has already had that function's disposition stamp applied
    (row spans are re-measured fresh from `body`'s own current lines each
    call, so the two passes compose safely regardless of how the first
    pass shifted line numbers)."""
    body_ended_with_newline = body.endswith(("\n", "\r"))
    lines = body.splitlines(keepends=True)
    spans = _find_row_spans(lines)

    for start, end, chunk_id in sorted(spans, key=lambda s: s[0], reverse=True):
        if chunk_id not in chunk_ids:
            continue
        dash_line = lines[start]
        dash_indent = len(dash_line) - len(dash_line.lstrip(" \t"))
        content_indent = _measure_row_content_indent(lines, start, end, dash_indent)
        newline = _line_ending(dash_line)

        keys = _row_provenance_key_line_indices(lines, start, end, content_indent)
        pad = " " * content_indent
        advanced_by_line = f"{pad}advanced_by: {serialize_yaml_scalar(deliverable_id)}{newline}"
        advanced_at_line = f"{pad}advanced_at: {serialize_yaml_scalar(advanced_at)}{newline}"

        if "advanced_by" in keys:
            lines[keys["advanced_by"]] = advanced_by_line
        if "advanced_at" in keys:
            lines[keys["advanced_at"]] = advanced_at_line

        to_insert = []
        if "advanced_by" not in keys:
            to_insert.append(advanced_by_line)
        if "advanced_at" not in keys:
            to_insert.append(advanced_at_line)

        if to_insert:
            insert_at = end
            if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r\n")):
                lines[insert_at - 1] += newline
            lines[insert_at:insert_at] = to_insert

    new_body = "".join(lines)
    if not body_ended_with_newline and new_body.endswith(("\n", "\r")):
        if new_body.endswith("\r\n"):
            new_body = new_body[:-2]
        else:
            new_body = new_body[:-1]
    return new_body


def _assert_row_stamp_fidelity(
    old_text: str, new_text: str, path_rel: str
) -> Optional[str]:
    """`close_out_and_stamp._assert_stamp_fidelity`'s sibling, widened to
    this module's own field set (see `_ROW_STAMP_LINE_RE`). Independently
    diffs actual before/after text via `difflib.SequenceMatcher` rather
    than trusting either stamp pass's own bookkeeping — the same
    correctness backstop that fix's own docstring describes, applied here
    to a 4-field write (`disposition`/`disposition_ref`/`disposition_detail`
    plus `advanced_by`/`advanced_at`) instead of that function's 3."""
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    spans = _find_row_spans_in_plan(old_lines, old_text)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            first = old_lines[i1] if i1 < len(old_lines) else ""
            return (
                f"{path_rel}: refusing to write baton-row stamp -- an original "
                f"line was removed where only a row stamp change was expected "
                f"(first diverging line: {first!r})"
            )

        anchor = i1 - 1 if i1 > 0 else i1
        span = _row_span_containing(spans, anchor)
        if span is None:
            span = _row_span_containing(spans, i1)
        expected_indent = None
        if span is not None:
            row_start, row_end = span
            dash_line = old_lines[row_start]
            dash_indent = len(dash_line) - len(dash_line.lstrip(" \t"))
            expected_indent = _measure_row_content_indent(
                old_lines, row_start, row_end, dash_indent
            )

        touched = old_lines[i1:i2] + new_lines[j1:j2]
        for line in touched:
            if not _ROW_STAMP_LINE_RE.match(line):
                return (
                    f"{path_rel}: refusing to write baton-row stamp -- a change "
                    "outside the disposition/disposition_ref/disposition_detail/"
                    f"advanced_by/advanced_at fields was found (first diverging "
                    f"line: {line!r})"
                )
            if expected_indent is not None:
                actual_indent = len(line) - len(line.lstrip(" \t"))
                if actual_indent != expected_indent:
                    return (
                        f"{path_rel}: refusing to write baton-row stamp -- a "
                        f"stamped line landed at indent {actual_indent} but this "
                        f"row's own content indent is {expected_indent} (first "
                        f"diverging line: {line!r})"
                    )
    return None


def _batch_commit_subjects(repo_root: Path, shas: set) -> Dict[str, str]:
    """One `git log` call resolving every `sha` in `shas` to its own commit
    subject line, replacing what would otherwise be a per-row
    `_commit_subject` spawn inside `resolve_baton_rows`'s row loop (this
    chunk's N+1 site -- a baton with many resolvable rows previously spawned
    once per row).

    This is an OBJECT question (commit metadata at caller-supplied SHAs, not
    a range), so it batches unconditionally -- same posture as C13's
    `_batch_commit_timestamps` (`coordinator/bin/reap-orphaned-in-flight-
    handoffs.py`, commit 0df3818bc) and `emit/sections/handoffs.
    _resolve_shipped_in_dates`, whose reconciliation shape this mirrors
    rather than re-deriving: one `--no-walk=unsorted --ignore-missing` call,
    prefix-matched output lines back to the requested SHA set via an
    explicit `matched` set, never trusting output-row-count as a proxy for
    "every requested sha resolved".

    `--ignore-missing` exits 0 with an unresolvable sha simply ABSENT from
    stdout -- an unrecognized ref, a sha outside a shallow clone's fetched
    history, or a since-rewritten history. Absence is never read as
    resolved: a sha with no matching output line is simply absent from the
    returned map, and `resolve_baton_rows` falls back to the exact same
    `f"commit {sha} (subject unavailable)"` placeholder `_commit_subject`
    itself used, for a resolved-but-failed-lookup sha and a positively
    absent one alike -- fail-closed, and honest either way.

    Delimiter/parse contract (fork-adjudication.md § 10.3, `%x1f` is the
    house idiom for an untrusted-content field boundary): `%H%x1f%s` per
    commit -- one line per commit, since `%s` is git's own first-line-only
    subject and cannot itself contain `\\n`. Split each line on the FIRST
    `\\x1f` only (`str.partition`, not `str.split` with no maxsplit) so a
    stray `0x1f` byte inside a subject corrupts only the subject value, never
    shifts the sha field before it. Whole-stdout is never `.strip()`ped --
    only `\\r` is stripped and the string is split on `\\n` -- since
    `str.strip()` treats `\\x1f` as whitespace and could otherwise eat a
    delimiter at either edge.
    """
    if not shas:
        return {}
    ordered = sorted({str(s) for s in shas})
    try:
        result = _run_git(
            ["log", "--no-walk=unsorted", "--ignore-missing", "--format=%H%x1f%s", *ordered],
            repo_root,
        )
    except (OSError, ValueError):
        return {}
    stdout = result.stdout or ""
    if result.returncode != 0 or not stdout.strip():
        return {}

    subjects: Dict[str, str] = {}
    matched: set = set()
    for line in stdout.replace("\r", "").split("\n"):
        if "\x1f" not in line:
            continue
        full, _, subject = line.partition("\x1f")
        for raw in ordered:
            if raw not in matched and full[: len(raw)] == raw:
                subjects[raw] = subject
                matched.add(raw)
                break
    return subjects


def resolve_baton_rows(
    candidate_path: Path,
    deliverable_id: str,
    advanced_at: str,
    repo_root: Path,
) -> dict:
    """AC6g entrypoint. Called by `deliverable_cascade._handler` once per
    handoff it has ALREADY decided to advance (never for a refused
    candidate — deciding whether the owning baton advances is entirely
    `deliverable_cascade.py`'s own per-target predicate, AC6h; this
    function only ever resolves the ALREADY-ADVANCING candidate's own
    contained rows).

    Returns:
        {
          "spine_status": "absent" | "located" | "malformed",
          "advanced": [{"row_id": ..., "message": ...}, ...],
          "unresolved": [{"row_id": ..., "reason": ...}, ...],
          "error": <str, present only on a genuine failure -- git-log
                    query broke, the spine was malformed, or the write
                    itself failed>,
        }

    `spine_status: "absent"` (no `## Tasks` block in the handoff's own
    body at all) is the honest, ordinary common case today (see this
    module's docstring § Live substrate note) -- `advanced`/`unresolved`
    are both `[]` and there is no `error`."""
    try:
        text = candidate_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "spine_status": "malformed",
            "advanced": [],
            "unresolved": [],
            "error": f"{candidate_path}: could not read for row resolution: {exc}",
        }

    located = locate_fenced_block(text)
    if located.status == LocateStatus.ABSENT:
        return {"spine_status": "absent", "advanced": [], "unresolved": []}

    path_rel = str(candidate_path)
    rows, parse_error = _parse_spine_rows(text, path_rel)
    if parse_error is not None or rows is None:
        return {
            "spine_status": "malformed",
            "advanced": [],
            "unresolved": [],
            "error": parse_error or f"{path_rel}: baton row spine unparseable",
        }

    chunk_ids = _commit_required_chunk_ids(rows)
    if not chunk_ids:
        return {"spine_status": "located", "advanced": [], "unresolved": []}

    spine_ids = _all_spine_ids(rows)
    query_ok, _committed, committed_shas, _join_stats = _committed_chunk_shas(
        repo_root, deliverable_id, spine_ids
    )
    if not query_ok:
        return {
            "spine_status": "located",
            "advanced": [],
            "unresolved": [],
            "error": (
                f"{path_rel}: git-log query for baton-row commit evidence "
                "failed -- cannot determine row completion mechanically"
            ),
        }

    updates: Dict[str, str] = {}
    details: Dict[str, str] = {}
    unresolved: List[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        chunk_id = row.get("id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        if _row_disposition(row) != _OPEN:
            continue
        sha = next(
            (
                committed_shas[committed_id]
                for committed_id in committed_shas
                if _committed_id_covers_spine_id(committed_id, chunk_id)
            ),
            None,
        )
        if sha is not None:
            updates[chunk_id] = sha
        else:
            unresolved.append(
                {
                    "row_id": chunk_id,
                    "reason": (
                        "no commit evidence resolvable for this row -- left "
                        "open, not flipped (evidence-joined write rule)"
                    ),
                }
            )

    if not updates:
        return {"spine_status": "located", "advanced": [], "unresolved": unresolved}

    # N+1 fix: one batched `git log` for every row's covering-commit subject,
    # rather than one `_commit_subject` spawn per resolved row (see
    # `_batch_commit_subjects`'s own docstring for the reconciliation
    # contract). A sha absent from the batch result -- never conflated with
    # a resolved subject -- falls back to the same placeholder
    # `_commit_subject` itself used on a git-read failure.
    subjects = _batch_commit_subjects(repo_root, set(updates.values()))
    for chunk_id, sha in updates.items():
        details[chunk_id] = subjects.get(sha, f"commit {sha} (subject unavailable)")

    def mutate(old_text: str) -> str:
        located_inner = locate_fenced_block(old_text)
        if located_inner.status != LocateStatus.LOCATED or located_inner.span is None:
            raise MutateAbort(
                f"{path_rel}: '## Tasks' baton spine vanished between read and write"
            )
        start, end = located_inner.span
        body = old_text[start:end]

        new_body, stamp_error = _stamp_rows_in_body(body, updates, details)
        if stamp_error is not None:
            raise MutateAbort(f"{path_rel}: baton-row stamp failed: {stamp_error}")

        new_body = _stamp_row_provenance(new_body, set(updates), deliverable_id, advanced_at)

        new_text = old_text[:start] + new_body + old_text[end:]
        fidelity_error = _assert_row_stamp_fidelity(old_text, new_text, path_rel)
        if fidelity_error is not None:
            raise MutateAbort(fidelity_error)
        return new_text

    try:
        locked_rmw(candidate_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return {
            "spine_status": "located",
            "advanced": [],
            "unresolved": unresolved,
            "error": f"{path_rel}: not found at row-write time",
        }
    except LockTimeout as exc:
        return {
            "spine_status": "located",
            "advanced": [],
            "unresolved": unresolved,
            "error": f"{path_rel}: row-write lock timeout: {exc}",
        }
    except MutateAbort as exc:
        return {
            "spine_status": "located",
            "advanced": [],
            "unresolved": unresolved,
            "error": exc.args[0] if exc.args else "row-write mutation aborted",
        }

    advanced = [
        {
            "row_id": chunk_id,
            "message": f"row advanced (disposition: coded, advanced_by: {deliverable_id})",
        }
        for chunk_id in sorted(updates)
    ]
    return {"spine_status": "located", "advanced": advanced, "unresolved": unresolved}
