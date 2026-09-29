"""
coordinator_core.completion_record_integrity — THE single hollow-completion-
record predicate.

Purpose: a completion record under ``archive/completed/<YYYY-MM>/*.md`` can
sit forever looking like proof a workstream shipped while actually carrying
none of the three signals that make it trustworthy -- an unfilled template
placeholder, an empty ``commits:`` list, and a governing plan that never
reached a landed status. Nothing refused that combination
(state/audits/2026-09-28 incident: ``archive/completed/2026-09/
2026-09-20-2026-09-18-doe-holds-no-scripts-188007.md`` finalized with all
three failing at once, its governing plan still ``status: executing``).

``hollow_reasons`` is the ONE predicate every writer that finalizes a
completion record calls -- never re-implemented per-caller. It reads
placeholder markers off the SAME literal tokens the two live scaffolders
emit (``coordinator_core.ops.coordinator_complete_entry`` and
``coordinator_core.ops.ceremony.completion_entry``), not a guessed pattern.

Negative-spec:
    - Read-only. Never mutates the record, the governing plan, or anything
      else on disk.
    - Does NOT decide when a record is ALLOWED to be hollow (an in-flight
      WIP scaffold legitimately fails every check while its workstream is
      still open) -- that policy call belongs to each caller (a writer
      finalizing a record vs. the read-only sweep enumerating every record
      unconditionally). This module only answers "which checks fail right
      now", never "should this write be allowed".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter

#: The exact placeholder title `coordinator_complete_entry._write_entry` seeds
#: a fresh scaffold with -- shared with that module rather than duplicated
#: (see this module's docstring).
PLACEHOLDER_TITLE = "PLACEHOLDER — replace with past-tense workstream title"

#: The exact placeholder prose comment both live scaffolders leave in the body
#: until an EM replaces it. `coordinator_complete_entry.py` and
#: `ops/ceremony/completion_entry.py` word this sentinel differently on
#: purpose (two independent scaffolders, two eras) -- match EITHER literal
#: marker, not a paraphrase.
PROSE_PLACEHOLDER_MARKERS = (
    "<!-- PROSE: Replace this with a ≤8-sentence past-tense description of "
    "what shipped and why it matters. -->",
    "<!-- ONE paragraph (",
)

#: `coordinator_complete_entry.py`'s residue marker for an unset `nature:`.
NATURE_INFER_MARKER = "<!-- NATURE-INFER"

#: Plan-doc statuses this predicate accepts as "governing plan landed" --
#: the brief's own wording ("implemented/landed"), not the wider status
#: vocabulary a plan doc may carry at other lifecycle points.
_LANDED_PLAN_STATUSES = frozenset({"implemented", "landed"})

#: Directories a completed plan doc is moved INTO by a plan archiver,
#: relative to repo_root, searched (in this order) when `docs/plans/
#: <chain_slug>.md` is absent -- a plan that has shipped is routinely moved
#: OUT of docs/plans/, so "not found there" must not collapse into "not
#: landed" (see REASON_PLAN_UNRESOLVABLE). `archive/specs/` is
#: `fleet.archive_completed_plans`'s own destination
#: (`coordinator_core/ops/fleet/archive_plans.py`) and is common across the
#: fleet (every repo swept 2026-09-28 that has an archive/ tree at all has
#: an archive/specs/ subtree); `archive/plans/` and `archive/completed/
#: plans/` are alternate layouts observed in DoE-claude/example-sim-repo-md. The
#: archiver preserves the plan's original filename verbatim, so the lookup
#: is a filename match (`<chain_slug>.md`), not a content scan.
_ARCHIVED_PLAN_DIRS = ("archive/specs", "archive/plans", "archive/completed/plans")

REASON_PLACEHOLDER = "placeholder-marker"
REASON_EMPTY_COMMITS = "empty-commits"
REASON_PLAN_NOT_LANDED = "plan-not-landed"
#: The record names a `chain:` slug, but no plan doc for it was found at
#: `docs/plans/<slug>.md` NOR anywhere under `_ARCHIVED_PLAN_DIRS`. Distinct
#: from REASON_PLAN_NOT_LANDED: this is "cannot verify" (often a pre-
#: `--governing-plan-slug`-convention free-text chain label that was never a
#: real plan filename), not "verified and still open".
REASON_PLAN_UNRESOLVABLE = "plan-unresolvable"

_STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


class GoverningPlanInfo(NamedTuple):
    """What `hollow_reasons` found (or failed to find) for the record's
    governing plan -- surfaced so a caller (the sweep) can report it without
    re-deriving the same chain->plan-path->status walk a second time."""

    chain_slug: Optional[str]
    plan_path: Optional[str]
    plan_status: Optional[str]
    #: True iff SOME plan file was located (live or archived) and its
    #: `status:` was read -- False means "no plan file found anywhere
    #: searched" (REASON_PLAN_UNRESOLVABLE), as opposed to "found, but its
    #: status isn't landed" (REASON_PLAN_NOT_LANDED).
    resolved: bool = False


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _has_placeholder_markers(fm: dict, body: str) -> bool:
    title = fm.get("title")
    if title is not None and str(title) == PLACEHOLDER_TITLE:
        return True
    if any(marker in body for marker in PROSE_PLACEHOLDER_MARKERS):
        return True
    if NATURE_INFER_MARKER in body:
        return True
    return False


def _has_empty_commits(fm: dict) -> bool:
    commits = fm.get("commits")
    return not commits


def _find_archived_plan(repo_root: Path, chain_slug: str) -> Optional[Path]:
    """Filename-match `<chain_slug>.md` under each `_ARCHIVED_PLAN_DIRS`
    root, first hit wins (dir order, then sorted path within a dir --
    deterministic). A plan archiver preserves the original filename, so no
    content scan is needed."""
    target_name = f"{chain_slug}.md"
    for rel_dir in _ARCHIVED_PLAN_DIRS:
        root = repo_root / rel_dir
        if not root.is_dir():
            continue
        matches = sorted(root.rglob(target_name))
        if matches:
            return matches[0]
    return None


def _governing_plan_status(repo_root: Path, fm: dict) -> GoverningPlanInfo:
    chain = fm.get("chain")
    chain_slug = str(chain) if chain else None
    if not chain_slug:
        return GoverningPlanInfo(chain_slug=None, plan_path=None, plan_status=None, resolved=False)

    live_path = repo_root / "docs" / "plans" / f"{chain_slug}.md"
    text = _read_text(live_path)
    plan_path = live_path

    if text is None:
        archived_path = _find_archived_plan(repo_root, chain_slug)
        if archived_path is not None:
            plan_path = archived_path
            text = _read_text(archived_path)

    if text is None:
        # No plan file found anywhere searched -- unresolvable, not
        # "not landed" (see REASON_PLAN_UNRESOLVABLE docstring).
        return GoverningPlanInfo(chain_slug=chain_slug, plan_path=None, plan_status=None, resolved=False)

    m = _STATUS_RE.search(text)
    status = m.group(1).strip() if m else None
    return GoverningPlanInfo(chain_slug=chain_slug, plan_path=str(plan_path), plan_status=status, resolved=True)


def hollow_reasons_for_fields(fm: dict, body: str, repo_root: Path) -> List[str]:
    """THE core predicate, callable against an in-memory `(fm, body)` pair --
    used by a writer that wants to check the content it is ABOUT TO WRITE
    before it lands on disk (never write-then-inspect-then-unwrite). See
    `hollow_reasons` for the on-disk-file wrapper.

    Returns a subset of `[REASON_PLACEHOLDER, REASON_EMPTY_COMMITS,
    REASON_PLAN_NOT_LANDED, REASON_PLAN_UNRESOLVABLE]`, in that fixed
    order (the last two are mutually exclusive -- at most one of them ever
    appears). `[]` means the record passes every check.

    A `chain:` slug resolves against `docs/plans/<slug>.md` first, then
    (that missing) a filename match under `_ARCHIVED_PLAN_DIRS` -- a
    landed plan is routinely MOVED out of docs/plans/, and "not found
    there" must not collapse into "not landed". `REASON_PLAN_NOT_LANDED`
    fires when a plan file WAS resolved (live or archived) and its
    `status:` is anything other than `implemented`/`landed`.
    `REASON_PLAN_UNRESOLVABLE` fires when *fm* names a `chain:` slug but no
    plan file was found anywhere searched (often a pre-`--governing-plan-
    slug`-convention free-text chain label that was never a real plan
    filename). A record with NO `chain:` (a standalone/adhoc entry) has no
    governing plan to check and never fails either leg.
    """
    reasons: List[str] = []
    if _has_placeholder_markers(fm, body):
        reasons.append(REASON_PLACEHOLDER)
    if _has_empty_commits(fm):
        reasons.append(REASON_EMPTY_COMMITS)

    info = _governing_plan_status(repo_root, fm)
    if info.chain_slug:
        if not info.resolved:
            reasons.append(REASON_PLAN_UNRESOLVABLE)
        elif info.plan_status not in _LANDED_PLAN_STATUSES:
            reasons.append(REASON_PLAN_NOT_LANDED)

    return reasons


_UNREADABLE_RECORD_REASONS = [REASON_PLACEHOLDER, REASON_EMPTY_COMMITS, REASON_PLAN_UNRESOLVABLE]


def hollow_reasons(record_path: Path, repo_root: Path) -> List[str]:
    """Every hollow-completion-record check that fails for the ON-DISK
    record at *record_path*. Thin wrapper around `hollow_reasons_for_fields`
    -- read, parse, delegate. An unreadable/unparseable record degrades to
    `_UNREADABLE_RECORD_REASONS` (fails everything checkable -- a record
    this module cannot even read has no `chain:` it could resolve, so
    REASON_PLAN_UNRESOLVABLE, not REASON_PLAN_NOT_LANDED, is the honest
    third reason here).
    """
    text = _read_text(record_path)
    if text is None:
        return list(_UNREADABLE_RECORD_REASONS)

    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    body = parsed.get("body") or ""
    if not fm:
        return list(_UNREADABLE_RECORD_REASONS)

    return hollow_reasons_for_fields(fm, body, repo_root)


def governing_plan_info(record_path: Path, repo_root: Path) -> GoverningPlanInfo:
    """Read-only view of the record's governing plan (chain slug, path,
    status) -- the same walk `hollow_reasons` performs internally, exposed
    for a caller (the sweep) that needs to report it without re-parsing."""
    text = _read_text(record_path)
    if text is None:
        return GoverningPlanInfo(chain_slug=None, plan_path=None, plan_status=None)
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    return _governing_plan_status(repo_root, fm)


class HollowCompletionRecordError(Exception):
    """Raised by a finalizing writer when `hollow_reasons` finds ANY check
    failing for a record the writer is about to leave as its LAST word on
    that record (see each writer's own call site for exactly when that
    is). A finalized record that still has a placeholder, empty commits, OR
    an unresolved/unlanded governing plan is defective by itself -- the
    finalize gate does not wait for all three to fail at once. (An
    all-three-scoped gate was this module's first cut; a fresh scaffold is
    never at a finalize point in the first place, so the wider ANY-of-N bar
    costs nothing there and catches a single-axis defect at finalize that
    the narrower gate would have missed.)"""


def _raise_if_any_failed(reasons: List[str], label: str) -> None:
    if reasons:
        raise HollowCompletionRecordError(
            f"{label}: refusing to finalize a hollow completion record -- "
            f"failed: {', '.join(reasons)}"
        )


def assert_finalize_ready(record_path: Path, repo_root: Path) -> None:
    """Refuse iff ANY check in `hollow_reasons` fails -- a finalized record
    must pass every one. The read-only sweep
    (`coordinator_core.ops.completion_record_sweep`) reports the same
    failures unconditionally, including for records never routed through a
    finalize call site at all.

    Raises `HollowCompletionRecordError` naming which checks failed.
    """
    _raise_if_any_failed(hollow_reasons(record_path, repo_root), str(record_path))


def assert_fields_finalize_ready(fm: dict, body: str, repo_root: Path, *, label: str) -> None:
    """Same refusal as `assert_finalize_ready`, against in-memory
    `(fm, body)` -- for a writer checking content it is about to write,
    before any bytes land on disk. *label* is the identifier used in the
    raised error (typically the target path, which may not exist yet)."""
    _raise_if_any_failed(hollow_reasons_for_fields(fm, body, repo_root), label)
