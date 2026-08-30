"""
coordinator_core.execute_plan_assemble.close_out_and_stamp — mutating
assembler for `/execute-plan` Phase 4's close-out sequence.

Purpose: `/execute-plan`'s Phase 4 (DoE-claude
`coordinator/skills/execute-plan/SKILL.md` § "Phase 4: Commit, Report, and
Offer the Next Step", item 1) narrates a hand-sequenced git close-out as
inline prose -- decide whether every wave-map chunk landed, stamp the plan's
`status:` frontmatter field to `implemented` only on the full-shipped path,
then stage and land one scoped commit covering every changed path plus the
plan doc itself. This module collapses that to ONE named op --
`close_out_and_stamp(plan_path, repo_root=None)` -- so the skill invokes a
single CLI (`close-out-and-stamp <plan-path>`) instead of hand-sequencing
`git add`/`git commit` plus a separate stamp step.

Full-shipped vs. halted determination: reads the plan's `## Tasks`
machine-parseable spine (the single fenced ```yaml plan-tasks``` block
directly under `## Tasks` -- DoE-claude `docs/wiki/writing-plans.md` §
Machine-Parseable Task Spine), takes every commit-required row (disposition
`open`/`coded`, `deferred` absent or `false`), and asks whether it has
verified sha-ancestry evidence of landing -- see § Evidence sources below.
Every non-deferred, commit-required chunk-id having such evidence =
full-shipped; any gap = halted (stamp skipped, remaining chunk-ids
reported).

Evidence sources (C3, 2026-08-21, "the close ceremony stops paying for the
join" -- collapses `2026-07-27`'s original commit-subject/`Deliverable-Id`-
trailer join, its cross-repo sibling-scanning extension, and its AC8
auto-resolve consumer; see `state/handoffs/` and this plan's own row for
the incident history that mechanism accumulated): that whole precision
machinery -- parsing a commit SUBJECT for a `<chunk-id>: ...` prefix,
scoping the search via a `Deliverable-Id:` git trailer, scanning sibling
repos named in `scope:`, and auto-flipping a row's `disposition` from the
inferred match -- is DELETED, not narrowed. It never confirmed a row did
NOT ship (recall across the corpus was measured very low -- see
`coordinator_core/ops/plan_tasks_spine_drift_check.py`'s own docstring for
the corpus numbers this collapse is based on), and every widening it
received (paren-slug suffixes, prefix-then-id subjects, apostrophes,
hyphen-range diagnostics, near-miss diagnostics) was chasing that same
low-recall problem rather than closing it.

Two evidence sources remain, both pure sha-ancestry checks with no subject
parsing:

  1. A `disposition: coded` spine row's own `disposition_ref` field -- a
     commit sha an executor/PM recorded, by hand, inside THIS plan's own
     spine row (`_verify_disposition_ref`/`_disposition_ref_evidence`).
     Counts as evidence ONLY IF it resolves to a REAL commit object in this
     repo's own history AND `git merge-base --is-ancestor` proves that
     commit is an ancestor of `HEAD` -- the anti-self-attestation gate that
     keeps this from becoming "write a field, get a stamp". An absent,
     malformed, unresolvable, or non-ancestor ref is REJECTED (one of
     `DISPOSITION_REF_ABSENT`/`_MALFORMED`/`_UNRESOLVABLE`/`_NOT_ANCESTOR`
     -- see `_disposition_ref_evidence`'s own returned rejection map).
  2. For a plan that predates the `## Tasks` spine entirely (`## Dispatch
     Ledger` fallback, Defect fix 2026-08-06): the plan's own `## Dispatch
     Ledger` markdown table, whose `status` column carries a literal
     `committed <sha>` cell per row once that row lands. A row counts as
     delivered only when its cited sha resolves and is a `HEAD` ancestor,
     the identical two-stage check as path 1 (`_dispatch_ledger_delivered`).

A row with neither -- still `open`/`coded` with no verified
`disposition_ref`, on a plan with a LOCATED spine -- reads as missing.
There is no longer an automatic "the tree already has this, promote it for
me" inference; `resolve --coded <sha>` (`plan_tasks_mutate`'s PM-gated
verb) is the one way to attach evidence to a row.

Absent-spine posture: a plan with neither a `## Tasks` spine nor a
`## Dispatch Ledger` heading has no per-chunk oracle to check completeness
against at all -- `shipped` stays `True` (nothing to check), but the
`evidence_backed` flag `_determine_shipped` returns is `False`, so a
stamping caller does not treat that as attributed evidence of delivery
(see `close_out_and_stamp()`'s own stamp-decision gate). A MALFORMED spine
(>1 fenced block, or a fence not directly under the heading) is NOT
guessed past -- it fails loud, since chunk-completeness cannot be safely
determined against a spine that cannot be located.

Composition, not duplication (Wave-2 substrate-gap remit): this module does
NOT re-derive the `status:` transition logic or hand-roll a second
frontmatter writer -- it calls `coordinator_core.archive_stamp.
cs_stamp_plan_implemented` (itself a thin wrapper over the already-native
`coordinator_core.ops.plan_status_transition` port) for the stamp. The
commit leg calls `coordinator_core.git.commit.commit_paths` directly,
in-process, with an explicit pathspec (C3, docs/plans/2026-08-29-the-push-
subsystem-leaves-and-then-the-pipeline-can-go.md -- repointed off the killed
`commit_pipeline.run_commit_pipeline`), chosen over the former `coordinator/
bin/coordinator-safe-commit` shell-out (Defect 3, 2026-07-27) because that
binary's default mode refuses outright under ordinary multi-session
concurrency ("multiple live sessions detected; default-mode commit is
unsafe") and this caller never passed it a scope to avoid that refusal --
see `close_out_and_stamp()`'s own docstring for the explicit-path derivation
this fix introduces. Neither the stamp nor the commit path itself is
reimplemented here.

Negative-spec -- no plan-body-hash write here, deliberately: this op writes
ONLY the plan's `status:` field (via `plan_status_transition`), never an
`execution_authorized_sha`-shaped field. That field belongs to a DIFFERENT
stamp -- the review-time execution-authorization stamp written by
`coordinator_core.review_assemble.exec_auth_stamp` at the `/review` Exit
gate, whose hash recipe (the plan BODY hashed via the shared
`coordinator_core.frontmatter.primitives.canonical_body_sha` blob-hash
recipe, byte-identical to `git hash-object --stdin` over the body) is a
distinct, already-shipped concern this module does not touch, mutate, or
re-derive. This is the deliberate fix for the campaign's live bookkeeping
defect (three plans stamped a commit sha where a plan-body blob hash
belonged) -- the fix here is to not manufacture a second hash-shaped field
at all, not to re-derive the existing one with a different recipe.

Spec backlink: DoE-claude coordinator/skills/execute-plan/SKILL.md § Phase 4,
docs/plans/2026-07-27-plan-line-item-resolution-model.md § C7 (AC7/AC8/AC9),
docs/plans/2026-08-03-klabauter-rows-relocate-into-claude-klabauter.md § C5/C6
(disposition_ref evidence), docs/plans/2026-08-20-the-close-ceremony-stops-
paying-for-the-join.md § C3 (deletes the commit-subject/Deliverable-Id join,
the cross-repo sibling scan, and the AC8 auto-resolve consumer described
above prior to 2026-08-21)
"""

from __future__ import annotations

import dataclasses
import datetime
import difflib
import os
import re
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

import coordinator_core.archive_stamp as archive_stamp
from coordinator_core.execute_plan_assemble.row_spans import (  # noqa: F401 -- re-exported
    _CODED,
    _COMMIT_REQUIRED_DISPOSITIONS,
    _OPEN,
    _ROW_KEY_LINE_RE,
    _all_spine_ids,
    _commit_required_chunk_ids,
    _find_row_spans,
    _find_row_spans_in_plan,
    _line_ending,
    _measure_row_content_indent,
    _parse_spine_rows,
    _plan_deliverable_id,
    _row_disposition,
    _row_key_line_indices,
    _row_span_containing,
    _run_git,
    _stamp_rows_in_body,
    _unquote_row_id,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import (
    canonical_body_sha,
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.locked_write import LOCK_TIMEOUT_SECS, LockTimeout, MutateAbort, locked_rmw
from coordinator_core.machine_resolver import registry_get
from coordinator_core.git.commit import CommitRefused, FilterUnsupported, commit_paths
from coordinator_core.git.commit import hash_worktree_blobs_via_spawn
from coordinator_core.ops.ceremony import git_native, post_commit_tail
from coordinator_core.ops.ceremony.commit_message import compose_message
from coordinator_core.ops.ceremony.push import PUSH_STATUS_NOT_ATTEMPTED
from coordinator_core.ops.extract_scope_paths import _extract_scope_paths
from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.ops.handoff_close_origin_stub import _handler as _close_origin_stub_handler
from coordinator_core.ops.plan_status_transition import (
    _FLIPPABLE_STATUSES,
    _FROZEN_STATUSES,
    _strip_unquoted_trailing_comment,
)
from coordinator_core.session import core as session_core
from coordinator_core.wire_paths import rel_id

EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2

#: Corpus-mutator declaration (generator-provenance sweep): this module
#: stamps whichever plan doc it is given (plan_path/live_path, and its
#: tracker_path sibling) — the target file is caller-supplied and
#: data-dependent (any plan being closed out), not a fixed artifact.
MUTATES = ["docs/plans/*.md"]





_LANDED_STATUS = "landed"






def _open_blocking_chunk_ids(spine_rows: list[Any]) -> list[str]:
    """Chunk-ids whose row is still `open` and therefore blocks an
    `implemented` stamp (AC7) -- everything else (`coded`/`spun_off`/
    `backlogged`/`wont_do`, and legacy `deferred: true` rows, which D8
    treats as backlogged-equivalent) is resolved and does not block."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) == _OPEN:
            chunk_id = row.get("id")
            if chunk_id:
                ids.append(str(chunk_id))
    return ids





def _plan_execution_authorized_sha(plan_text: str) -> Optional[str]:
    """Reads the plan's own `execution_authorized_sha:` frontmatter field,
    unquoted -- the four-field execution-authorization stamp's content-
    binding witness (`review_assemble.exec_auth_stamp`). `None` when the
    plan has no parseable frontmatter or no such field.

    NEGATIVE SPEC: this value is `canonical_body_sha` -- `git_blob_sha1` of
    the plan BODY text ALONE (frontmatter excluded), a synthetic content
    hash never written into this (or any) repo's git object store, since no
    real git blob ever holds body-without-frontmatter content. `git
    cat-file -e <this-sha>` therefore fails on essentially every plan.

    C3 (2026-08-21): this function's own former consumer -- the widened
    commit-search range the deleted commit-subject/`Deliverable-Id` join
    used -- is gone; nothing in this module calls it any more. Left defined
    (a plain frontmatter reader, not part of the deleted join machinery)
    for a future caller that needs the raw field."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "execution_authorized_sha")



#: The pre-spine legacy delivery record, still live on ~23 real plans on this
#: branch (2026-08-06 census -- see this module's docstring §
#: `Dispatch Ledger` fallback for the exact count and how it was taken):
#: a hand-authored `## Dispatch Ledger` markdown table whose `status` column
#: carries a literal `committed <sha>` cell per row once that row's work
#: lands, predating the `## Tasks` machine-parseable spine convention
#: entirely. `_parse_spine_rows`'s own ABSENT branch (D7) reads a missing
#: spine as "no per-chunk oracle to check completeness against" and treats
#: it as full-shipped UNCONDITIONALLY -- correct for a plan genuinely too
#: early/small to ever carry a spine, but WRONG for one of these 23: it
#: means `close_out_and_stamp` would stamp `implemented` on a plan whose own
#: Dispatch Ledger might show unfinished, parked, or never-dispatched rows,
#: without ever reading that ledger at all (verified live, this fix:
#: `close_out_and_stamp('docs/plans/2026-07-02-ccos-6-rehome-attribution-
#: python.md', dry_run=True)` returned `shipped: true` under the OLD
#: unconditional-bypass behavior purely because the spine was absent -- the
#: Dispatch Ledger's own 7 `committed <sha>` rows were never consulted).
#:
#: `_dispatch_ledger_delivered` below closes that hole: an ABSENT spine no
#: longer bypasses the oracle -- it reroutes to a SECOND, narrower oracle
#: that reads the plan's own Dispatch Ledger table instead. Reuses
#: `locate_fenced_block` (the SAME locate seam `_parse_spine_rows` already
#: calls) to detect the ABSENT case in the first place -- this is a
#: FALLBACK path only, reached exclusively when a `## Tasks` spine could not
#: be located at all; a plan with a real (even if MALFORMED) spine never
#: reaches this code, spine-present always wins.
#:
#: Conservative by construction, same failure direction as everywhere else
#: in this module (false-negative over false-positive): a plan with no
#: `## Dispatch Ledger` heading at all, a heading with no parseable table,
#: a table missing a recognizable `chunk-id`/`status` column pair, or ANY
#: row whose `status` cell is not exactly `committed <sha>` (a bare `sha`
#: that does not resolve via `git cat-file -e` in THIS repo counts as NOT
#: committed, same as a missing cell) is reported NOT-SHIPPED. There is no
#: "mostly parseable, assume the rest" path -- an ambiguous ledger returns
#: not-shipped, never a guess.
#: Widened (Defect fix, false-positive-stamp incident) to tolerate a
#: trailing suffix after the heading text itself -- a real corpus heading
#: (`## Dispatch Ledger — claude-klabauter [M] slice`) never matched the old
#: exact-line anchor, so that plan's ABSENT-spine, present-ledger case fell
#: all the way through to `_determine_shipped`'s no-evidence branch instead
#: of being read by `_dispatch_ledger_delivered`. Matches only the heading
#: TEXT, deliberately not `_parse_dispatch_ledger_table`'s own row/column
#: matching -- widening those is explicitly out of scope for this fix.
#: Review: coordinator:code-reviewer -- requires a separator before the
#: suffix so `## Dispatch LedgerFooBar` (no space/dash) doesn't also match;
#: `.*` alone was looser than the stated "tolerate a trailing suffix" intent.
_DISPATCH_LEDGER_HEADING_RE = re.compile(r"^## Dispatch Ledger(\s.*)?$", re.MULTILINE)
_DISPATCH_LEDGER_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_DISPATCH_LEDGER_COMMITTED_RE = re.compile(r"^committed\s+([0-9a-fA-F]{6,40})\b")


def _dispatch_ledger_section(plan_text: str) -> Optional[str]:
    """Slices the plan text from a `## Dispatch Ledger` heading (if any) up
    to (excluding) the next `## ` heading, or end-of-document -- `None` when
    no such heading exists at all. Pure text slicing, no table parsing."""
    heading_match = _DISPATCH_LEDGER_HEADING_RE.search(plan_text)
    if heading_match is None:
        return None
    section_start = heading_match.end()
    next_match = _DISPATCH_LEDGER_NEXT_HEADING_RE.search(plan_text, section_start)
    section_end = next_match.start() if next_match is not None else len(plan_text)
    return plan_text[section_start:section_end]


def _parse_dispatch_ledger_table(
    section_text: str,
) -> tuple[Optional[list[dict[str, str]]], Optional[str]]:
    """Parses the FIRST markdown pipe-table found in `section_text` into
    `[{"chunk_id": str, "status_cell": str}, ...]`, keyed off the table's
    OWN header row (`chunk-id`/`status` columns, matched case-insensitively)
    rather than a fixed column index -- the real corpus's Dispatch Ledger
    tables do not all order columns identically. Returns `(None, reason)`
    on anything this parser cannot confidently read: no table found under
    the heading, a header missing either required column, a second
    (separator) row that is not a `-`/`:`-only markdown separator shape, or
    a data row with fewer cells than the header promises. Never guesses a
    partial parse into a verdict -- see this module's own conservatism note
    above."""
    lines = [line.strip() for line in section_text.splitlines()]
    table_lines: list[str] = []
    started = False
    for line in lines:
        if line.startswith("|") and line.endswith("|") and len(line) >= 2:
            table_lines.append(line)
            started = True
        elif started:
            break
    if len(table_lines) < 3:
        return None, "no Dispatch Ledger table (header + separator + >=1 row) found"

    header_cells = [c.strip().lower() for c in table_lines[0].strip("|").split("|")]
    if "chunk-id" not in header_cells or "status" not in header_cells:
        return None, "Dispatch Ledger table header has no chunk-id/status columns"
    chunk_idx = header_cells.index("chunk-id")
    status_idx = header_cells.index("status")

    # Review: coordinator:code-reviewer -- `table_lines[1]` is assumed to be
    # the markdown header/data separator row purely by position. Validate
    # its shape before skipping it; a real data row landing there (a
    # separator-less or differently-shaped table) must fail loud rather
    # than be silently dropped, per this parser's own "never guesses a
    # partial parse into a verdict" posture.
    separator_cells = [c.strip() for c in table_lines[1].strip("|").split("|")]
    if not separator_cells or not all(
        cell and set(cell) <= {"-", ":"} for cell in separator_cells
    ):
        return None, f"Dispatch Ledger table has no header/data separator row: {table_lines[1]!r}"

    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) <= max(chunk_idx, status_idx):
            return None, f"malformed Dispatch Ledger row (too few columns): {line!r}"
        chunk_id = cells[chunk_idx]
        if not chunk_id:
            return None, f"Dispatch Ledger row has an empty chunk-id: {line!r}"
        rows.append({"chunk_id": chunk_id, "status_cell": cells[status_idx]})
    if not rows:
        return None, "Dispatch Ledger table has no data rows"
    return rows, None


def _batch_git_cat_file_check(
    shas: Sequence[str], repo_root: Path
) -> dict[str, Optional[str]]:
    """Resolves every sha in `shas` to a full commit object id in ONE
    `git cat-file --batch-check` spawn (cheapen-in-place, 2026-08-15 --
    see `state/audits/2026-08-15-fleet-composed-op-spawn-census.md` row
    18), replacing what `_dispatch_ledger_delivered` used to spend one
    `git cat-file -e <sha>` spawn per Dispatch Ledger row on. Feeds every
    sha on stdin, one per line -- `--batch-check` guarantees one output
    line per input line, IN ORDER, even for a missing/ambiguous object
    (`<sha> missing`/`<sha> ambiguous`), so positional zip is safe.

    Returns `{sha: full_oid_or_None}`. `None` covers every case the
    original per-sha `git cat-file -e` treated as "does not exist":
    missing, ambiguous, or malformed output. Narrower than the original
    call's own semantics in one respect -- this only accepts an object
    whose type is `commit` -- but that narrowing is a no-op on the actual
    verdict: the caller's very next step is `merge-base --is-ancestor`,
    which itself requires a commit-ish and would reject a non-commit
    object anyway, so a sha that resolved-but-wasn't-a-commit was already
    guaranteed to end up `missing` under the pre-batch code path too.
    Never raises -- an empty `shas` short-circuits with no spawn at all,
    matching this module's existing zero-work-zero-spawn posture."""
    result: dict[str, Optional[str]] = {sha: None for sha in shas}
    if not shas:
        return result
    from coordinator_core.git.run import run_git

    proc = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=str(repo_root),
        input=("\n".join(shas) + "\n").encode("utf-8"),
    )
    if proc.timed_out:
        return result
    out_lines = (proc.stdout or "").splitlines()
    for sha, line in zip(shas, out_lines):
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit":
            result[sha] = parts[0]
    return result


