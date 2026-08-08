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
Spec backlink: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md
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
Cockpit's four ground-truth instances rather than waiting for a live one.

Negative-spec:
  - Does NOT re-derive commit-coverage matching — every join primitive
    (`_committed_chunk_shas`, `_committed_id_covers_spine_id`,
    `_commit_required_chunk_ids`, `_parse_spine_rows`) is imported from
    `close_out_and_stamp.py` verbatim, never reimplemented.
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

import re
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
    _OPEN,
    _all_spine_ids,
    _commit_required_chunk_ids,
    _committed_chunk_shas,
    _committed_id_covers_spine_id,
    _find_row_spans,
    _find_row_spans_in_plan,
    _line_ending,
    _measure_row_content_indent,
    _parse_spine_rows,
    _row_disposition,
    _row_span_containing,
    _run_git,
    _stamp_rows_in_body,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import serialize_yaml_scalar
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

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