def _rev_list_ancestor_shas(repo_root: Path) -> Optional[set[str]]:
    """Every full commit sha reachable from `HEAD`, in ONE `git rev-list
    HEAD` spawn -- the batched replacement for a per-sha `git merge-base
    --is-ancestor <sha> HEAD` call (cheapen-in-place, 2026-08-15 -- see
    this module's spawn census row 18). "sha is an ancestor of HEAD" and
    "sha is a member of `git rev-list HEAD`'s own output" are the same
    reachability question asked two ways -- `rev-list HEAD` walks
    exactly the commit graph `--is-ancestor` would have walked per call.

    Returns `None` on a `rev-list` failure (non-zero exit) -- the
    caller degrades that to the same not-shipped, no-crash posture every
    other git-query failure in this module already takes, distinct from
    an empty-but-successful set (a repo with a one-commit `HEAD`, whose
    ancestor set is genuinely just that commit)."""
    result = _run_git(["rev-list", "HEAD"], repo_root)
    if result.returncode != 0:
        return None
    return set((result.stdout or "").splitlines())


def _dispatch_ledger_delivered(
    plan_text: str, repo_root: Path
) -> tuple[bool, list[str], Optional[str]]:
    """The legacy-format oracle (see this module's own § Dispatch Ledger
    fallback comment block above `_dispatch_ledger_heading_re`): returns
    `(is_shipped, missing_chunk_ids, error)`. `error` set (other fields
    meaningless) means the ledger could not be read at all -- no heading, no
    table, or an unrecognizable header -- which callers treat as NOT-SHIPPED
    (conservative), distinct from a genuine per-row gap only for diagnostic
    messaging.

    A row counts as delivered ONLY when its `status` cell matches
    `committed <sha>` (optionally followed by trailing prose, e.g.
    `committed ed6c513d7 (EM-inline)` -- a real corpus shape) AND that `sha`
    resolves to a real object in `repo_root`'s history via
    `git cat-file -e` AND `git merge-base --is-ancestor <sha> HEAD` proves
    that object is reachable from `HEAD` -- a ledger citing a SHA that does
    not exist in this repo (a typo, a sha from a different repo/clone, a
    fabricated value) OR that exists but was never landed on this branch
    (a dangling, rebased-away, or fetched-but-unmerged commit) does NOT
    count, exactly the same anti-self-attestation posture
    `_verify_disposition_ref` already applies to the spine-based oracle's
    own second evidence path. Any other status text (`ready — not yet
    dispatched`, `identified — not yet dispatched`, `PARKED`, blank, ...)
    is NOT delivered -- this function does not attempt to special-case or
    exclude those rows the way the spine oracle excludes
    `spun_off`/`backlogged`/`wont_do` rows via `disposition`; the Dispatch
    Ledger format has no equivalent field, so every ledger row is
    commit-required by construction."""
    section = _dispatch_ledger_section(plan_text)
    if section is None:
        return False, [], "no '## Dispatch Ledger' heading found in plan"
    rows, error = _parse_dispatch_ledger_table(section)
    if error is not None:
        return False, [], error

    # Batched (cheapen-in-place, 2026-08-15 -- see this module's spawn
    # census row 18): the old code ran TWO `git` spawns per row
    # (`cat-file -e`, then `merge-base --is-ancestor`) -- a 20-chunk plan
    # was 40 spawns. Every row's cited sha is first collected, then
    # resolved/reachability-checked in exactly TWO spawns TOTAL for the
    # whole table (`_batch_git_cat_file_check`, `_rev_list_ancestor_shas`),
    # regardless of row count -- same two-stage verdict per row
    # (existence, THEN reachability from `HEAD`, mirroring
    # `_verify_disposition_ref`), just computed from two shared batch
    # results instead of a live spawn per row.
    row_shas: dict[int, str] = {}
    for idx, row in enumerate(rows):
        match = _DISPATCH_LEDGER_COMMITTED_RE.match(row["status_cell"])
        if match is not None:
            row_shas[idx] = match.group(1)

    distinct_shas = sorted(set(row_shas.values()))
    resolved = _batch_git_cat_file_check(distinct_shas, repo_root)
    ancestor_shas = _rev_list_ancestor_shas(repo_root) if distinct_shas else set()

    missing: list[str] = []
    for idx, row in enumerate(rows):
        sha = row_shas.get(idx)
        if sha is None:
            missing.append(row["chunk_id"])
            continue
        full_oid = resolved.get(sha)
        if full_oid is None:
            missing.append(row["chunk_id"])
            continue
        if ancestor_shas is None or full_oid not in ancestor_shas:
            missing.append(row["chunk_id"])
    return (len(missing) == 0), missing, None





def _determine_shipped(
    plan_text: str, plan_path_rel: str, repo_root: Path
) -> tuple[bool, list[str], bool, Optional[str]]:
    """Returns `(is_shipped, missing_chunk_ids, evidence_backed, error)`.
    `error` is set (and every other field is meaningless) when the spine is
    MALFORMED or the plan's `## Dispatch Ledger` table could not be
    confidently read. Every other outcome is a definite shipped/halted
    verdict.

    C3 (2026-08-21, "the close ceremony stops paying for the join"): this
    oracle no longer joins a commit's subject/`Deliverable-Id` trailer
    against the spine at all -- that whole precision machinery (subject
    parsing, trailer round-trip, sibling-repo commit scanning, near-miss/
    hyphen-range divergence diagnostics) is DELETED, not narrowed. The two
    evidence paths that remain are both pure sha-ancestry checks with no
    subject parsing: a `disposition: coded` row's own `disposition_ref`
    (`_disposition_ref_evidence`/`_verify_disposition_ref`), and, for a plan
    that predates the `## Tasks` spine entirely, its `## Dispatch Ledger`
    table's `committed <sha>` cells (`_dispatch_ledger_delivered`). Neither
    path infers completion from a commit MESSAGE -- both require an
    explicit sha that `git merge-base --is-ancestor` proves landed on
    `HEAD`.

    `evidence_backed` is `False` only on the genuinely pre-spine,
    pre-ledger case (a plan with neither a `## Tasks` spine nor a
    `## Dispatch Ledger` heading -- nothing at all exists to check
    completeness against). `shipped` stays `True` there (unchanged
    "nothing to check" posture), but a stamping caller must not read that
    as evidence-backed delivery -- see the stamp-decision gate in
    `close_out_and_stamp` itself. `True` in every other case, including a
    LOCATED spine with zero commit-required rows (there was something to
    consult; it simply had nothing outstanding)."""
    # Dispatch Ledger fallback: an ABSENT `## Tasks` spine no longer
    # bypasses this oracle as an automatic full-shipped verdict -- it
    # reroutes to the plan's own legacy Dispatch Ledger table instead,
    # checked FIRST (before ever calling `_parse_spine_rows`) so a LOCATED
    # (even if empty-bodied) spine always wins and never reaches this
    # fallback at all.
    if locate_fenced_block(plan_text).status == LocateStatus.ABSENT:
        if _dispatch_ledger_section(plan_text) is None:
            # No spine AND no `## Dispatch Ledger` heading at all -- the
            # genuinely pre-spine, pre-ledger case (nothing at all to check
            # completeness against). `shipped=True` preserved verbatim, but
            # `evidence_backed=False` tells a stamping caller that verdict
            # was never backed by a lookup of any kind.
            return True, [], False, None
        ledger_shipped, ledger_missing, ledger_error = _dispatch_ledger_delivered(
            plan_text, repo_root
        )
        if ledger_error is not None:
            # A heading exists, but the table under it could not be
            # confidently read (missing/unrecognized columns, malformed
            # rows) -- mirrors the spine's own MALFORMED posture: fail loud
            # rather than guess, since a guess here could silently stamp
            # undelivered work terminal.
            return False, [], True, f"{plan_path_rel}: {ledger_error}"
        return ledger_shipped, ledger_missing, True, None

    rows, error = _parse_spine_rows(plan_text, plan_path_rel)
    if error is not None:
        return False, [], True, error

    chunk_ids = _commit_required_chunk_ids(rows)
    if not chunk_ids:
        return True, [], True, None

    # Plan-side disposition_ref evidence (see `_disposition_ref_evidence`'s
    # own docstring): the sole remaining evidence path for a LOCATED spine
    # -- a `disposition: coded` row's own `disposition_ref` verified as a
    # real, ancestor commit. `verified_ids` names the exact row it is
    # evidence for (no sub-chunk-suffix coverage matching needed -- that
    # matching was part of the deleted commit-subject join).
    verified_ids, _rejections = _disposition_ref_evidence(rows, repo_root)
    missing = [cid for cid in chunk_ids if cid not in verified_ids]
    return (len(missing) == 0), missing, True, None


# ---------------------------------------------------------------------------
# docs/project-tracker.md `N of M` reconciliation (AC7)
#
# Tracker rows are hand-authored prose carrying PM-ratified boundary and gate
# narrative wrapped around a bare `N of M` chunk-progress claim -- the sending
# memo's explicit hard constraint (see this module's own C8 backlink) is that
# a fix here may edit ONLY that digit claim; the surrounding narrative must
# come out byte-identical. This is deliberately NOT a full-row rewrite (a
# render() in render_project_tracker.py's sense) -- that machinery folds a
# queue-backed store's OWN authored fields, never PM-ratified prose a human
# wrote around a number. A bounded edit or a no-op (when no plan can be
# joined, or the claim already agrees) is the correct, safe answer; guessing
# a full section back together from parts is not attempted here.
#
# Spec backlink: pln-terminal-state-propagation-giv-c85539
# § C8 / AC7.
# ---------------------------------------------------------------------------

_TRACKER_WORKSTREAM_HEADER_RE = re.compile(r"^### \d+\. .*$", re.MULTILINE)
"""Matches the `### {number}. {title}` header render_project_tracker.py's
own `_render_workstream_section` emits for a queue-backed tracker, and which
this repo's hand-curated `docs/project-tracker.md` also uses -- the section
boundary this reconciler splits on."""

_TRACKER_SPECS_LINE_RE = re.compile(r"^\*\*Specs:\*\*\s*(.+)$", re.MULTILINE)
"""One workstream section's `**Specs:**` line, per the tracker format
contract (coordinator/pipelines/update-docs/tracker-maintenance.md § Project
Tracker Format Reference) -- the join anchor to a `docs/plans/*.md` spec."""

_TRACKER_SPEC_PLAN_PATH_RE = re.compile(r"`(docs/plans/[^`]+\.md)`")
"""Backtick-quoted `docs/plans/*.md` paths inside a `**Specs:**` line --
join CANDIDATES only; join on `deliverable_id`, never on the path/`plan:`
field itself (see this module's C8 backlink and the plan's own R1/R1a/R2
rulings on why `plan:` is not a join key)."""

_TRACKER_N_OF_M_RE = re.compile(r"\b(\d+) of (\d+) chunks?\b")
"""The bounded edit target itself -- a literal `N of M chunk(s)` digit
claim. Deliberately narrow (no word-number form like "all four chunks",
no bare `N/M`) so this reconciler can never mistake an unrelated number
pair for the claim it is licensed to touch."""


def _tracker_section_spec_plan_paths(section_text: str) -> list[str]:
    """Every `docs/plans/*.md` path a workstream section's own `**Specs:**`
    line names, in order -- `[]` when the section carries no `**Specs:**`
    line, or none of its entries are a backtick-quoted plan path."""
    match = _TRACKER_SPECS_LINE_RE.search(section_text)
    if not match:
        return []
    return _TRACKER_SPEC_PLAN_PATH_RE.findall(match.group(1))


def _tracker_row_shipped_of_total(
    plan_path_rel: str, repo_root: Path
) -> Optional[tuple[int, int]]:
    """Reduces `_determine_shipped`'s own commit-evidence verdict for ONE
    Specs:-referenced plan to a bare `(shipped, total)` pair for a tracker
    row's `N of M` claim -- reusing `_parse_spine_rows` /
    `_commit_required_chunk_ids` / `_determine_shipped` verbatim (never a
    second implementation) and the same forward-slash `rel_id` normalisation
    `close_out_and_stamp`'s own entrypoint uses (Windows is first-class
    here -- see that call site's own A5-fix comment).

    Returns `None` -- "leave the tracker claim untouched" -- when the plan
    is unreadable, carries no parseable frontmatter, its `## Tasks` spine
    cannot be located/parsed, the git-log query itself failed, or the spine
    names zero commit-required chunks (a 0-of-0 plan has no `N of M` claim
    to reconcile against). A tracker claim is never overwritten with a
    guess in any of those cases -- silence, not a wrong number, is the safe
    failure direction."""
    live_path = Path(plan_path_rel)
    if not live_path.is_absolute():
        live_path = repo_root / live_path
    if not live_path.is_file():
        return None
    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if split_frontmatter(text) is None:
        return None

    try:
        norm_rel = rel_id(live_path, repo_root)
    except ValueError:
        norm_rel = plan_path_rel

    rows, rows_error = _parse_spine_rows(text, norm_rel)
    if rows_error is not None or rows is None:
        return None
    total = len(_commit_required_chunk_ids(rows))
    if total == 0:
        return None

    _shipped, missing, _evidence_backed, spine_error = _determine_shipped(
        text, norm_rel, repo_root
    )
    if spine_error is not None:
        return None
    return total - len(missing), total


def _reconcile_tracker_section(
    section_text: str, repo_root: Path
) -> tuple[str, Optional[dict[str, Any]]]:
    """Reconciles ONE workstream section's `N of M` claim (if any) against
    commit evidence, returning `(possibly-rewritten section_text, edit-or-
    None)`. The rewrite -- when one fires -- replaces ONLY the matched
    claim's own digit run; every other character of `section_text`,
    including everything before and after that span, is returned
    unmodified (HARD CONSTRAINT -- see this module's own test for the
    byte-identical assertion this exists to satisfy)."""
    header_match = re.match(r"^### \d+\. (.*)$", section_text, re.MULTILINE)
    title = header_match.group(1).strip() if header_match else "?"

    plan_paths = _tracker_section_spec_plan_paths(section_text)
    if not plan_paths:
        return section_text, None

    claim_match = _TRACKER_N_OF_M_RE.search(section_text)
    if claim_match is None:
        return section_text, None

    derived: Optional[tuple[int, int]] = None
    derived_plan_path: Optional[str] = None
    for plan_path in plan_paths:
        result = _tracker_row_shipped_of_total(plan_path, repo_root)
        if result is not None:
            derived = result
            derived_plan_path = plan_path
            break
    if derived is None:
        return section_text, None

    shipped, total = derived
    claimed_shipped = int(claim_match.group(1))
    claimed_total = int(claim_match.group(2))
    if (shipped, total) == (claimed_shipped, claimed_total):
        return section_text, None

    old_digits = claim_match.group(0)
    new_digits = f"{shipped} of {total}" + old_digits[len(f"{claimed_shipped} of {claimed_total}"):]
    new_section_text = (
        section_text[: claim_match.start()] + new_digits + section_text[claim_match.end() :]
    )
    edit = {
        "section": title,
        "plan_path": derived_plan_path,
        "old": f"{claimed_shipped} of {claimed_total}",
        "new": f"{shipped} of {total}",
    }
    return new_section_text, edit


def reconcile_tracker_shipped_counts(
    tracker_text: str, repo_root: Path
) -> tuple[str, list[dict[str, Any]]]:
    """Bounded-edit reconciliation pass over `docs/project-tracker.md`'s own
    text (AC7) -- pure, no I/O of its own (the caller reads/writes the
    file; see `apply_tracker_reconciliation` for the single write
    entrypoint). Splits `tracker_text` on `_TRACKER_WORKSTREAM_HEADER_RE`
    and reconciles each workstream section independently via
    `_reconcile_tracker_section`.

    Returns `(new_text, edits)`. `edits` names every section this pass
    actually rewrote; `new_text == tracker_text` (byte-identical) whenever
    `edits == []` -- content the reconciler could not join, or already
    agrees with commit evidence, passes through completely untouched, not
    merely "unchanged in effect"."""
    headers = list(_TRACKER_WORKSTREAM_HEADER_RE.finditer(tracker_text))
    if not headers:
        return tracker_text, []

    edits: list[dict[str, Any]] = []
    pieces = [tracker_text[: headers[0].start()]]
    for index, header in enumerate(headers):
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(tracker_text)
        section_text = tracker_text[header.start() : section_end]
        new_section_text, edit = _reconcile_tracker_section(section_text, repo_root)
        if edit is not None:
            edits.append(edit)
        pieces.append(new_section_text)
    return "".join(pieces), edits


def apply_tracker_reconciliation(
    tracker_path: Path, repo_root: Path
) -> list[dict[str, Any]]:
    """The single write entrypoint for AC7's tracker reconciliation (this
    repo's own north star: read-only compute + ONE apply entrypoint, never
    a second ad hoc writer -- `reconcile_tracker_shipped_counts` above is
    that read-only compute half). Reads `tracker_path`, reconciles it, and
    writes back ONLY when at least one edit fired -- a no-op run never
    dirties the tree or perturbs the file's mtime. Returns the same `edits`
    list `reconcile_tracker_shipped_counts` returns (`[]` on a no-op run),
    for a caller (`ceremony.update_docs_scan`'s manifest today; the mise
    tracker-sync step once it shares this same compute path) to report."""
    text = tracker_path.read_text(encoding="utf-8")
    new_text, edits = reconcile_tracker_shipped_counts(text, repo_root)
    if edits:
        tracker_path.write_text(new_text, encoding="utf-8", newline="\n")
    return edits


#: Rejection reason strings `_verify_disposition_ref` returns -- named here as
#: a single source of truth since `_disposition_ref_evidence` and any caller
#: reporting them (`close_out_and_stamp`'s own result dict) must use the
#: identical four values (see this module's docstring § Plan-side
#: disposition_ref evidence).
DISPOSITION_REF_ABSENT = "absent"
DISPOSITION_REF_MALFORMED = "malformed"
DISPOSITION_REF_UNRESOLVABLE = "unresolvable"
DISPOSITION_REF_NOT_ANCESTOR = "non-ancestor"

#: A `disposition_ref` is always written by this module (or a human
#: following the same convention) as a bare hex commit sha -- never a
#: symbolic ref, branch name, or tag. Bounding the shape BEFORE ever handing
#: the value to `git rev-parse` is deliberate defense-in-depth: it means an
#: arbitrary string (blank, whitespace, a `-`-leading token that could be
#: mistaken for a flag, a symbolic ref like `HEAD~3` that resolves to
#: something OTHER than what the author actually pinned) is rejected as
#: `DISPOSITION_REF_MALFORMED` before any subprocess call, rather than
#: silently resolving to an unintended commit.
_DISPOSITION_REF_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _verify_disposition_ref(
    repo_root: Path, ref: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Verifies a single row's `disposition_ref` value as commit-required
    evidence (see this module's docstring § Plan-side disposition_ref
    evidence for the design this implements and why it is safe against
    self-attestation).

    Returns `(sha, reason)`: `sha` is the ref's own full, resolved commit sha
    when -- and ONLY when -- it names a real commit object in `repo_root`'s
    history that `git merge-base --is-ancestor` proves is reachable from
    `HEAD`. Otherwise `sha` is `None` and `reason` is exactly one of
    `DISPOSITION_REF_ABSENT` (not a non-blank string at all -- the row has no
    `disposition_ref`, or it is blank/whitespace-only), `DISPOSITION_REF_
    MALFORMED` (present, but not a bare hex sha shape -- see `_DISPOSITION_
    REF_SHA_RE`'s own docstring for why this is checked before ever reaching
    git), `DISPOSITION_REF_UNRESOLVABLE` (hex-shaped, but `git rev-parse
    --verify` cannot resolve it to a commit object in this repo -- a typo, a
    sha from a repo this isn't, or an object this shallow/partial clone does
    not have), or `DISPOSITION_REF_NOT_ANCESTOR` (resolves to a real commit,
    but `HEAD` never reached it -- a rebased-away, cherry-picked-into-a-
    different-branch, or fabricated sha). Never raises."""
    if not isinstance(ref, str) or not ref.strip():
        return None, DISPOSITION_REF_ABSENT
    ref = ref.strip()
    if not _DISPOSITION_REF_SHA_RE.match(ref):
        return None, DISPOSITION_REF_MALFORMED

    resolve_result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo_root)
    sha = (resolve_result.stdout or "").strip()
    if resolve_result.returncode != 0 or not sha:
        return None, DISPOSITION_REF_UNRESOLVABLE

    ancestor_result = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], repo_root)
    if ancestor_result.returncode != 0:
        return None, DISPOSITION_REF_NOT_ANCESTOR

    return sha, None


def _disposition_ref_evidence(
    spine_rows: list[Any], repo_root: Path
) -> tuple[set[str], dict[str, str]]:
    """Chunk-ids for which a `disposition: coded` row's own `disposition_ref`
    verifies as real evidence (`_verify_disposition_ref`), PLUS a rejection-
    reason map for every `coded` row whose `disposition_ref` did NOT verify
    -- see this module's docstring § Plan-side disposition_ref evidence.

    Scoped to `disposition: coded` rows ONLY -- narrower than `_commit_
    required_chunk_ids`'s own `open`/`coded` set, and deliberately so: an
    `open` row has not yet been resolved by anything (its `disposition_ref`,
    if present at all, is not this evidence path's concern -- it either
    ships via the ordinary commit-subject join, or AC8's own auto-resolve
    step picks it up later), so treating a plain legacy `open` row's absent
    `disposition_ref` as a "rejection" would manufacture rejection-reason
    noise on every ordinary legacy spine that has never used `disposition:`
    at all (every row defaults to `open` per D1). `coded` is the disposition
    a row is EXPLICITLY moved to once something -- an executor, a prior
    auto-resolve pass, or a manual `resolve --coded` -- attests it landed;
    that is the one state where a missing/failed `disposition_ref` is a
    genuine, reportable gap rather than "this plan predates the field
    entirely".

    Returns `(verified_ids, rejections)`. `verified_ids` is consumed
    directly by `_determine_shipped`'s own `missing` computation -- no
    sub-chunk-suffix coverage matching is needed here (that matching
    belonged to the deleted commit-subject join), since a `disposition_ref`
    is evidence for the exact row it lives on, never a prefix that might
    cover a sub-chunk or dash-tag variant. `rejections` maps every `coded`
    chunk-id whose ref did NOT
    verify to its own `_verify_disposition_ref` reason string -- callers
    report this ONLY for ids that remain in `missing_chunk_ids` after every
    evidence path has been unioned in, per this module's docstring's
    "unhappy-path-only" posture for its other diagnostics."""
    verified: set[str] = set()
    rejections: dict[str, str] = {}
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) != _CODED:
            continue
        chunk_id = row.get("id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        sha, reason = _verify_disposition_ref(repo_root, row.get("disposition_ref"))
        if sha is not None:
            verified.add(chunk_id)
        else:
            rejections[chunk_id] = reason
    return verified, rejections


# ---------------------------------------------------------------------------
# The goal-falsifier stamp-decision gate (C2, 2026-08-27, "the close ceremony
# refuses a goal nothing observed" -- docs/plans/2026-08-27-the-close-
# ceremony-refuses-a-goal-nothing-observed.md § C2, AC4-AC6/AC7/AC8/AC18-20):
# a THIRD refusal class alongside the two already live at this stamp-
# decision gate -- the halted path (a commit-required row without verified
# evidence skips the stamp) and hard `EXIT_BUSINESS_FAIL` (repo-identity
# MISMATCH, malformed spine). This one fires only on the branch that would
# otherwise become `status: implemented`: a plan whose own
# `prime_exit_criterion.falsifier` names an observation is not allowed to
# ship implemented on the strength of the spine oracle alone when that
# observation was never recorded, was recorded inert (`asserted: false`),
# has a baseline that cannot be trusted (`baseline_ref` fails the same
# ancestor check `_verify_disposition_ref` already applies to
# `disposition_ref`), or recorded a verdict other than `pass`.
#
# VERDICT, NOT DELTA (the defect this closes -- see the dispatch brief's own
# replay against a real plan): the gate reads `exit_criterion_met.
# falsifier_verdict` as an already-judged enum, never a raw-text comparison
# against `baseline_output`. `coordinator_core.goals.falsifier_compare`
# (C1) is gravestoned and has no caller here or anywhere else -- this gate
# performs NO digest, NO comparison, and NO execution of any observation:
# it only ever reads two already-recorded scalars off the plan's own
# frontmatter. See this plan's own anti-scope: "the engine never executes
# the falsifier."
#
# Grandfathering was presence-based when C2 landed (AC7) and is now
# presence-AND-date-based: a plan whose `prime_exit_criterion` is absent
# still exits at the gate's very first check -- `None`, and its caller adds
# no new result-dict key at all, byte-identical to the pre-C2 result dict
# for every plan on disk before this landed -- UNLESS it is both M+ and
# `created` on or after `GRANDFATHER_DATE`, which arm 0 refuses.
#
# Presence alone was not a grandfather rule, it was an opt-out: it exempted
# the pre-C2 corpus (correct) and equally exempted a plan authored today
# that simply declined to declare a criterion (not correct). The date is
# what separates those two populations, and it is pinned as a literal
# because "before this plan's landing commit" resolves differently in every
# repo that asks -- DoE-claude cross-repo memo, 2026-08-27, § "Pin the
# grandfather date". The M+ bound comes from plan.schema.json's own
# read-side size rule for the falsifier: an S/XS plan omitting a criterion
# was never in scope and still is not. A `prime_exit_criterion` present
# but carrying no `falsifier` (absent, null, non-object, or missing any of
# its four required keys -- `_falsifier_block`'s own TOTAL detection) is
# arm 1's "unchanged behaviour, full stop": the gate still adds its
# `goal_gate` key (since `prime_exit_criterion` itself was present), but
# never refuses.
# ---------------------------------------------------------------------------

_REQUIRED_FALSIFIER_KEYS = ("how", "baseline_output", "baseline_ref", "expected_when_true")


def _falsifier_block(prime_exit_criterion: Any) -> Optional[dict]:
    """Total, never-raising detection of a real `falsifier` sub-object (AC7):
    a non-dict `prime_exit_criterion`, an absent/non-dict `falsifier`, or a
    `falsifier` missing any of its four required non-blank string keys ALL
    return `None` here -- the caller's own arm 1 ("plan declares no
    falsifier -> unchanged behaviour, full stop") reads a `None` return
    identically regardless of WHICH of those shapes produced it, and this
    function never raises on any of them (a malformed/unparseable shape is
    exactly the case it exists to route safely, not to crash on)."""
    if not isinstance(prime_exit_criterion, dict):
        return None
    falsifier = prime_exit_criterion.get("falsifier")
    if not isinstance(falsifier, dict):
        return None
    for key in _REQUIRED_FALSIFIER_KEYS:
        value = falsifier.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    return falsifier


def _read_status_override(plan_text: str) -> Optional[dict[str, str]]:
    """The sanctioned escape for arms 2-5 (AC19): an existing
    `status_override_by`/`_reason`/`_at` attestation (the same trio
    `plan_status_transition._stamp_implemented` already writes for the
    frozen-status override), bound to the CURRENT plan-body hash
    (`canonical_body_sha`) by requiring that hash appear, verbatim hex,
    inside `status_override_reason` -- an override attested against an
    earlier body state does not silently keep suppressing the falsifier
    refusal after the body has since been re-authored underneath it. All
    three fields must be present and non-blank, and the plan's own body
    must hash at all (a plan whose frontmatter cannot be split has no body
    to bind against). Returns the three raw field values plus the bound
    hash on a valid, current attestation, or `None` on anything else --
    never raises."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    by = read_fm_field_unquoted(split.fm_text, "status_override_by")
    reason = read_fm_field_unquoted(split.fm_text, "status_override_reason")
    at = read_fm_field_unquoted(split.fm_text, "status_override_at")
    if not by or not by.strip():
        return None
    if not reason or not reason.strip():
        return None
    if not at or not at.strip():
        return None
    body_sha = canonical_body_sha(plan_text)
    if not body_sha or body_sha not in reason:
        return None
    return {"by": by.strip(), "reason": reason.strip(), "at": at.strip(), "body_sha": body_sha}


def _resolve_derived_from(derived_from: str, root: Path) -> Optional[str]:
    """Resolves `prime_exit_criterion.derived_from` against the plan's own
    repo (AC20) -- a link a reader can open and compare, never a
    self-declared provenance flag. The schema's own `pattern` already
    narrows the string to one of two shapes; this reads the SAME string
    without re-validating that pattern:

      - `state/sizings/<id>.yaml` -- must exist as a real file in `root`.
      - `<goal_id>#kr-<kr-id>` -- `goal_id` must name a goal whose own `id`
        field is found among `state/goals/*.yaml`, AND that goal's
        `key_results[]` must carry an entry whose `id` equals the FULL
        `kr-<kr-id>` token after the `#` -- an id, never an array index,
        the same anchor rule `kr-suggestion.schema.json` already uses.

    Returns `None` on a resolved link, otherwise a short message naming
    WHICH half failed. At most one filesystem read (the sizing-path shape)
    or one directory scan (the KR shape) -- zero git spawns, so AC14's
    at-most-2 budget (spent entirely by `baseline_ref`'s own ancestor
    check) is untouched. A malformed/unreadable `state/goals/*.yaml` file
    is skipped, never fatal -- mirrors this module's degrade-quietly
    posture everywhere else it reads a corpus of caller-authored YAML."""
    if "#" not in derived_from:
        sizing_path = root / derived_from
        if not sizing_path.is_file():
            return f"sizing object not found: {derived_from}"
        return None

    goal_id, _, kr_id = derived_from.partition("#")
    goals_dir = root / "state" / "goals"
    if not goals_dir.is_dir():
        return f"goal id not found (no state/goals/ directory): {goal_id}"
    for goal_path in sorted(goals_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(goal_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict) or doc.get("id") != goal_id:
            continue
        key_results = doc.get("key_results")
        if isinstance(key_results, list):
            for kr in key_results:
                if isinstance(kr, dict) and kr.get("id") == kr_id:
                    return None
        return f"key result id not found: {kr_id} in goal {goal_id}"
    return f"goal id not found: {goal_id}"


GOAL_REFUSAL_EXIT_CRITERION_ABSENT = "exit_criterion_met_absent"
GOAL_REFUSAL_NOT_ASSERTED = "exit_criterion_not_asserted"
GOAL_REFUSAL_BASELINE_REF_PREFIX = "baseline_ref_"
GOAL_REFUSAL_VERDICT_NOT_PASS = "falsifier_verdict_not_pass"
GOAL_REFUSAL_DERIVED_FROM_UNRESOLVABLE = "derived_from_unresolvable"
GOAL_REFUSAL_PRIME_ABSENT = "prime_exit_criterion_absent"
GOAL_REFUSAL_FALSIFIER_ABSENT = "falsifier_absent"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

GRANDFATHER_DATE = "2026-08-27"
"""The literal ISO date the prime-exit-criterion requirement starts binding.

A plan whose `created` is strictly BEFORE this date is grandfathered and may
omit `prime_exit_criterion` entirely; one created on or after it, and sized
M+, must carry the block or its close-out refuses `implemented`.

Why a pinned literal rather than "before this plan's landing commit": the
landing commit is unresolvable from the sending repo's side, and a rule that
different repos resolve differently is not one rule. Pinned by DoE-claude's
cross-repo memo `2026-08-27-doe-claude-em-prime-exit-criterion-settled-shape`
§ "Pin the grandfather date", which asked for exactly this literal.

NEGATIVE SPEC: this date never moves forward. Advancing it would silently
re-grandfather a cohort of plans that were authored under the requirement,
which is the corpus-rewrite this gate exists to prevent."""

_PRIME_ABSENT_NEXT_MOVE = (
    "Author prime_exit_criterion (statement + derived_from, plus a falsifier "
    "or a named falsifier_exemption) in this plan's frontmatter, then re-run "
    "the close-out."
)
"""Arm 0's own next move. `_GOAL_REFUSAL_NEXT_MOVE` is wrong here and would
misdirect: it says to re-run the observation, and this plan has no observation
to re-run -- the field that would name one was never written. Same register as
that constant (one useful move, no restatement of the refusal)."""


def _plan_created_on_or_after_grandfather(fm: dict) -> bool:
    """Whether the plan's `created` places it under the requirement.

    Fails toward GRANDFATHERED (`False`) on anything it cannot read cleanly —
    absent `created`, a non-scalar, or a value that does not lead with an ISO
    date. That direction is deliberate and is the opposite of this module's
    usual fail-loud posture: this arm's refusal blocks a close-out on a field's
    ABSENCE, so a misparse would refuse a plan that is entitled to its stamp
    and has no local evidence to argue back with. Every other arm of this gate
    refuses on something the plan positively declared and got wrong; this one
    cannot, so it declines rather than guesses.

    `created` may arrive as a `datetime.date` (the YAML loader parses an
    unquoted ISO date) or as a string, since `schema_validate`'s own leniency
    accepts both for a `type: string` field. Both are compared lexically after
    normalising to `YYYY-MM-DD`, which is correct for ISO-8601 dates and needs
    no date arithmetic."""
    created = fm.get("created")
    if isinstance(created, datetime.datetime):
        created = created.date()
    if isinstance(created, datetime.date):
        return created.isoformat() >= GRANDFATHER_DATE
    if not isinstance(created, str):
        return False
    head = created.strip()[:10]
    if not _ISO_DATE_RE.match(head):
        return False
    return head >= GRANDFATHER_DATE


_MPLUS_TSHIRTS = frozenset({"M", "L", "XL", "XXL"})


def _plan_is_m_plus(fm: dict, root: Path) -> bool:
    """Whether the plan's linked sizing object sizes it M or larger.

    The t-shirt lives in an external `state/sizings/<id>.yaml` document, which
    is why the schema enforces the falsifier requirement READ-SIDE rather than
    in JSON Schema (see plan.schema.json's own 2.6.0 bump note) — this is that
    read.

    Fails toward NOT-M-PLUS (`False`) on an absent, null, unresolvable, or
    unreadable `sizing_object`, and on a sizing object carrying no usable
    `estimate.tshirt`. Same direction and same reason as
    `_plan_created_on_or_after_grandfather`: an unread size must never be the
    thing that refuses a stamp. An S-or-XS plan omitting the criterion is
    legitimate and is not this arm's business.

    Reads at most one file and spawns nothing, so the gate's at-most-2 git
    budget (spent by `baseline_ref`'s ancestor check) is untouched."""
    sizing_ref = fm.get("sizing_object")
    if not isinstance(sizing_ref, str) or not sizing_ref.strip():
        return False
    sizing_path = root / sizing_ref.strip()
    try:
        doc = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    if not isinstance(doc, dict):
        return False
    estimate = doc.get("estimate")
    if not isinstance(estimate, dict):
        return False
    tshirt = estimate.get("tshirt")
    return isinstance(tshirt, str) and tshirt.strip().upper() in _MPLUS_TSHIRTS

_EXEMPTION_CLASS = "unfalsifiable-by-observation-doctrine-or-schema-edit"


def _falsifier_exemption(prime_exit_criterion: Any) -> Optional[dict]:
    """Total, never-raising detection of a real `falsifier_exemption`, in
    `_falsifier_block`'s own shape and for the same reason.

    Returns the exemption only when it carries BOTH required fields in a
    usable form: `class` at the single spelling the schema pins it to
    (`_EXEMPTION_CLASS` -- held to one string so a later reader cannot widen
    the hatch by inventing a new class name), and a non-blank `admission`.
    Anything else -- a non-dict criterion, an absent or non-dict exemption, a
    novel class string, a blank admission -- returns `None`, which the caller
    reads as "no exemption was taken" and proceeds to the size gate.

    The strictness is the point: this is the ONE sanctioned way past an M+
    falsifier requirement, so a malformed exemption must not buy the same
    silence a well-formed one does."""
    if not isinstance(prime_exit_criterion, dict):
        return None
    exemption = prime_exit_criterion.get("falsifier_exemption")
    if not isinstance(exemption, dict):
        return None
    if exemption.get("class") != _EXEMPTION_CLASS:
        return None
    admission = exemption.get("admission")
    if not isinstance(admission, str) or not admission.strip():
        return None
    return exemption


_FALSIFIER_ABSENT_NEXT_MOVE = (
    "Author prime_exit_criterion.falsifier (how + baseline_output + "
    "baseline_ref + expected_when_true, captured against a baseline that "
    "reads FALSE), or a named falsifier_exemption if this criterion is "
    "genuinely un-falsifiable by observation, then re-run the close-out."
)
"""The absent-falsifier arm's own next move, for `_PRIME_ABSENT_NEXT_MOVE`'s
reason: `_GOAL_REFUSAL_NEXT_MOVE` says to re-run an observation, and this
plan named none to re-run. Distinct from `_PRIME_ABSENT_NEXT_MOVE` too --
that plan has no criterion at all, this one has a criterion and no
instrument, and telling it to author a statement it already wrote misreads
its own frontmatter back to it."""


_GOAL_REFUSAL_NEXT_MOVE = (
    "Run the close-out skill, which re-runs the observation and records a fresh "
    "exit_criterion_met verdict -- this engine only ever reads what a prior run "
    "already recorded and never re-runs a plan's falsifier itself."
)
"""Shared tail for the goal-gate's refusal message, in `_FIDELITY_NEXT_MOVE`'s
own register (AC18): lead with the ONE useful next move, not a generic
refusal. A separate constant, not a reuse of `_FIDELITY_NEXT_MOVE` itself --
that constant answers a different problem (a stamp-fidelity write-diff
defect); this one answers "the observation this refusal is about was never
re-run", which the close-out skill (not this engine) is the thing that
re-runs."""


def _evaluate_goal_falsifier_gate(
    plan_text: str, root: Path
) -> Optional[dict[str, Any]]:
    """The third refusal class this gate's own module-level comment block
    names (C2): evaluated ONLY at the point `close_out_and_stamp` would
    otherwise set `status_target = "implemented"` -- a plan that is halted
    for spine reasons already skips its stamp for THAT reason, and never
    reaches here.

    Predicate, in order (see the C2 dispatch brief for the full citation
    trail):
      0. No `prime_exit_criterion` at all -> grandfathered to `None` UNLESS
         the plan is both M+ (per its linked sizing object) and `created` on
         or after `GRANDFATHER_DATE`, in which case it refuses
         (`GOAL_REFUSAL_PRIME_ABSENT`), same `status_override_*` exception as
         the arms below. Before this arm existed, absence was an
         unconditional exit and the whole gate was opt-out by omission.
         Everything the caller relies on for the genuinely grandfathered
         corpus is unchanged: still `None`, still no new result-dict key,
         which is AC7's own differential-fixture guarantee.
      5. `derived_from` resolution (AC20) -- independent of falsifier
         presence; an unresolvable link refuses (same override exception).
      1(cont). No usable `falsifier` block (`_falsifier_block` -- TOTAL,
         never-raising detection) -> refuses (`GOAL_REFUSAL_FALSIFIER_ABSENT`)
         on the SAME two bounds arm 0 uses, M+ and created on or after
         `GRANDFATHER_DATE`, and only when no well-formed
         `falsifier_exemption` was taken; otherwise refused stays `False`.
         The size bound is the whole point of this arm: without it the S-lane
         carve-out silently applied to every lane.
      2. `exit_criterion_met` absent, or present with `asserted: false` ->
         refuse (AC4), unless a current `status_override_*` attestation is
         present (AC19).
      3. `falsifier.baseline_ref` fails `_verify_disposition_ref` -> refuse,
         naming which of its four reasons (AC6), same override exception.
      4. `exit_criterion_met.falsifier_verdict != "pass"` -> refuse (AC5) --
         one enum read, never a comparison of `falsifier_output` against
         `baseline_output` (the retired delta rule).

    Returns `None` only for step 1's absent-prime-exit-criterion case;
    every other path returns a dict:
    `{"refused": bool, "reason": Optional[str], "detail": Optional[str],
    "override": bool}`. `override` is `True` whenever a current
    `status_override_*` attestation suppressed what would otherwise have
    been a refusal (AC19) -- callers must report that as an override, never
    as a clean stamp. Never raises."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    prime = fm.get("prime_exit_criterion")
    if prime is None:
        # Arm 0 (the grandfather-date pin). Absence of `prime_exit_criterion`
        # was the ONLY unconditional exit from this gate, which made the gate
        # skippable by simply not declaring the field -- an EM who never wrote
        # a criterion was never caught, while one who wrote it and got the
        # observation wrong was. Every other arm judges a positive declaration;
        # this one is why the corpus could opt out of all of them.
        #
        # Bounded on BOTH axes so the refusal only reaches plans the
        # requirement was actually authored for: `created` on or after
        # GRANDFATHER_DATE, and M+ per the linked sizing object (the same
        # read-side size rule plan.schema.json names for the falsifier itself).
        # Either predicate failing to read cleanly declines -- see each
        # helper's own docstring for why this arm alone fails toward
        # grandfathering rather than toward refusal.
        if not _plan_created_on_or_after_grandfather(fm):
            return None
        if not _plan_is_m_plus(fm, root):
            return None
        if _read_status_override(plan_text) is not None:
            return {
                "refused": False,
                "reason": None,
                "detail": None,
                "override": True,
            }
        return {
            "refused": True,
            "reason": GOAL_REFUSAL_PRIME_ABSENT,
            "detail": (
                "plan declares no prime_exit_criterion, and is sized M+ with "
                f"created on or after {GRANDFATHER_DATE} (plans created before "
                "that date are grandfathered and skip this check)"
            ),
            "override": False,
        }

    result: dict[str, Any] = {
        "refused": False,
        "reason": None,
        "detail": None,
        "override": False,
    }
    override = _read_status_override(plan_text)

    derived_from = prime.get("derived_from") if isinstance(prime, dict) else None
    if isinstance(derived_from, str) and derived_from.strip():
        failure = _resolve_derived_from(derived_from.strip(), root)
        if failure is not None:
            if override is None:
                result["refused"] = True
                result["reason"] = GOAL_REFUSAL_DERIVED_FROM_UNRESOLVABLE
                result["detail"] = failure
                return result
            result["override"] = True

    falsifier = _falsifier_block(prime)
    if falsifier is None:
        # Arm 1(cont), size-gated. A plan that declares a criterion and no
        # usable falsifier used to leave here non-refusing at EVERY lane,
        # which applied the S-lane carve-out to all of them: the schema
        # expresses the carve-out as a rule keyed on `estimate.tshirt`, and
        # nothing enforced it, so an M+ plan that never authored an
        # observation was indistinguishable from an S plan that was never
        # asked for one. Measured 2026-08-27 against four fixtures differing
        # only in t-shirt (S/M/L/XL, all non-refusing) --
        # cross-repo/archive/2026-08-27-doe-claude-em-ac-12-needs-a-size-gate-not-the-verdict-gate.md.
        #
        # Bounded exactly as arm 0 is, and for the same reasons: grandfather
        # date first, then M+, each failing toward NOT refusing. A named
        # `falsifier_exemption` is checked ahead of both -- it is the
        # schema's own escape hatch for an M+ criterion genuinely
        # un-falsifiable by observation, and refusing a plan that took the
        # sanctioned route would punish the discipline the hatch exists to
        # reward.
        if _falsifier_exemption(prime) is not None:
            return result
        if not _plan_created_on_or_after_grandfather(fm):
            return result
        if not _plan_is_m_plus(fm, root):
            return result
        if override is not None:
            result["override"] = True
            return result
        result["refused"] = True
        result["reason"] = GOAL_REFUSAL_FALSIFIER_ABSENT
        result["detail"] = (
            "plan declares prime_exit_criterion with no usable falsifier, and "
            f"is sized M+ with created on or after {GRANDFATHER_DATE} (an M+ "
            "criterion genuinely un-falsifiable by observation takes a named "
            "falsifier_exemption instead; S and XS carry no falsifier at all)"
        )
        return result

    exit_criterion_met = fm.get("exit_criterion_met")
    if not isinstance(exit_criterion_met, dict) or "asserted" not in exit_criterion_met:
        if override is None:
            result["refused"] = True
            result["reason"] = GOAL_REFUSAL_EXIT_CRITERION_ABSENT
            result["detail"] = "exit_criterion_met is absent or not an object carrying 'asserted'"
            return result
        result["override"] = True
        return result

    if exit_criterion_met.get("asserted") is False:
        if override is None:
            result["refused"] = True
            result["reason"] = GOAL_REFUSAL_NOT_ASSERTED
            result["detail"] = (
                "exit_criterion_met.asserted is false: "
                f"{exit_criterion_met.get('reason') or 'no reason recorded'}"
            )
            return result
        result["override"] = True
        return result

    baseline_ref = falsifier.get("baseline_ref")
    sha, ref_reason = _verify_disposition_ref(root, baseline_ref)
    if sha is None:
        if override is None:
            result["refused"] = True
            result["reason"] = f"{GOAL_REFUSAL_BASELINE_REF_PREFIX}{ref_reason}"
            result["detail"] = (
                f"prime_exit_criterion.falsifier.baseline_ref did not verify: {ref_reason}"
            )
            return result
        result["override"] = True
        return result

    verdict = exit_criterion_met.get("falsifier_verdict")
    if verdict != "pass":
        if override is None:
            result["refused"] = True
            result["reason"] = GOAL_REFUSAL_VERDICT_NOT_PASS
            result["detail"] = f"exit_criterion_met.falsifier_verdict is {verdict!r}, not 'pass'"
            return result
        result["override"] = True
        return result

    return result


# Admits `disposition_detail:` to the fidelity gate's allowed-change set
# DELIBERATELY, alongside `disposition:`/`disposition_ref:` -- this stamper
# now writes all three fields (DR-103), so `_assert_stamp_fidelity` below
# must recognize a `disposition_detail:` line as an expected touch, not an
# unrelated-line corruption.
_STAMP_LINE_RE = re.compile(r"^[ \t]*disposition(?:_ref|_detail)?:[ \t]")














_FIDELITY_NEXT_MOVE = (
    "Fix the row-span / stamp-assembly logic in _stamp_rows_in_body -- that "
    "is where this divergence is produced. Your plan file was NOT modified: "
    "this gate runs before any write, so there is nothing to restore from "
    "git, and the failure is deterministic, so re-running close-out-and-stamp "
    "reproduces it rather than clearing it."
)
"""Shared tail for every `_assert_stamp_fidelity` refusal message.

Design-as-offers (memo, example-cockpit-repo-em 2026-08-01): leads with the ONE
useful next move -- where the defect lives -- instead of the two misleading
instructions this text used to carry. "Restore the plan from git" implied a
damaged file when the gate refuses BEFORE any write happens, and "re-run
close-out-and-stamp" invited a retry loop against a deterministic stamper
defect. Both facts are now stated explicitly, after the next move, so a
reader who was about to do either stops."""


def _assert_stamp_fidelity(
    old_text: str, new_text: str, plan_path_rel: str
) -> Optional[str]:
    """Step 2 fidelity gate: every line of `old_text` must still be
    present verbatim in `new_text`, except that a row's `disposition:` /
    `disposition_ref:` / `disposition_detail:` lines may have been changed
    or newly inserted. Returns `None` when the write is safe to land; otherwise a fail-loud,
    design-as-offers-worded error naming `plan_path_rel` and the first
    diverging line -- the caller must refuse to write, commit, or push on
    any non-`None` return (this is the correctness backstop for the exact
    failure mode this fix addresses: a lossy re-dump silently destroying
    unrelated content).

    Deliberately independent of `_stamp_rows_in_body`'s own bookkeeping --
    this diffs the ACTUAL before/after text via `difflib.SequenceMatcher`
    (the same line-oriented algorithm `git diff` and `difflib.unified_diff`
    build on) rather than trusting the stamper's insert/replace indices,
    so a bug in the stamper's own row-span math is still caught here
    rather than silently landing. Every non-`equal` diff opcode's touched
    lines (both sides) must match `_STAMP_LINE_RE`; a `delete` opcode (an
    original line vanishing outright) is refused unconditionally, since
    stamping never removes a line.

    Review: code-reviewer -- F3: matching `_STAMP_LINE_RE` against an
    already-`.strip()`-ed line made its own `^[ \\t]*` prefix vacuous, and
    even matched against the raw line the pattern's `[ \\t]*` is a
    wildcard -- neither form alone can tell a correctly-indented stamp
    line from a wrongly-indented one. This gate independently re-derives
    each touched line's OWN row from `old_text` (never trusting
    `_stamp_rows_in_body`'s bookkeeping) and asserts the touched line's
    actual leading-whitespace width equals that row's own measured
    content indent (`_measure_row_content_indent`, the same
    independently-correct measurement `_stamp_rows_in_body` itself now
    uses per F4) -- so a stamp landing at the wrong indent is refused here
    even if the stamper's own row-span math got it wrong."""
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
                f"{plan_path_rel}: refusing to write -- stamp fidelity check "
                f"found an original line removed where only a disposition/"
                f"disposition_ref change was expected (first diverging line: "
                f"{first!r}). {_FIDELITY_NEXT_MOVE}"
            )

        # Independently re-derive which row this change belongs to, and
        # that row's own expected content indent -- anchored on the line
        # immediately BEFORE the change (an `insert` opcode has i1 == i2,
        # so there is no touched old-side line to anchor on directly; the
        # line right before the insertion point is always the true owning
        # row, including the edge case of an insertion at a row's very
        # end, which lands exactly on the next row's start index).
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
            if not _STAMP_LINE_RE.match(line):
                return (
                    f"{plan_path_rel}: refusing to write -- stamp fidelity "
                    "check found a change outside the disposition/"
                    f"disposition_ref/disposition_detail fields (first diverging line: {line!r}). "
                    f"{_FIDELITY_NEXT_MOVE}"
                )
            if expected_indent is not None:
                actual_indent = len(line) - len(line.lstrip(" \t"))
                if actual_indent != expected_indent:
                    return (
                        f"{plan_path_rel}: refusing to write -- stamp fidelity "
                        "check found a disposition/disposition_ref/disposition_detail "
                        f"line at "
                        f"indent {actual_indent} but this row's own content "
                        f"indent is {expected_indent} (first diverging line: "
                        f"{line!r}). {_FIDELITY_NEXT_MOVE}"
                    )
    return None


def _peek_plan_status(plan_text: str) -> Optional[str]:
    """Best-effort read of the plan's current, normalized `status:`
    frontmatter value -- mirrors the identical parse/strip/unquote steps
    `_stamp_plan_landed` (below) and `plan_status_transition._stamp_implemented`
    each perform internally, used here ONLY so `close_out_and_stamp` can
    tell, from the OUTSIDE and BEFORE calling either stamp helper, whether
    that call is about to hit one of their own documented no-op branches
    (already-terminal / already-at-target). Review finding, 2026-07-27:
    neither helper's return code alone distinguishes "wrote a real change"
    from "no-op, rc=0" -- both return 0 on their no-op branches too -- so a
    caller that sets `stamped = True` on `rc == 0` unconditionally is wrong
    on an idempotent re-run against an already-terminal/already-landed
    plan: `stamped` reads `True` against a byte-clean `plan.md`, and the
    commit leg then attempts (and fails loud on) a zero-diff commit. See
    `close_out_and_stamp()`'s own docstring, "Commit leg gated on
    `wrote_anything`" section.

    Returns `None` on any parse failure (no frontmatter, no status field,
    an unsupported quoted-scalar-plus-trailing-comment shape). Both call
    sites below already know the stamp call itself will fail loud
    (`stamp_rc != 0`) on the identical parse failure and return before
    `stamped` is ever set from this value -- so a `None` here is never
    read as a specific status, only as "the stamp call is about to error
    out anyway."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    status = read_fm_field(split.fm_text, "status")
    if status is None or status.startswith("#"):
        return None
    status = _strip_unquoted_trailing_comment(status)
    if status and status[0] in ("'", '"') and not status.endswith(status[0]):
        return None
    return unquote_yaml_scalar(status)


def _stamp_plan_landed(
    plan_path: str,
    *,
    dry_run: bool = False,
    plan_text: Optional[str] = None,
    timeout: float = LOCK_TIMEOUT_SECS,
) -> int:
    """Flips a plan's `status:` frontmatter field to `landed` (D9) -- the
    intermediate status meaning "every chunk's code is on the branch, but
    not every spine row has reached a disposition".

    Mirrors `coordinator_core.ops.plan_status_transition._stamp_implemented`'s
    parse/gate/write shape -- there is no `stamp-landed` verb there to
    compose over (`landed` is new territory this plan introduces, and
    `plan_status_transition.py` is outside this module's write-scope) --
    but imports that module's already-correct `_FROZEN_STATUSES` /
    `_FLIPPABLE_STATUSES` / `_strip_unquoted_trailing_comment` rather than
    re-deriving the comment-stripping/quote-handling edge cases it already
    solved: composition of the reusable PARTS, not a second independent
    parser. `landed` itself is a member of `_FLIPPABLE_STATUSES` (that
    module bucket it there, C8a, D9 -- flippable onward to `implemented`,
    not terminal), so gating this write against those same two sets keeps
    the two stamps' safety posture identical: neither ever resurrects an
    abandoned/deferred/superseded/implemented plan, and both are
    idempotent no-ops when already at their own target.

    Returns 0 on transition-applied or no-op, 1 on error -- the same
    exit-code contract `_stamp_implemented` uses.

    `dry_run` (2026-08-04, `--dry-run` mode -- see `close_out_and_stamp`'s
    own docstring): the transition decision below (frozen/no-op/landed/
    unexpected-status) runs completely unchanged; only the final disk
    write is skipped -- the write is a purely mechanical last step
    (persisting an already-computed `rebuilt` string) that plays no part
    in the decision itself, so suppressing it changes nothing this
    function reports. `plan_text` (dry-run callers only) supplies the
    CURRENT in-memory plan text to operate on instead of re-reading
    `plan_path` from disk -- required because a preceding dry-run AC8
    auto-resolve step (if any) never persisted its own change to disk, so
    re-reading the live file here would see STALE, pre-auto-resolve
    content; `plan_text` lets this call see the same effective input a
    live run's own sequential writes would have produced. Live callers
    never pass `plan_text` -- `None` preserves the original
    read-from-disk behavior verbatim.

    `timeout` (seconds) is forwarded to `locked_write.locked_rmw`'s own
    `timeout` on the disk-read path only (see below) -- test-only knob,
    live callers rely on the default `LOCK_TIMEOUT_SECS`."""
    # Review: code-reviewer (P2 #1) -- C1 (plan_tasks_mutate.resolve) now
    # calls this function from a hot path reached far more frequently and
    # from more concurrent contexts than the low-frequency close-out
    # ceremony it previously served alone, on a machine whose own doctrine
    # names 50-70 concurrent LLM sessions as average load (repo CLAUDE.md
    # § Load norm). An unlocked plain read/write here is a lost-update
    # window. `state` carries the parse/decision outcome out of the
    # `_mutate` closure below so the surrounding rc/message logic (which
    # must stay byte-identical to the pre-lock version) can read it without
    # `locked_rmw` needing to know anything about this function's specific
    # status-transition contract.
    state: dict[str, Any] = {}

    def _mutate(old_text: str) -> str:
        original = old_text.replace("\r\n", "\n")
        split = split_frontmatter(original)
        if split is None:
            state["error"] = (
                f"close-out-and-stamp: no parseable YAML frontmatter in {plan_path}"
            )
            raise MutateAbort(state["error"])

        status = read_fm_field(split.fm_text, "status")
        if status is None or status.startswith("#"):
            state["error"] = (
                f"close-out-and-stamp: no \"status\" field found in frontmatter of {plan_path}"
            )
            raise MutateAbort(state["error"])
        status = _strip_unquoted_trailing_comment(status)
        if status and status[0] in ("'", '"') and not status.endswith(status[0]):
            state["error"] = (
                f"close-out-and-stamp: status value appears to carry a "
                "quoted-scalar-plus-trailing-comment, which stamp-landed does not "
                f"support -- remove the inline comment or the quotes ({plan_path})"
            )
            raise MutateAbort(state["error"])
        status = unquote_yaml_scalar(status)

        if status in _FROZEN_STATUSES:
            state["noop_message"] = (
                f"close-out-and-stamp: {plan_path} status \"{status}\" is terminal/deferred — no-op"
            )
            return old_text
        if status == _LANDED_STATUS:
            state["noop_message"] = (
                f"close-out-and-stamp: {plan_path} status already \"landed\" — no-op"
            )
            return old_text
        if status not in _FLIPPABLE_STATUSES:
            state["error"] = (
                f"close-out-and-stamp: unexpected current status \"{status}\" for stamp-landed"
            )
            raise MutateAbort(state["error"])

        state["prior_status"] = status
        fm_text = replace_fm_field(split.fm_text, "status", _LANDED_STATUS)
        return rebuild(split, fm_text)

    if plan_text is not None:
        # Dry-run / ceremony caller supplying already-in-memory text (see
        # this function's own docstring on `plan_text`) -- the surrounding
        # ceremony is already a multi-step, non-atomic sequence at the
        # call site, and the low-frequency path this serves is the one the
        # review explicitly characterised as pre-existing/tolerable, not
        # the hot path this fix targets. Locking a single sub-step of an
        # already-non-atomic multi-write ceremony would not make the
        # ceremony atomic, so this branch keeps its original unlocked
        # decide-then-write shape verbatim.
        try:
            new_text = _mutate(plan_text.replace("\r\n", "\n"))
        except MutateAbort:
            print(state["error"], file=sys.stderr)
            return 1

        if not dry_run and "noop_message" not in state:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)

        if "noop_message" in state:
            print(state["noop_message"])
            return 0
        suffix = " (dry-run, not written)" if dry_run else ""
        print(f"close-out-and-stamp: {plan_path} status \"{state['prior_status']}\" → landed{suffix}")
        return 0

    if not os.path.exists(plan_path):
        print(f"close-out-and-stamp: plan not found: {plan_path}", file=sys.stderr)
        return 1

    try:
        locked_rmw(Path(plan_path), _mutate, repo_root=Path(plan_path), timeout=timeout)
    except LockTimeout as exc:
        print(
            f"close-out-and-stamp: timed out waiting for file lock on {plan_path}: {exc}",
            file=sys.stderr,
        )
        return 1
    except MutateAbort:
        print(state["error"], file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"close-out-and-stamp: plan not found: {plan_path}", file=sys.stderr)
        return 1

    if "noop_message" in state:
        print(state["noop_message"])
        return 0
    print(f"close-out-and-stamp: {plan_path} status \"{state['prior_status']}\" → landed")
    return 0


_CLOSE_OUT_PARTIAL_FIELD = "close_out_last_partial"


def _close_out_partial_stamp_value(missing_chunk_ids: list[str]) -> str:
    """The single-line value `_stamp_close_out_partial_evaluation` writes --
    a UTC timestamp plus the exact verdict this run reached, so a later
    reader sees not just THAT a partial evaluation happened but WHAT it
    found and WHEN (see that function's own docstring for the defect this
    closes)."""
    timestamp = (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ids = ",".join(missing_chunk_ids)
    return f"{timestamp} -- {len(missing_chunk_ids)} missing: {ids}"


def _stamp_close_out_partial_evaluation(
    plan_text: str,
    missing_chunk_ids: list[str],
) -> Optional[str]:
    """Defect 2 fix (2026-08-06): "a skipped stamp is indistinguishable from
    an unrun one." Before this fix, the halted/partial branch of
    `close_out_and_stamp` wrote NOTHING to the plan at all unless AC8's
    auto-resolve happened to fire on some OTHER row -- so a plan this
    ceremony genuinely evaluated and correctly declined to stamp (missing
    chunks, correctly reported) was, on disk, byte-identical to a plan
    nobody had ever run this ceremony against at all. A later reader (human
    or another op) had no way to tell "ran, found partial, correctly
    declined" from "never evaluated" short of re-running the whole ceremony
    and hoping to catch a stale git-log range.

    The fix reuses this module's OWN existing write mechanism -- the
    `replace_fm_field`/`insert_fm_field`/`rebuild` frontmatter-stamp
    primitives `_stamp_plan_landed` (above) and `plan_status_transition`
    are both already built on, already imported into this module -- rather
    than a second writer, a new state file, or a new artifact type: it
    writes ONE additional scalar frontmatter field,
    `close_out_last_partial:`, recording the UTC timestamp this run
    evaluated the plan plus its own verdict (missing-chunk-id list,
    `_close_out_partial_stamp_value`'s own format).
    This is the SAME kind of write this op already makes elsewhere (a
    single frontmatter scalar), just a second field instead of `status:` --
    not a new mechanism, and not a plan-body/Dispatch-Ledger/Tasks-spine
    write, which stay exactly as untouched as they always have been (this
    module's own docstring negative-spec on `status:` being the ONLY write
    describes the SHIPPED path's stamp; this field is the halted path's own
    analogous, narrower stamp).

    Fires ONLY on the halted path (`missing_chunk_ids` non-empty -- callers
    gate this), and is skipped entirely when the plan's frontmatter cannot
    be split at all (mirrors every other write helper's degrade-safe
    posture: never crash on an unparseable document; `close_out_and_stamp`
    itself has already refused earlier for that case in practice, but this
    helper does not assume that invariant holds forever).

    Idempotent by PRESENCE, not by value, ON THE HALTED PATH ONLY
    (deliberate choice, NOT the obvious "always refresh the timestamp"
    design): while this plan keeps evaluating as halted, once
    `close_out_last_partial:` exists at all, a later halted-path call
    NEVER rewrites it, even when this run's own verdict (missing-id list,
    provenance) differs from what is already stamped. The alternative --
    rewriting on every call -- was tried and reverted during this fix's own
    test pass: it silently broke the pre-existing "a halted plan with
    nothing new to report is a genuine no-op" guarantee
    (`TestCloseOutAndStampContinued
    ::test_partially_shipped_with_nothing_committed_at_all_is_a_genuine_
    noop`, `TestIdempotentRerunDoesNotAttemptAZeroDiffCommit`'s sibling
    invariant for the halted path) -- every repeat close-out call against
    an unchanged, still-halted plan would mint a fresh commit purely from
    the timestamp ticking forward, which is not what "evaluated and found
    partial" needs to mean. One evaluation record per plan is sufficient
    to answer the question this fix exists for ("has ANYONE ever run
    this ceremony against this plan") -- a STALE recorded verdict is a
    strictly better failure mode than a perpetually dirtying halted plan,
    and `missing_chunk_ids` in this call's own live return value is
    already the CURRENT verdict regardless of what the frontmatter
    field's own last-recorded snapshot says. This never-rewrite-on-repeat
    posture is UNCHANGED and still load-bearing -- nothing about it moved.

    Note (Review: coordinator:code-reviewer -- SUPERSEDED, C1,
    2026-08-08): this note originally claimed the field is "NOT cleared or
    refreshed" once a plan ships, and that a stale field is "harmless to
    any programmatic reader (`status:` always dominates)". Both claims are
    now FALSE, not merely stale. `close_out_and_stamp`'s certified-ship
    path (`status_target == "implemented"`) now clears
    `close_out_last_partial:` from the plan's frontmatter BEFORE calling
    `archive_stamp.cs_stamp_plan_implemented` -- see that call site's own
    comment for why the clear must precede the stamp, not follow it. So a
    plan that ships via this op's own certified path, WHEN THE STAMP ITSELF
    SUCCEEDS, never carries a stale marker at all. A stamp failure after the
    clear (`stamp_rc not in (0, 2)`) is a narrower, different case -- the
    call site now restores the marker on disk before returning, so this
    still does not leave a stray false-clean marker behind; see that call
    site's own comment for the reachable scenario this covers (a real,
    non-no-op status flip whose own commit attempt fails). The "harmless to
    any programmatic reader" half is doubly
    stale: `coordinator_core.workstream_complete` now reads this field as a
    programmatic signal in its own right (leg A) -- the exact reader this
    claim did not anticipate -- so a stray, uncleared marker is no longer
    harmless-by-construction; it is load-bearing input to a different op.

    Returns the rewritten plan text, or `None` when nothing was written
    (frontmatter unparseable, OR the field is already present -- both
    read as "no write needed" to the caller identically). Pure -- like
    `_stamp_plan_landed`'s own transition-decision half, this performs NO
    disk I/O itself; the caller (`close_out_and_stamp`) owns the single
    live-path write, gated on `dry_run` exactly the way its other two
    stamp branches already are."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    if read_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD) is not None:
        return None
    value = _close_out_partial_stamp_value(missing_chunk_ids)
    new_fm = insert_fm_field(
        split.fm_text, _CLOSE_OUT_PARTIAL_FIELD, value, after_key="status"
    )
    return rebuild(split, new_fm)


def _clear_close_out_partial_marker(plan_text: str) -> Optional[str]:
    """Certified-ship counterpart to `_stamp_close_out_partial_evaluation`
    (C1, 2026-08-08 -- `docs/plans/2026-08-08-a-status-field-cannot-vouch-
    for-itself.md`): removes `close_out_last_partial:` from `plan_text`'s
    own frontmatter, via the same `remove_fm_field`/`rebuild` primitives
    every other write in this module already composes over -- not a second
    writer.

    Returns the rewritten text, or `None` when there is nothing to clear
    (frontmatter unparseable, OR the field is already absent) -- identical
    "no write needed" contract to `_stamp_close_out_partial_evaluation`'s
    own return convention, so callers can gate a write on `is not None`
    the same way in both directions.

    Placement is load-bearing and MUST NOT be re-derived by a future
    reader: the only caller (`close_out_and_stamp`'s `status_target ==
    "implemented"` branch) calls this, and writes the result to
    `live_path`, BEFORE calling `archive_stamp.cs_stamp_plan_implemented`
    -- never after. `cs_stamp_plan_implemented` forwards to
    `plan_status_transition._stamp_implemented`, which reads the LIVE FILE
    off disk itself (via its own locked read-modify-write) and commits its
    write itself; this module's own in-memory `text` is never re-read
    after that call returns. A write-back mirroring the halted-path
    marker's OWN placement (mutate `text`, write it out AFTER the stamp
    call) would silently REVERT `status: implemented` back to whatever
    status `text` still held at that point, since `text` predates the
    stamp's own disk write. Clearing first means `plan_status_transition`'s
    `locked_rmw` reads the already-cleaned file and flips `status:` in the
    SAME read-modify-write, landing both changes in one commit."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    if read_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD) is None:
        return None
    new_fm = remove_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD)
    return rebuild(split, new_fm)


def _dry_run_scratch_plan(text: str, suffix: str) -> Path:
    """`--dry-run` support helper: materializes `text` (this ceremony's own
    in-memory plan content -- reflecting any dry-run-computed AC8
    auto-resolve change, which a dry run never persists to disk) into a
    throwaway file in the system temp directory.

    Why this exists (composition, not duplication -- see this module's own
    docstring § "Composition, not duplication"): `archive_stamp.
    cs_stamp_plan_implemented` has no in-memory-text entry point of its own
    -- it forwards straight to `plan_status_transition.main(["stamp-
    implemented", "--plan", ...])`, a byte-parity port of the node
    stamp-implemented oracle that reads its `--plan` argument from disk
    internally. Re-deriving that port's own frozen/flippable/unexpected-
    status decision matrix locally (the way `_stamp_plan_landed` above
    does for the DIFFERENT `landed` transition) would be exactly the kind
    of second, drifting implementation this module's docstring warns
    against for the `implemented` transition specifically -- there IS a
    canonical single writer for it, `cs_stamp_plan_implemented`, and this
    op composes over it rather than parallel-implementing it (see
    `close_out_and_stamp`'s own docstring, "Composition, not
    duplication").

    So a `--dry-run` call to the `implemented` transition instead invokes
    the REAL, canonical function -- unmodified -- against a scratch COPY of
    the plan's current in-memory content, never the live file. The
    resulting exit code is therefore byte-identical to what a live call
    would report (same function, same input bytes), while the live file on
    disk is never touched. Caller deletes the returned path once done (see
    `close_out_and_stamp`'s own `--dry-run` call site, which does so in a
    `finally:` block) -- this helper does not clean up after itself, since
    the caller needs the file to exist for the duration of the call it
    wraps."""
    fd, tmp_name = tempfile.mkstemp(prefix="close-out-and-stamp-dry-run-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def _reach_post_commit_tail_stub_close(
    root: Path,
    plan_path_rel: str,
    committed_sha: str,
    delivery_proof: Optional[dict] = None,
) -> dict:
    """Give this ceremony's own successful commit reach to
    `post_commit_tail`'s origin-stub-close leg (step 5d) -- the SAME
    composition `ceremony.wsc_tail` already invokes in-process (see
    `post_commit_tail.run()`'s own docstring), never a second copy of its
    join/scan/guard logic.

    `delivery_proof` (optional; PM ruling -- let a positive, complete
    delivery proof close the origin stub directly, with the live-children
    guard retained as the fallback for callers holding no such proof) is
    forwarded VERBATIM into `post_commit_tail.run()`, which forwards it
    verbatim into `handoff.close_origin_stub`'s own `delivery_proof` param --
    this function neither builds nor validates it; the caller (this
    function's own two call sites, both inside the `status_target ==
    "implemented"` branch, the only place this ceremony has a proof to give)
    constructs it from THIS run's own already-computed `shipped`/`missing`/
    `plan_deliverable_id`/`status_target` values (plus a literal
    `join_provenance: "joined"`, preserved for that downstream contract --
    see `close_out_and_stamp`'s own delivery_proof comment) -- see
    `handoff_close_origin_stub._is_complete_delivery_proof`
    for the exact completeness conditions it is checked against downstream.
    `None` (the default) preserves today's guard-only behaviour exactly.

    Both `/execute-plan`'s close-out and `/mise-en-place`'s per-baton tail
    land here through the SAME `coordinator/bin/close-out-and-stamp.py` ->
    `close_out_and_stamp()` call path, so wiring this one call site gives
    both ceremonies reach in one place (spec: docs/plans/2026-08-04-
    terminal-state-propagation-join-keys.md § C5).

    `chain_terminal=False` on the composed call is deliberate: it makes
    `post_commit_tail.run()`'s OTHER composed step
    (`consumed_handoff_stamp.post_commit_stamp_and_ship`) a documented,
    side-effect-free no-op here -- this ceremony has no WSC session id and
    owns no consumed-handoff set of its own. Only the origin-stub-close leg
    is a genuine reach target for this ceremony.

    ~~and stamping consumed handoffs is `ceremony.wsc_tail`'s job, not this
    one's.~~ **Struck 2026-08-30.** That deferral named an owner that stopped
    existing: K-046 deleted `ceremony.wsc_tail` on 2026-08-23 (`c07062c99`).
    Nothing inherited the job. Because THIS is the only live call site of
    `post_commit_tail.run()`, and it hardcodes `chain_terminal=False`,
    `post_commit_stamp_and_ship` now has ZERO reachable invocations anywhere
    in the tree -- the suppression above is correct for this ceremony and is
    simultaneously the whole reason the consumed-handoff ship-stamp never
    fires for anyone. Keep the `False`; it is not this ceremony's job. The
    missing owner is kill-ledger K-046's standing requirement.

    `initial_consumed=[]` -- this ceremony resolves no consumed handoffs of
    its own; the plan path alone (via `governing_plan_slug`) is the join
    key `_run_origin_stub_close` needs (C1's `deliverable_id` fallback join
    is what makes that plan-path-only join actually resolve something).

    Never raises -- `post_commit_tail.run()`'s origin-stub-close leg is
    already soft-fail-and-record internally; a failure here surfaces only
    inside the returned `{acted, skipped, failed}` dict.
    """
    governing_plan_slug = Path(plan_path_rel).stem
    common_dir = git_common_dir(root)

    async def _run() -> post_commit_tail.PostCommitTailOutcome:
        return await post_commit_tail.run(
            root,
            common_dir,
            "",
            committed_sha,
            chain_terminal=False,
            governing_plan_slug=governing_plan_slug,
            initial_consumed=[],
            close_origin_stub_handler=_close_origin_stub_handler,
            delivery_proof=delivery_proof,
        )

    import asyncio

    outcome = asyncio.run(_run())
    return outcome.origin_stub_result


def _stage_paths_committed_already(root: Path, stage_paths: Sequence[str]) -> bool:
    """True iff none of `stage_paths` carries any uncommitted change
    (staged or unstaged) per `git status --porcelain` -- i.e. this ceremony's
    own writes to those paths already landed in a commit made by SOMEONE
    ELSE before this op's own commit leg got a chance to run.

    Exists for the DR-272 interaction (`plan_status_transition._commit_plan_
    flip`, 2026-08-05/06): `cs_stamp_plan_implemented` -> `_stamp_implemented`
    now commits its own real (non-no-op) status flip immediately, under its
    own name, via `git_native.commit_authored_content` -- so by the time
    control returns to THIS function, the plan doc this op just stamped (and,
    when AC8 also fired, auto-resolved) is very often ALREADY sitting at
    HEAD, byte-identical to the worktree. `coordinator_core.git.commit.
    commit_paths` (C3, repointed off the killed `run_commit_pipeline`) does
    not distinguish that from a genuinely dirty path either -- it commits
    whatever tree the resolved blobs assemble to regardless of whether that
    tree actually differs from HEAD's, so without this check
    `close_out_and_stamp` would reach `commit_paths` with nothing new to
    commit and land a real, spurious commit object pointing at an
    unchanged tree, rather than git's own "nothing to commit" refusal the
    old `run_commit_pipeline`/`git commit` path used to raise. See this
    function's only call site for how the two are told apart.

    Historical note (W3, docs/plans/2026-08-08-a-landed-commit-reported-as-
    failed.md): under the now-deleted `run_commit_pipeline`, this same
    "already landed" case surfaced as an ordinary `commit_failed=True` exit,
    which this function's check existed to tell apart from a genuine
    refusal. `commit_paths` carries no such no-op detection of its own (see
    above), so this function's check is now the ONLY thing preventing a
    spurious empty-tree commit, not merely the loudest way to explain one.

    Deliberately narrower than a repo-wide dirty check: scoped to exactly
    `stage_paths` (this op's own pathspec), so a live peer session's
    unrelated dirty file elsewhere in the shared worktree never influences
    this decision.
    """
    result = git_native.status_porcelain(root)
    if not result.ok:
        # A porcelain-query failure is not evidence of "already committed" --
        # fall through to the ordinary commit attempt, which will surface
        # its own diagnostic if something is genuinely wrong.
        return False
    dirty_paths = {line[3:] for line in result.stdout.splitlines() if line}
    return not (dirty_paths & set(stage_paths))


_AC_HEADING_RE = re.compile(r"^## Acceptance Criteria\s*$", re.MULTILINE | re.IGNORECASE)
"""Anchors the plan's `## Acceptance Criteria` section -- the PM-authored
surface the reviewer's finding names as never consulted by this module's
own spine-only completeness oracle (see this module's docstring, and
`_ac_table_desync_finding` below).

Case-insensitive (Review: code-reviewer -- Finding [P3], 2026-08-06): real
plans vary the heading's casing (`## Acceptance criteria`, `## acceptance
CRITERIA`); a case-sensitive match silently reads those as "no AC heading
at all" and the desync check goes quiet on exactly the plans it exists to
watch. Widening costs nothing on the false-positive axis -- this regex
only decides WHERE the AC section starts, never whether a row reads
resolved -- and the failure direction if some other `## ...` heading ever
coincidentally matched case-insensitively would still only ever be "no
finding" or a mis-scoped section that itself degrades to `_parse_ac_table_
rows` returning `None`, never a spurious desync report (C3)."""

_AC_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

_AC_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_AC_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")

_AC_UNRESOLVED_STATUS_RE = re.compile(r"^(?:$|(?:pending|todo|tbd|open))", re.IGNORECASE)
"""Matches an EMPTY `Status` cell, or one whose vocabulary word OPENS the cell.

Word-bounded rather than whole-cell anchored: the anchored form read
`open -- owned by X` as resolved, because the trailing reason defeated the
`$`. A cell that names an unresolved status and then explains itself is still
unresolved, and that shape is exactly how an author records WHO owes the
resolution. The empty-cell alternative is kept explicit (a bare `\b` after an
optional group would match every cell, resolved ones included)."""
_AC_STRIKETHROUGH_CELL_RE = re.compile(r"^~~(.+)~~$", re.DOTALL)
"""Matches a `Status` cell whose ENTIRE (whitespace-trimmed) content is
struck through, e.g. `~~pending~~` -- see `_ac_status_is_unresolved` for
why this must be checked BEFORE `strip("*_~")` runs (Review: code-reviewer
-- Finding [P2], 2026-08-06): stripping the delimiters first collapses
`~~pending~~` down to the bare vocabulary word `pending`, silently
defeating the deliberate "struck-through text reads as author-resolved"
rule this constant's own sibling `_AC_UNRESOLVED_CHECKBOX_GLYPHS` already
documents. Only a WHOLE-cell strikethrough counts -- a cell that merely
contains a struck-through fragment alongside other text (`~~pending~~,
now green`) does not match this anchor and falls through to the ordinary
vocabulary check on its own (post-strip) merits."""
_AC_UNRESOLVED_CHECKBOX_GLYPHS = frozenset({"☐"})
"""The narrow, corpus-derived set of AC `Status` cell values this check
treats as UNRESOLVED (D1/C1) -- deliberately conservative (a check with a
high false-positive rate is worse than none, per this stub's own Report
instruction): a blank cell, `pending`, `todo`, `tbd`, `open`, or an empty
checkbox glyph `☐`. Everything else -- `✅`, `☑`, `green (<sha>)`,
`done (<chunk>)`, `superseded`, struck-through `~~...~~` text, or any other
free-form terminal-looking value -- is treated as RESOLVED, on purpose:
this check exists to catch the specific desync the reviewer named (spine
fully resolved, AC table still reads exactly like nobody ever touched it),
not to adjudicate every possible AC status vocabulary a plan author might
invent. See `docs/plans/*.md` § Acceptance Criteria for the corpus this
was derived from, 2026-08-06."""


def _ac_section_text(plan_text: str) -> Optional[str]:
    """The plan body slice between its own `## Acceptance Criteria` heading
    and the next `## ` heading (or end of document) -- `None` when the plan
    carries no such heading at all. Mirrors `_reconcile_tracker_section`'s
    own heading-to-next-heading slicing convention above, applied to the
    plan doc instead of the tracker."""
    match = _AC_HEADING_RE.search(plan_text)
    if match is None:
        return None
    start = match.end()
    next_heading = _AC_NEXT_HEADING_RE.search(plan_text, start)
    end = next_heading.start() if next_heading else len(plan_text)
    return plan_text[start:end]


def _parse_ac_table_rows(section_text: str) -> Optional[list[tuple[str, str]]]:
    """Parses a markdown pipe-table's rows out of an `## Acceptance
    Criteria` section slice, returning `[(ac_id, status_cell), ...]` in
    document order, or `None` when no recognizable table is present (no
    pipe-rows at all, fewer than a header+separator+one data row, or a
    header row with no cells to key off of) -- callers treat `None` as "no
    finding", never as an error (C3: degrade quietly, never raise).

    Corpus shapes observed (`docs/plans/*.md`, 2026-08-06 read-through):
    `| # | Criterion | Status |`, `| ID | Criterion | Status |`, with
    either `|---|---|---|` or `| --- | --- | --- |` separator spacing --
    the `Status` column is located BY HEADER NAME (case-insensitive
    substring match), never a fixed column index, since column order and
    count both vary across plans (some carry only `ID`/`Criterion`/
    `Status`, at least one observed 2-column variant with no `#`/`ID`
    column at all). Falls back to the LAST column when no header cell
    mentions "status" -- still the common convention even when unnamed.

    A row whose cell count doesn't match the header's is skipped rather
    than guessed at (malformed row -- C3's degrade-quietly posture applies
    per-row too, not just to the table as a whole)."""
    raw_rows: list[list[str]] = []
    for line in section_text.splitlines():
        match = _AC_TABLE_ROW_RE.match(line)
        if match is None:
            continue
        raw_rows.append([cell.strip() for cell in match.group(1).split("|")])
    if len(raw_rows) < 2:
        return None

    header = raw_rows[0]
    if not header or not any(header):
        return None

    status_idx: Optional[int] = None
    for idx, cell in enumerate(header):
        if "status" in cell.lower():
            status_idx = idx
            break
    if status_idx is None:
        status_idx = len(header) - 1

    body_rows = [
        row for row in raw_rows[1:] if not _AC_TABLE_SEPARATOR_RE.match("|".join(row))
    ]
    if not body_rows:
        return None

    parsed: list[tuple[str, str]] = []
    for row in body_rows:
        if len(row) != len(header) or status_idx >= len(row):
            continue
        ac_id = row[0] if row[0] else "?"
        parsed.append((ac_id, row[status_idx]))
    if not parsed:
        return None
    return parsed


def _ac_status_is_unresolved(status_cell: str) -> bool:
    """True iff an AC table `Status` cell value reads as still-unresolved
    per `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s own narrow, corpus-derived
    vocabulary -- see that constant's docstring for what is and is not
    included, and why.

    Whole-cell strikethrough short-circuits to RESOLVED (Review:
    code-reviewer -- Finding [P2], 2026-08-06) BEFORE the `*_~` delimiter
    strip below runs: stripping first would collapse `~~pending~~` down to
    the bare word `pending`, which the vocabulary check would then flag
    unresolved -- exactly backwards from `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s
    own documented intent that struck-through prose reads as
    author-resolved. See `_AC_STRIKETHROUGH_CELL_RE`'s own docstring."""
    trimmed = status_cell.strip()
    if _AC_STRIKETHROUGH_CELL_RE.match(trimmed):
        return False
    cleaned = trimmed.strip("*_~").strip()
    if cleaned in _AC_UNRESOLVED_CHECKBOX_GLYPHS:
        return True
    return bool(_AC_UNRESOLVED_STATUS_RE.match(cleaned.lower()))


def _ac_table_desync_finding(
    plan_text: str, spine_fully_resolved: bool
) -> Optional[dict[str, Any]]:
    """Advisory-only desync detector (C1/C2 -- eng-director review finding,
    2026-08-06): fires when every commit-required `## Tasks` spine row has
    already reached a terminal, verified disposition (`spine_fully_
    resolved`, computed by the caller from the SAME `shipped`/`fully_
    resolved` verdict this module already derives for its own stamp
    decision -- no second completeness oracle here) while the plan's own
    `## Acceptance Criteria` table still carries at least one row this
    check reads as unresolved.

    Returns `None` -- "no finding" -- whenever `spine_fully_resolved` is
    `False` (the spine itself isn't done; an AC table lagging a plan that
    hasn't shipped is not this check's concern), the plan has no `##
    Acceptance Criteria` heading at all, its table is unparseable/
    malformed/absent (`_parse_ac_table_rows` returning `None`), or every
    row it DID parse reads as resolved. Never raises -- every parse step
    above is a pure string/regex operation over already-in-memory text, but
    the whole computation is wrapped in a broad `except Exception` guard
    anyway (mirrors `compute_open_spine_row_gate`'s own degrade-never-raise
    posture for this exact shape of advisory check -- see
    `coordinator_core/workstream_complete/directives_spine_worklist.py`)
    so a corpus shape this parser did not anticipate can never turn an
    advisory check into a new stamp-path failure mode.

    C2's own load-bearing constraint lives entirely in the CALLER, not
    here: this function only ever produces a finding dict for the result
    payload and message text -- it has no access to, and never touches,
    the stamp decision itself."""
    if not spine_fully_resolved:
        return None
    try:
        section = _ac_section_text(plan_text)
        if section is None:
            return None
        rows = _parse_ac_table_rows(section)
        if rows is None:
            return None
        unresolved_ac_ids = [
            ac_id for ac_id, status_cell in rows if _ac_status_is_unresolved(status_cell)
        ]
        if not unresolved_ac_ids:
            return None
        return {
            "unresolved_ac_ids": unresolved_ac_ids,
            "total_ac_rows": len(rows),
        }
    except Exception:
        return None


def close_out_and_stamp(
    plan_path: str, *, repo_root: Optional[Path] = None, dry_run: bool = False
) -> tuple[int, dict[str, Any]]:
    """Decide full-shipped vs. halted, stamp `status: implemented` on the
    full-shipped path only, then land one scoped commit covering every path
    this ceremony itself changed.

    `dry_run` (2026-08-04 -- see this module's own docstring and
    `coordinator/bin/close-out-and-stamp.py`'s `--dry-run` usage text for the
    incident this closes: a caller with no way to observe this ceremony's
    verdict short of MUTATING had no choice but to run the mutating path
    purely to read it, and did): one computation, two dispositions of the
    SAME result -- every read and decision below (the shipped/halted
    oracle, the implemented-vs-landed-vs-halted status target, the
    fidelity check) runs IDENTICALLY regardless of `dry_run`. The write
    sites this ceremony owns are individually gated on it instead:

      1. The `status:` stamp -- `_stamp_plan_landed` skips its own final
         disk write under `dry_run` (same transition decision either way);
         the `implemented` transition instead invokes the REAL, unmodified
         `archive_stamp.cs_stamp_plan_implemented` against a throwaway
         scratch COPY of this run's current in-memory plan text
         (`_dry_run_scratch_plan`) rather than re-deriving that function's
         own decision matrix a second time -- see that helper's own
         docstring for why composing over the real writer (pointed at a
         copy) is the shared-computation-preserving choice here, not a
         parallel implementation.
      2. The commit leg (`commit_paths`) is not invoked at all under
         `dry_run` -- there is no scratch-copy equivalent for "stage and
         commit into THIS repo's real history", so this leg is skipped
         outright rather than simulated; `commit_result` reports what WOULD
         have been staged instead.

    The returned result dict always carries `"dry_run": bool` (present on
    every return, including `EXIT_BUSINESS_FAIL`) so a caller can never
    mistake a preview for a completed close-out.

    Commit-leg path set (Defect 3 fix, 2026-07-27): this op's ONLY write is
    the plan's own `status:` frontmatter field (via `cs_stamp_plan_implemented`
    on the shipped path; nothing at all on the halted path). The prior
    implementation shelled out to `coordinator-safe-commit` in its
    liveness-auto-detecting default mode with no scope at all, which that
    binary correctly refuses under ordinary multi-session concurrency ("this
    repo's NORMAL state" -- Defect 3 report). The fix runs the commit
    in-process through `commit_paths` with an EXPLICIT pathspec of exactly
    `[plan_path_rel]` -- the one path this function is capable of
    having changed -- never a broad/auto-detected scope, so a peer session's
    concurrently-dirty files on the same branch are never swept in. This is
    intentionally NOT "the plan document itself, plus other changed paths"
    (the docstring language a prior draft of this fix used) -- there ARE no
    other paths this op ever touches, so the plan path alone IS the
    complete, defensible non-empty set.

    Commit leg gated on `wrote_anything` (`stamped or partial_evaluation_
    stamped`), not unconditional (correction during this fix's own test
    pass; corrected AGAIN, review finding, 2026-07-27 -- see below):
    `commit_paths` raises `CommitRefused` on an empty pathspec, but that
    fires ONLY when `stage_paths` resolves to nothing stageable at all -- it
    does NOT detect "the resolved tree is byte-identical to HEAD". If this
    op wrote nothing, but the commit leg still ran anyway, `plan_path_rel`
    would still exist and still resolve to a real blob, and `commit_paths`
    would land a real, spurious commit object over an unchanged tree rather
    than refusing or no-op'ing. `wrote_anything` is this op's own single
    source of truth for "did I change anything"; the commit leg runs ONLY
    when it is `True`, and is skipped entirely (`committed_sha=None`,
    `commit_failed=False`) rather than attempted and caught otherwise.

    `stamped` alone is NOT that source of truth, and must not be read as
    one (review finding, 2026-07-27): `_stamp_plan_landed` and
    `plan_status_transition._stamp_implemented` (via
    `cs_stamp_plan_implemented`) BOTH return `rc == 0` on a documented
    no-op branch (status already terminal; landed's stamp additionally
    no-ops on an already-"landed" status) with NO on-disk write. `stamped`
    is set from `_peek_plan_status`'s PRE-CALL read of the plan's current
    status, cross-referenced against the exact no-op conditions each
    helper documents -- not from the stamp call's bare return code -- so
    it is `True` only when that branch's own call genuinely wrote. This
    keeps a repeated `close_out_and_stamp` call against an
    already-shipped, fully-resolved plan a genuine no-op end to end: no
    stamp write, `wrote_anything` stays `False`, and the commit leg is
    skipped rather than attempted against zero diff.

    Repo-identity gate (C4a, 2026-08-11 -- see
    `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`):
    `coordinator_core.pickup_assemble.compute_repo_identity_gate(root, sid)`
    is called once `root` is resolved. cwd-derived-only: this call refuses
    (existing `EXIT_BUSINESS_FAIL`/`{"error": ...}` vocabulary, carrying the
    gate's own `message`) ONLY on a `MISMATCH` verdict AND only when the
    caller did NOT pass an explicit `repo_root` -- an explicitly-supplied
    root is the caller's own choice and never second-guessed here.
    `UNRESOLVED` never refuses (DR-277: hardening it into a refusal turns a
    fail-open guard into a fleet-wide ceremony outage). Both `MATCH` and
    `UNRESOLVED` (and `MISMATCH` on an explicit-root call, which is not
    refused) are carried informationally on the `EXIT_OK` return as
    `"gates": {"repo_identity": <gate's own returned dict>}`.

    C3 (2026-08-21, "the close ceremony stops paying for the join"): this
    function no longer performs a commit-subject/`Deliverable-Id`-trailer
    join, does not scan sibling repos, and does not auto-resolve a
    committed-but-`open` row (AC8) -- the evidence source that fed that
    inference (a subject-parsed git-log join) is gone. The only remaining
    evidence for a `## Tasks` spine row is its own verified
    `disposition_ref` (`_determine_shipped`); a plan predating the spine
    still resolves off its `## Dispatch Ledger` table's own
    `committed <sha>` cells. Both are pure sha-ancestry checks.

    Returns `(exit_code, result_dict)`:
      - `EXIT_OK` with `{"shipped": bool, "stamped": bool,
        "missing_chunk_ids": [str, ...], "disposition_ref_rejections": {...},
        "commit": {...}, "message": str, "gates": {"repo_identity": {...}}}`
        on success. `disposition_ref_rejections` (2026-08-04
        -- see `_disposition_ref_evidence`'s own docstring) is `{}` whenever
        `missing_chunk_ids` is empty, or every still-missing row's
        `disposition_ref` verified as real evidence; otherwise a
        `{chunk_id: reason}` map, one entry per still-missing chunk-id whose
        row carried a `disposition_ref` that did NOT verify, `reason` being
        one of `DISPOSITION_REF_ABSENT`/`DISPOSITION_REF_MALFORMED`/
        `DISPOSITION_REF_UNRESOLVABLE`/`DISPOSITION_REF_NOT_ANCESTOR` -- the
        specific cause a rejected disposition_ref did not count, present
        alongside `missing_chunk_ids` for the same reason.
        `partial_evaluation_stamped` (2026-08-06, Defect 2 fix -- see
        `_stamp_close_out_partial_evaluation`'s own docstring) is `True`
        whenever this run wrote the plan's `close_out_last_partial:`
        frontmatter field this call (halted verdict, non-empty `missing`);
        `False` on a shipped/landed verdict, or a halted verdict whose
        frontmatter could not be split at all. This is the durable trace
        that lets a LATER reader of the plan tell "this ceremony ran,
        evaluated, and correctly declined to stamp" apart from "nobody has
        ever run this ceremony against this plan".
      - `EXIT_BUSINESS_FAIL` with `{"error": str, ...}` on a resolution
        failure, a malformed spine, a failed stamp, or a failed commit --
        every `EXIT_BUSINESS_FAIL` dict also carries `"dry_run": bool`
        (see the `dry_run` parameter's own docstring section above), even
        the very earliest parse-failure returns, before any of this op's
        own writes were ever attempted.

    Both branches ALWAYS carry `"dry_run": bool` (present on every return),
    per the `dry_run` parameter's own docstring section above.
    """
    # Deferred: `coordinator_core.pickup_assemble` imports `coordinator_core.ops`,
    # whose eager registration walk reaches this module. A module-level import here
    # closes that cycle and drops the cascade ops from the registry.
    from coordinator_core.pickup_assemble import resolve_repo_root
    # C1 extraction: `compute_repo_identity_gate`/`_REPO_IDENTITY_MISMATCH` now
    # live in the lean `repo_identity_gate` module -- importing from there
    # avoids paying for the 10k-line `pickup_assemble` module for this leg too.
    from coordinator_core.repo_identity_gate import (
        _REPO_IDENTITY_MISMATCH,
        compute_repo_identity_gate,
    )

    explicit_repo_root = repo_root is not None
    root = repo_root or resolve_repo_root()
    if root is None:
        return EXIT_BUSINESS_FAIL, {
            "error": "could not resolve a git worktree root",
            "dry_run": dry_run,
        }

    # Repo-identity gate (C4a) -- cwd-derived-only: an explicitly-supplied
    # `repo_root` still gets the informational `gates.repo_identity` entry
    # below, but never a refusal from it (the caller named the root, so a
    # cwd/registry-anchor divergence isn't this call's business). Only the
    # cwd-derived (`repo_root` omitted) path can refuse on MISMATCH.
    # UNRESOLVED never refuses either way (DR-277).
    sid = session_core.resolve_session_id(str(root)) or None
    repo_identity_gate = compute_repo_identity_gate(root, sid)
    if (
        repo_identity_gate["verdict"] == _REPO_IDENTITY_MISMATCH
        and not explicit_repo_root
    ):
        return EXIT_BUSINESS_FAIL, {
            "error": repo_identity_gate["message"],
            "dry_run": dry_run,
        }

    live_path = Path(plan_path) if Path(plan_path).is_absolute() else root / plan_path
    if not live_path.is_file():
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: not found", "dry_run": dry_run}

    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {
            "error": f"{plan_path}: could not read ({exc})",
            "dry_run": dry_run,
        }

    if split_frontmatter(text) is None:
        return EXIT_BUSINESS_FAIL, {
            "error": f"{plan_path}: no parseable frontmatter",
            "dry_run": dry_run,
        }

    # A5 fix: `rel_id` (not `str(...relative_to(...))`) -- `plan_path_rel`
    # is matched below against git-derived paths (`_determine_shipped`),
    # which are ALWAYS forward-slash; `str()` renders `os.sep`, so a
    # Windows session misclassifies shipped/missing chunks at plan
    # close-out.
    try:
        plan_path_rel = rel_id(live_path, root)
    except ValueError:
        plan_path_rel = plan_path

    shipped, missing, evidence_backed, spine_error = _determine_shipped(
        text, plan_path_rel, root
    )
    if spine_error is not None:
        return EXIT_BUSINESS_FAIL, {"error": spine_error, "dry_run": dry_run}

    plan_deliverable_id = _plan_deliverable_id(text)

    # `rows` is re-derived here (rather than threaded out of
    # `_determine_shipped`) so the `open_blocking`/`ac_table_desync`
    # computation below and the disposition_ref-rejection diagnostic both
    # read the SAME parse `_determine_shipped` itself used -- `None` only
    # when the spine is genuinely ABSENT (the Dispatch Ledger fallback
    # path), which is also the only case `_parse_spine_rows` itself
    # degrades to `([], None)` for (see that function's own ABSENT
    # handling); a MALFORMED spine already returned via `spine_error`
    # above, so this second call cannot fail where the first one didn't.
    rows, rows_error = _parse_spine_rows(text, plan_path_rel)
    if rows_error is not None:
        return EXIT_BUSINESS_FAIL, {"error": rows_error, "dry_run": dry_run}

    # Plan-side disposition_ref rejection diagnostic (2026-08-04) -- unhappy
    # path ONLY: names WHY a still-missing row's own `disposition_ref` did
    # not count as evidence (see `_disposition_ref_evidence`'s own
    # docstring). `_determine_shipped` already consulted this same evidence
    # to compute `missing` above, so re-deriving rejections here for the
    # same `rows` is a read-only diagnostic re-scan, never a second verdict.
    disposition_ref_rejections: dict[str, str] = {}
    if missing and rows:
        _verified_ids, all_rejections = _disposition_ref_evidence(rows, root)
        disposition_ref_rejections = {
            chunk_id: reason
            for chunk_id, reason in all_rejections.items()
            if chunk_id in missing
        }

    # AC7: `implemented` requires BOTH the code oracle (`shipped`) and the
    # resolution oracle (no row still `open`) -- `landed` is the
    # intermediate state where code is in but resolution isn't (D9).
    open_blocking = _open_blocking_chunk_ids(rows) if rows else []
    fully_resolved = not open_blocking

    # AC-table/spine desync check (C1/C2 -- eng-director review finding,
    # 2026-08-06, standalone from the ratified succession-edge problem-set):
    # advisory ONLY, computed from the SAME shipped/fully_resolved verdict
    # already derived above -- never a second completeness oracle, and
    # never a new blocking exit (see `_ac_table_desync_finding`'s own
    # docstring for the full C1-C3 rationale).
    commit_required_ids = _commit_required_chunk_ids(rows) if rows else []
    spine_fully_resolved = bool(commit_required_ids) and shipped and fully_resolved
    ac_table_desync = _ac_table_desync_finding(text, spine_fully_resolved)

    if shipped and fully_resolved and evidence_backed:
        status_target: Optional[str] = "implemented"
    elif shipped and evidence_backed:
        status_target = _LANDED_STATUS
    else:
        status_target = None

    # C2 goal-falsifier stamp-decision gate (see this module's own § "The
    # goal-falsifier stamp-decision gate" comment block above
    # `_evaluate_goal_falsifier_gate` for the full design): evaluated ONLY
    # on the branch that would otherwise ship `implemented` -- a plan
    # already halted for spine reasons never reaches this gate at all,
    # since it has no stamp to lose. A refusal downgrades `status_target`
    # from `"implemented"` to `None` (no stamp at all, same as the halted
    # path's own "nothing to write" posture) rather than to `"landed"`,
    # since `landed` names spine-resolution incompleteness specifically,
    # which is not what a goal-observation gap means. `goal_gate` stays
    # `None` (no new result-dict key added at all) whenever the plan never
    # declared a `prime_exit_criterion` -- AC7's own differential-fixture
    # guarantee for the grandfathered corpus.
    goal_gate: Optional[dict[str, Any]] = None
    if status_target == "implemented":
        goal_gate = _evaluate_goal_falsifier_gate(text, root)
        if goal_gate is not None and goal_gate.get("refused"):
            status_target = None

    # Delivery proof for `_reach_post_commit_tail_stub_close` (PM ruling --
    # let a positive, complete delivery proof close the origin stub
    # directly). Built ONLY on the full-shipped path (`status_target ==
    # "implemented"`) -- the only branch this function's own reach call
    # sites run on -- from facts this run already computed: `plan_
    # deliverable_id` (this plan's own frontmatter), `missing` (empty here
    # by construction, since `status_target == "implemented"` requires
    # `fully_resolved`, which requires `not open_blocking` -- `missing` is
    # intersected into `open_blocking` upstream), and the literal
    # `status_target` value itself.
    # `handoff_close_origin_stub._is_complete_delivery_proof` re-checks
    # every one of these conditions independently -- this dict is the
    # claim, not a bypass of that check. `join_provenance` (kept as a
    # literal `"joined"`, C3 2026-08-21 -- the join itself is gone, but
    # this external contract still string-compares against it) is TRUE
    # by construction on this branch: `status_target == "implemented"`
    # already required `evidence_backed` (checked above), so this proof
    # is only ever built once real evidence -- a verified `disposition_
    # ref` or a Dispatch Ledger sha -- backed the stamp.
    delivery_proof: Optional[dict] = (
        {
            "deliverable_id": plan_deliverable_id,
            "join_provenance": "joined",
            "missing_chunk_ids": list(missing),
            "status": status_target,
            "commit_required_chunk_count": len(commit_required_ids),
        }
        if status_target == "implemented"
        else None
    )

    stamped = False
    if status_target == "implemented":
        # Pass the already-resolved ABSOLUTE live_path, not plan_path_rel --
        # cs_stamp_plan_implemented forwards straight to
        # plan_status_transition.main(["stamp-implemented", "--plan", ...]),
        # which resolves --plan against the process cwd (it has no repo-root
        # anchoring of its own -- see that module's os.path.exists/open calls).
        # This op is engine-dispatched with no cwd guarantee, so a repo-relative
        # path silently stamps the wrong file (or fails) whenever cwd != root.
        # live_path is absolute and already verified to exist/be readable
        # above, so this is strictly more correct than the relative string.
        # plan_path_rel is KEPT for the commit-subject strings and error
        # messages below -- those are human-facing display, repo-relative is
        # right there; only the stamp call needed the absolute path.
        pre_stamp_status = _peek_plan_status(text)
        # C1 (2026-08-08): clear `close_out_last_partial:` BEFORE the stamp
        # call, not after -- see `_clear_close_out_partial_marker`'s own
        # docstring for why a post-stamp write-back would revert the
        # `implemented` flip. The clear itself is NOT dry-run-gated -- it
        # folds into `text` unconditionally, identically on both legs; only
        # the LIVE-FILE WRITE below it is gated on `dry_run`. Under
        # `dry_run` the live file is never touched, so the cleared `text` is
        # instead materialized into the (already-existing) throwaway scratch
        # copy below, which picks it up the same way.
        #
        # Review: code-reviewer -- P2 finding, 2026-08-08: this pre-stamp
        # live-file write is not transactional with the stamp call
        # succeeding. `pre_clear_marker_value` captures the marker's raw
        # value (before the clear) so a failed stamp can restore it -- see
        # the `stamp_rc not in (0, 2)` handling below for why this matters:
        # `plan_status_transition._stamp_implemented` can flip `status:` to
        # `implemented` on disk via its own locked_rmw and STILL return a
        # failure rc (a real flip whose own commit attempt fails, or whose
        # subsequent commit-plan-flip resume also fails) -- that makes
        # `status:` terminal on disk with the marker already cleared, both
        # uncommitted. `coordinator_core.workstream_complete` leg A reads a
        # terminal `status:` next to an absent marker as "not-applicable"
        # (verified clean) -- exactly the false-clean read this fix must not
        # produce. Restoring only the marker field (never touching `status:`
        # or anything else `_stamp_implemented` may have written) undoes the
        # part of this hazard this module owns, without reverting a
        # genuinely-landed-but-uncommitted status flip that plan_status_
        # transition's own next-run resume logic (`_stamp_implemented`'s
        # "stranded uncommitted status flip" branch) already knows how to
        # recover.
        pre_clear_split = split_frontmatter(text)
        pre_clear_marker_value = (
            read_fm_field_unquoted(pre_clear_split.fm_text, _CLOSE_OUT_PARTIAL_FIELD)
            if pre_clear_split is not None
            else None
        )
        cleared_text = _clear_close_out_partial_marker(text)
        cleared_live_marker = False
        if cleared_text is not None:
            text = cleared_text
            if not dry_run:
                live_path.write_text(text, encoding="utf-8", newline="\n")
                cleared_live_marker = True
        if dry_run:
            # See `_dry_run_scratch_plan`'s own docstring: composes over the
            # REAL, unmodified `cs_stamp_plan_implemented` (never a second,
            # locally re-derived decision matrix) by pointing it at a
            # throwaway copy of this run's current in-memory `text` -- the
            # live plan file is never opened by this branch at all.
            scratch_path = _dry_run_scratch_plan(text, live_path.suffix)
            try:
                stamp_rc = archive_stamp.cs_stamp_plan_implemented(str(scratch_path))
            finally:
                scratch_path.unlink(missing_ok=True)
        else:
            stamp_rc = archive_stamp.cs_stamp_plan_implemented(str(live_path))
        # rc=2 (C6, docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6
        # Addendum Q4): plan_status_transition.main() now fires the terminal-state
        # cascade after a non-no-op flip and returns 2 when the cascade resolved no
        # downstream artifact -- the plan's own status DID flip to implemented; only
        # the cascade found nothing to advance (e.g. a docs-only plan with no live
        # handoff carrying its deliverable_id). That is not a stamp failure and must
        # not be reported as one -- only rc=1 (a genuine stamp error) is fatal here.
        if stamp_rc not in (0, 2):
            # Review: code-reviewer -- P2 finding, 2026-08-08: restore the
            # marker this branch cleared before the stamp call, since a
            # failed stamp does not undo it. Re-reads the LIVE file (never
            # `text`) because `_stamp_implemented` may itself have written
            # to `live_path` (a real, non-no-op status flip whose own
            # commit attempt then failed) -- restoring `text` verbatim would
            # revert that flip too, which is plan_status_transition's own
            # concern to resume on a later run, not this op's to undo. Only
            # the marker field is touched, and only when it is still absent
            # (a defensive re-check -- see this call's own docstring for why
            # this can legitimately no longer be true, e.g. a later run
            # already resumed and re-cleared it).
            if cleared_live_marker and pre_clear_marker_value is not None:
                try:
                    current_text = live_path.read_text(encoding="utf-8", errors="replace")
                    current_split = split_frontmatter(current_text)
                    if current_split is not None and read_fm_field(
                        current_split.fm_text, _CLOSE_OUT_PARTIAL_FIELD
                    ) is None:
                        restored_fm = insert_fm_field(
                            current_split.fm_text,
                            _CLOSE_OUT_PARTIAL_FIELD,
                            pre_clear_marker_value,
                            after_key="status",
                        )
                        live_path.write_text(
                            rebuild(current_split, restored_fm), encoding="utf-8", newline="\n"
                        )
                except OSError:
                    # Restoring the marker is best-effort recovery on top of
                    # an already-failing stamp -- a second disk error here
                    # must not mask the real failure being reported below.
                    pass
            return EXIT_BUSINESS_FAIL, {
                "error": f"{plan_path_rel}: stamp-plan-implemented failed (rc={stamp_rc})",
                "dry_run": dry_run,
            }
        # cs_stamp_plan_implemented (via plan_status_transition._stamp_implemented)
        # no-ops with rc=0, writing NOTHING, when the plan's status was
        # ALREADY terminal (`_FROZEN_STATUSES`, which includes
        # "implemented" itself) -- `stamped` must reflect the PRE-CALL
        # status, not the bare rc, or an idempotent re-run against an
        # already-implemented plan reads as `stamped=True` with a
        # byte-clean plan.md (review finding, 2026-07-27).
        stamped = pre_stamp_status is not None and pre_stamp_status not in _FROZEN_STATUSES
    elif status_target == _LANDED_STATUS:
        pre_stamp_status = _peek_plan_status(text)
        stamp_rc = _stamp_plan_landed(str(live_path), dry_run=dry_run, plan_text=text)
        if stamp_rc != 0:
            return EXIT_BUSINESS_FAIL, {
                "error": f"{plan_path_rel}: stamp-plan-landed failed (rc={stamp_rc})",
                "dry_run": dry_run,
            }
        # _stamp_plan_landed no-ops with rc=0, writing NOTHING, on BOTH an
        # already-terminal status AND an already-"landed" status -- mirror
        # both no-op conditions here (same review finding as above).
        stamped = (
            pre_stamp_status is not None
            and pre_stamp_status not in _FROZEN_STATUSES
            and pre_stamp_status != _LANDED_STATUS
        )

    # Defect 2 fix (C2, 2026-08-06): the halted path gets its own durable
    # "evaluated and found partial" trace -- see
    # `_stamp_close_out_partial_evaluation`'s own docstring for the defect
    # this closes. Runs AFTER the implemented/landed stamp branches above
    # (status_target is None here whenever it fires, so it never competes
    # with either of them) and BEFORE `wrote_anything`/the commit leg below,
    # so this write is folded into the SAME commit as any AC8 auto-resolve
    # that also fired this run, rather than needing a second commit.
    partial_evaluation_stamped = False
    if status_target is None and missing:
        new_text = _stamp_close_out_partial_evaluation(text, missing)
        if new_text is not None and new_text != text:
            text = new_text
            partial_evaluation_stamped = True
            if not dry_run:
                live_path.write_text(text, encoding="utf-8", newline="\n")

    if status_target == "implemented":
        subject = f"close-out: {plan_path_rel} shipped end-to-end, stamped implemented"
    elif status_target == _LANDED_STATUS:
        subject = (
            f"close-out: {plan_path_rel} code landed, {len(open_blocking)} "
            f"row(s) still open ({', '.join(open_blocking)}) -- stamped landed"
        )
    elif not evidence_backed:
        # `shipped` was True but `evidence_backed` was False (see the
        # stamp-decision gate above) -- distinct wording from the generic
        # still-uncommitted branch below: there is no `missing` list to
        # report at all here, just an unconsulted plan.
        subject = (
            f"close-out: {plan_path_rel} not stamped -- no ## Tasks spine or "
            "## Dispatch Ledger to consult, so no evidence source was ever "
            "read"
        )
    elif goal_gate is not None and goal_gate.get("refused"):
        # C2's third refusal class -- the spine oracle would have shipped
        # `implemented`, but the plan's own goal observation refused (see
        # this module's own § "The goal-falsifier stamp-decision gate").
        subject = (
            f"close-out: {plan_path_rel} not stamped -- prime exit criterion "
            f"goal observation refused ({goal_gate['reason']}): {goal_gate['detail']}"
        )
    else:
        subject = (
            f"close-out: {plan_path_rel} partial -- "
            f"{len(missing)} chunk(s) still uncommitted ({', '.join(missing)})"
        )

    # A commit is owed whenever this op wrote ANYTHING to the plan doc -- a
    # status stamp (implemented/landed) OR a halted-path `close_out_last_
    # partial:` evaluation write. `stamped` alone under-counts the second
    # case; `wrote_anything` is this function's actual single source of
    # truth for "did I change anything" that Defect 3's commit-gating
    # rationale refers to.
    wrote_anything = stamped or partial_evaluation_stamped
    stage_paths = [plan_path_rel]
    origin_stub_result: dict = {"acted": [], "skipped": [], "failed": []}
    if wrote_anything and dry_run:
        # `--dry-run`: a commit/push is owed on the live path, but there is
        # no scratch-copy equivalent for "stage and commit into THIS repo's
        # real history" the way the two stamp writes above have -- so this
        # leg is skipped OUTRIGHT rather than simulated. `commit_result`
        # names what WOULD have been staged, so a caller can tell "nothing
        # to commit" apart from "a commit was owed but suppressed".
        commit_result = {
            "committed_sha": None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [
                f"dry_run: commit suppressed (would stage/commit {stage_paths!r})",
            ],
        }
    elif wrote_anything and _stage_paths_committed_already(root, stage_paths):
        # DR-272 interaction (see `_stage_paths_committed_already`'s own
        # docstring): the stamp write (and, when it fired, AC8's
        # auto-resolve write, bundled into the SAME on-disk state the stamp
        # step re-reads before committing) already landed in a commit made
        # by `plan_status_transition._commit_plan_flip` under its own name --
        # there is nothing left on `stage_paths` for this op's own commit
        # leg to stage. Report the ALREADY-LANDED HEAD sha as this op's own
        # `committed_sha` (a real, resolvable commit this run caused,
        # whichever op's name is on it) rather than attempting a redundant
        # `git commit` that git would correctly refuse with a bare
        # "nothing to commit".
        head_result = git_native.rev_parse_head(root)
        commit_result = {
            "committed_sha": head_result.stdout.strip() if head_result.ok else None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [
                "already committed by the stamp/auto-resolve write's own "
                "committing op (plan-status-transition, DR-272) -- no "
                "separate commit needed",
            ],
        }
        if commit_result["committed_sha"]:
            # `delivery_proof` (PM ruling) lets a complete, stub-specific
            # proof close the origin stub WITHOUT consulting the
            # live-children guard: this close is IN PLACE (deployment_state
            # -> shipped, no `git mv`), so it cannot strand a dependent the
            # way an archival move could -- archival remains separately
            # gated on liveness in `archive_handoffs.py`, untouched here.
            origin_stub_result = _reach_post_commit_tail_stub_close(
                root, plan_path_rel, commit_result["committed_sha"], delivery_proof
            )
    elif wrote_anything:
        # Explicit, non-empty stage_paths -- see this function's docstring
        # "Commit-leg path set" section: the plan doc is the ONLY path this
        # op ever changes, so it is also the complete, defensible pathspec.
        # Never a broad/auto-detected scope -- that is precisely the
        # blanket-add hazard the (now-bypassed) coordinator-safe-commit
        # liveness gate existed to prevent, and reintroducing it here would
        # defeat this fix.
        # C3 (docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-
        # pipeline-can-go.md): repointed off the killed `run_commit_pipeline`
        # onto the sanctioned zero-spawn commit shape, `coordinator_core.git.
        # commit.commit_paths` (`ceremony.commit_v2`'s own in-process call,
        # mirrored here rather than round-tripped through the op registry --
        # this module already runs in-process). No push leg at all any more:
        # this call site never owned a synchronous push (DR-329 § 7 --
        # publication is deferred to whichever cadence checkpoint runs next
        # and calls `push_outstanding()` itself), so there is nothing here to
        # preserve a `push_mode` for.
        message = compose_message(subject=subject)
        try:
            outcome = commit_paths(
                root,
                stage_paths,
                message,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
            )
        except (CommitRefused, FilterUnsupported) as exc:
            commit_result = {
                "committed_sha": None,
                "pushed": None,
                "push_status": PUSH_STATUS_NOT_ATTEMPTED,
                "pushed_range": None,
                "pushed_count": None,
                "commit_failed": True,
                "sha_unverified": False,
                "diagnostics": [str(exc)],
            }
            return EXIT_BUSINESS_FAIL, {
                "error": f"close-out commit failed: {exc}",
                "shipped": shipped,
                "stamped": stamped,
                "partial_evaluation_stamped": partial_evaluation_stamped,
                "missing_chunk_ids": missing,
                "disposition_ref_rejections": disposition_ref_rejections,
                "open_chunk_ids": open_blocking,
                "ac_table_desync": ac_table_desync,
                "commit": commit_result,
                "dry_run": dry_run,
            }
        commit_result = {
            "committed_sha": outcome.sha,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "sha_unverified": False,
            "diagnostics": [],
        }
        # Reach `post_commit_tail`'s stub-close leg (AC4) -- see
        # `_reach_post_commit_tail_stub_close`'s own docstring.
        # `delivery_proof` (PM ruling) lets a complete, stub-specific
        # proof close the origin stub WITHOUT consulting the
        # live-children guard: this close is IN PLACE (deployment_state
        # -> shipped, no `git mv`), so it cannot strand a dependent the
        # way an archival move could -- archival remains separately
        # gated on liveness in `archive_handoffs.py`, untouched here.
        origin_stub_result = _reach_post_commit_tail_stub_close(
            root, plan_path_rel, outcome.sha, delivery_proof
        )
    else:
        # Nothing of this op's own to commit -- skipped entirely rather
        # than attempted-and-caught (see "wrote_anything" above; this is
        # the "no code landed AND nothing auto-resolved" case).
        commit_result = {
            "committed_sha": None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [],
        }

    if status_target == "implemented":
        message = f"{plan_path_rel}: full plan shipped, stamped implemented, committed"
    elif status_target == _LANDED_STATUS:
        message = (
            f"{plan_path_rel}: code landed, {len(open_blocking)} row(s) still open, "
            "stamped landed, committed"
        )
    elif not evidence_backed:
        message = (
            f"{plan_path_rel}: not stamped -- no ## Tasks spine or "
            "## Dispatch Ledger to consult, so no evidence source was ever read"
        )
    elif goal_gate is not None and goal_gate.get("refused"):
        # C2's third refusal class (AC4-AC6, AC18): the ONE next move,
        # `_GOAL_REFUSAL_NEXT_MOVE`'s own register -- lead with what to do,
        # not a generic refusal.
        # Arm 0 refuses on a field that was never written, so it takes its own
        # next move -- `_GOAL_REFUSAL_NEXT_MOVE` would tell the reader to
        # re-run an observation this plan never named.
        _next_move = _GOAL_REFUSAL_NEXT_MOVE
        if goal_gate["reason"] == GOAL_REFUSAL_PRIME_ABSENT:
            _next_move = _PRIME_ABSENT_NEXT_MOVE
        elif goal_gate["reason"] == GOAL_REFUSAL_FALSIFIER_ABSENT:
            _next_move = _FALSIFIER_ABSENT_NEXT_MOVE
        message = (
            f"{plan_path_rel}: not stamped -- prime exit criterion goal "
            f"observation refused ({goal_gate['reason']}): {goal_gate['detail']}. "
            f"{_next_move}"
        )
    else:
        message = (
            f"{plan_path_rel}: {len(missing)} chunk(s) still uncommitted, "
            "committed partial state -- if delivered, resolve the row's own "
            "disposition_ref to the covering commit sha (`plan-tasks-resolve "
            "--coded <sha>` per row, verified against HEAD ancestry), then "
            "re-run close-out-and-stamp"
        )
        if disposition_ref_rejections:
            # Points at a SPECIFIC still-missing row's own disposition_ref
            # and why it did not count (see `_disposition_ref_evidence`'s
            # own docstring) -- additive context, never a replacement for
            # the base message above.
            rejection_notes = ", ".join(
                f"{chunk_id} ({reason})"
                for chunk_id, reason in sorted(disposition_ref_rejections.items())
            )
            message += (
                f" -- NOTE: disposition_ref did not count as evidence for: "
                f"{rejection_notes}."
            )

    if ac_table_desync:
        # Advisory NOTE only (C2 -- this must never gate the stamp decision
        # above, which has already run by this point) -- same additive-
        # suffix posture as the disposition_ref_rejections NOTE block
        # above, appended regardless of which status_target branch produced
        # `message`.
        message += (
            " -- ADVISORY: the '## Tasks' spine is fully resolved but the "
            f"plan's own '## Acceptance Criteria' table still has "
            f"{len(ac_table_desync['unresolved_ac_ids'])} of "
            f"{ac_table_desync['total_ac_rows']} row(s) reading unresolved "
            f"({', '.join(ac_table_desync['unresolved_ac_ids'])}) -- the AC "
            "table may simply be stale; not blocking the stamp."
        )

    if dry_run:
        # Additive suffix only -- every branch above still describes what
        # WOULD happen (subject/message wording talks about "stamped"/
        # "committed" unconditionally, since that prose is shared with the
        # live path -- see this function's own docstring "one computation,
        # two dispositions"); this is the one place a reader is told, in
        # the human-facing `message` itself, that nothing was actually
        # written.
        message += " [dry-run: no write/commit performed]"

    result: dict[str, Any] = {
        "shipped": shipped,
        "stamped": stamped,
        "status_target": status_target,
        "partial_evaluation_stamped": partial_evaluation_stamped,
        "missing_chunk_ids": missing,
        "disposition_ref_rejections": disposition_ref_rejections,
        "open_chunk_ids": open_blocking,
        "ac_table_desync": ac_table_desync,
        "commit": commit_result,
        "message": message,
        "dry_run": dry_run,
        "origin_stub_close": origin_stub_result,
        "gates": {"repo_identity": repo_identity_gate},
    }
    # `goal_gate` is added ONLY when the C2 gate was actually consulted at
    # all (`goal_gate is not None` -- a plan that never declared a
    # `prime_exit_criterion` never reaches `_evaluate_goal_falsifier_gate`
    # in the first place, see the stamp-decision-gate call site above) --
    # AC7's own differential-fixture guarantee depends on this key being
    # ABSENT, not merely `None`-valued, for the grandfathered corpus.
    if goal_gate is not None:
        result["goal_gate"] = goal_gate
    return EXIT_OK, result


def main(argv: list[str]) -> int:
    """`close-out-and-stamp <plan-path> [--dry-run]`

    `--dry-run` computes and returns the full close-out verdict while
    writing NOTHING (no frontmatter stamp, no plan-body disposition
    backfill, no commit, no push) -- see `close_out_and_stamp`'s own
    docstring for the shared-computation design this implements, and this
    module's header-comment docstring in `coordinator/bin/
    close-out-and-stamp` for the incident that motivated it. The result
    dict always carries `"dry_run": bool` so a caller cannot mistake a
    preview for a completed close-out.

    Still strictly positional otherwise, and still errors on any argument
    beyond the one required `<plan-path>` plus the one optional
    `--dry-run` flag -- extending, not loosening, the pre-existing
    "extra arguments are a usage error" contract."""
    import json

    if not argv or argv[0] in ("--help", "-h"):
        print(
            "usage: close-out-and-stamp <plan-path> [--dry-run]",
            file=sys.stderr if argv else sys.stdout,
        )
        return EXIT_OK if argv and argv[0] in ("--help", "-h") else EXIT_USAGE

    plan_path: Optional[str] = None
    dry_run = False
    extra: list[str] = []
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        elif plan_path is None and not arg.startswith("--"):
            plan_path = arg
        else:
            extra.append(arg)

    if extra:
        print(f"close-out-and-stamp: unrecognized argument(s): {extra!r}", file=sys.stderr)
        return EXIT_USAGE
    if plan_path is None:
        print("close-out-and-stamp: missing required <plan-path>", file=sys.stderr)
        return EXIT_USAGE

    exit_code, result = close_out_and_stamp(plan_path, dry_run=dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
