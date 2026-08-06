"""
coordinator_core.ops.fleet.archive_plans — fleet.archive_completed_plans op.

Purpose: git-mv terminal plans (status ∈ {implemented, superseded, abandoned})
from docs/plans/*.md into archive/specs/YYYY-MM/ under the confirm→act
(dry_run:true / dry_run:false) wire contract.  Registers as
"fleet.archive_completed_plans" via @register_op.

Terminality predicate (contract §2.2): frontmatter status ∈
{implemented, superseded, abandoned}.

Live-reference skip guard (Key Decision 4,
docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md §4):
A terminal plan whose filename is still cited in the body of any live handoff
or live plan is NOT archived — classified as skipped reason:"live-reference".
This guard is an explicitly-named ADDITIONAL skip class, distinct from the
T1→T3 terminality-drift skip class (contract §2.1 :206).
Applied at BOTH T1 (preview) and T3 (act).  T1-filtering removes live-referenced
plans from the human preview entirely (safe-default behavioral divergence from the
contract's "human is the guard" reading — documented in plan Key Decision 4).

cannot-derive-date skip guard:
A terminal plan whose filename carries no YYYY-MM-DD prefix has no archive
destination (YYYY-MM is derived from the filename prefix). Such a plan is
classified as skipped reason:"cannot-derive-date" — NOT failed. Hard-failing it
would force exit_code=2 on every session-init sweep fleet-wide for a benign,
un-actionable filename. Applied at BOTH T1 (filtered from preview) and T3
(skip-guarded), mirroring the live-reference skip class. Logged at WARNING so the
misnamed plan is noticed and renamed rather than silently skipped forever.

Dirty-tree skip guard (Zone-A):
A terminal plan whose repo-relative path currently shows uncommitted
working-tree/index changes (``git status --porcelain -- <path>`` non-empty) is
skipped reason:"dirty-tree: ..." — a git-mv would silently clobber in-flight
edits (e.g. a concurrent review-integration apply mid-write). Applied at BOTH
T1 (filtered from preview) and T3 (skip-guarded), mirroring the live-reference
skip class. Best-effort: a git-status subprocess failure degrades to "not
dirty" — this guard is NOT the load-bearing fail-closed property (the
claim-liveness guard below is). Incident: 2026-07-11 — a plan was git-mv'd
while it held uncommitted review-integration edits, survived only by luck of
git-mv working-tree-copy semantics.

Claim-liveness skip guard (Zone-A):
A terminal plan whose plan-execution claim (``coordinator_core.session.claims.
claim_plan`` — acquired at /execute-plan Phase 1.5 and /workstream-complete,
released only at the two clean terminals) is still held by a LIVE session is
skipped reason:"live-claim: ...". A plan can be ``status: implemented``
(accurate — the code shipped) while its workstream is still being closed out
in /workstream-complete's B-wave review — the claim is precisely that
"still in flight" signal. Applied at BOTH T1 and T3.
⚠ LOAD-BEARING SAFETY PROPERTY: this guard FAILS CLOSED on an
ambiguous/unavailable liveness read — ANY exception while evaluating
``coordinator_core.session.liveness.claim_holder_live`` (including the
ValueError raised on an empty/invalid claim_dir) is treated as "live, defer",
NEVER as "not live, archive" (the 2026-07-11 incident this guard exists to
prevent). A missing liveness predicate, or one that errors, must never read
as an all-clear on a path that git-mv's the repo.

Unreconciled-AC skip guard:
A terminal plan whose ``## Acceptance Criteria`` table contains one or more rows
left bare pending — no disposition recorded at all — is skipped reason:
"unreconciled-ac: N pending row(s)". This is NOT a revival of AC-as-execution-
gate (`docs/plans/2026-06-30-retire-acceptance-oracle.md` retired that fleet-
wide and that decision stands): the guard does not require ACs to be *done*,
only *reconciled* — every row must carry SOME terminal disposition (done,
shipped, shipped-differently, abandoned, spun-off, backlogged, deferred,
superseded, discharged, removed/struck-through, or any other non-bare status
text). A plan every one of whose ACs is marked "abandoned" archives cleanly —
that is a correctly reconciled plan. Only a row whose Status cell is empty or
one of the bare-pending tokens (``☐``, ``[ ]``, ``pending``, ``open``, ``todo``,
``tbd``, ``○`` — case-insensitive, after stripping emphasis/strikethrough
markup) with no qualifying text counts as unreconciled — a row like
"pending (external)" carries an explicit annotation and is NOT counted. A row
whose leading (ID) cell is itself struck through (``~~AC-3~~``) is treated as
removed and never counted. Best-effort, NOT fail-closed (mirrors the
dirty-tree guard, not the claim-liveness guard): a file-read failure or an
AC table with no identifiable "Status" column degrades to 0 (generous —
ambiguous reads treat as reconciled) rather than blocking archival; a false
skip (refusing to archive a fine plan) is a mild annoyance, over-strictness
gets the guard disabled. A plan with no ``## Acceptance Criteria`` section at
all is unaffected (AC tables are optional prose, per
`docs/wiki/plan-coverage-checker.md`). Applied at BOTH T1 (filtered from
preview) and T3 (skip-guarded), mirroring every other skip class above.

Live handoff definition: status NOT in {claimed, superseded} (dual-tolerant read;
  old-vocabulary "consumed" still recognized as a fallback — DR-084).
Live plan definition: status NOT in {implemented, superseded, abandoned},
  excluding review sidecars (<stem>.<tag>.md — they cite their parent filename
  and would otherwise hold every plan-with-sidecar in place indefinitely).

Sidecar co-move: any file matching <stem>.*.md alongside the primary plan in
docs/plans/ is moved alongside it.  Sidecar results are not reported in the
wire output (supplementary files); if a sidecar move fails, a warning is logged
and the primary plan is still reported as acted.

Archive destination: archive/specs/YYYY-MM/ (YYYY-MM from leading date in
filename, e.g. 2026-07-04-foo.md → 2026-07).

Spec backlinks:
  - Plan (C2):     docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md
  - Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md §2.2
  - DR-211:        docs/decisions/DR-211-fleet-op-substrate-write-boundary.md

Negative-spec:
  - Does NOT use git add -A or git add . — scoped exact-pathspec only (DR-211 D3 Invariant 4).
  - Does NOT use blocking subprocess.run — all git calls via asyncio.create_subprocess_exec
    (DR-211 D4 async mandate).
  - Does NOT treat params.repo_root as the worktree source — uses
    main_worktree_root(common_dir) (Key Decision 5).
  - Does NOT add an HTTP route — MUTATING ops are UDS-only (DR-211 five-bound (v)).
  - Does NOT expand the frozen wire envelope with a reason/error top-level field on errors.
"""

from __future__ import annotations
import sys

import glob as _glob
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    Move,
    _make_git_env,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_status,
    rel_id,
    plan_claim_dir,
    validate_params,
)
from coordinator_core.session import liveness as _session_liveness

_LOG = logging.getLogger(__name__)

# Terminality predicate (contract §2.2).
# PLAN-axis terminal statuses, SSOT'd; the DR-084 handoff rename never touches this export.
from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS

_TERMINAL_STATUSES: frozenset = PLAN_TERMINAL_STATUS

# Handoff statuses that mean NOT live (claimed/superseded = retired from active work).
# Dual-tolerant read (DR-084): "consumed" is the pre-rename value, kept as a fallback
# so records still carrying the old status word are still recognized as retired.
_RETIRED_HANDOFF_STATUSES: frozenset = frozenset({"claimed", "consumed", "superseded"})

# Regex to extract YYYY-MM from a filename prefix (e.g. 2026-07-04-foo.md → "2026-07").
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")

# Marker prefix for sidecar Move.candidate_id — filtered from wire output.
# Format: _SIDECAR_PREFIX + primary_candidate_id + ":" + sidecar_filename
_SIDECAR_PREFIX = "__sidecar__"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_sidecar(path: Path) -> bool:
    """Return True if path is a review sidecar (<plan-stem>.<tag>.md).

    Sidecar filenames have a dot in the stem after stripping .md:
      - sidecar:  2026-07-04-my-plan.prior-art-check.md → stem contains a dot
      - primary:  2026-07-04-my-plan.md → stem has no dot
    """
    return "." in path.stem


def _derive_yyyy_mm(fname: str) -> Optional[str]:
    """Derive YYYY-MM from a plan filename prefix.

    E.g. "2026-07-04-foo.md" → "2026-07".
    Returns None when the filename carries no YYYY-MM-DD prefix (ungated skip).
    """
    m = _DATE_PREFIX_RE.match(fname)
    return m.group(1) if m else None


def _extract_title(path: Path) -> Optional[str]:
    """Return the 'title' field from YAML frontmatter, or None if absent/unreadable."""
    meta = _read_meta(str(path))
    return meta.get("title") if meta else None


def _collect_live_reference_text(worktree_root: Path) -> Tuple[str, bool]:
    """Return (concatenated body text of all live handoffs and live plans, scan_incomplete).

    A terminal plan whose filename appears in this text is live-referenced and
    must NOT be archived (Key Decision 4 live-reference skip guard).

    Live handoff: status NOT in {claimed, superseded} (dual-tolerant; old
      "consumed" value still recognized as a fallback — DR-084).
    Live plan:    status NOT in terminal set; sidecars excluded.

    scan_incomplete=True when either state/handoffs/ or docs/plans/ could not
    be fully enumerated (e.g. permission-denied) — uses iterdir(), NOT
    glob("*.md"): Path.glob()'s selector silently swallows PermissionError
    while walking (unreadable dir -> glob() yields an empty iterator, no
    exception; mirrors roadmap_dag.py's identical fix). A truncated corpus
    reads a terminal plan cited only in the unreadable subtree as "not
    live-referenced" — exactly the failure mode Key Decision 4 exists to
    prevent. Callers MUST fail closed on scan_incomplete: skip archival
    rather than trust a partial live-reference text.
    """
    parts: List[str] = []
    scan_incomplete = False

    # Live handoffs (state/handoffs/*.md).
    handoffs_dir = worktree_root / "state" / "handoffs"
    if handoffs_dir.is_dir():
        try:
            handoff_entries = sorted(handoffs_dir.iterdir())
        except OSError as exc:
            _LOG.warning(
                "archive_plans: cannot scan %s for live-reference text — %s; "
                "marking scan incomplete (fail-closed)", handoffs_dir, exc,
            )
            scan_incomplete = True
            handoff_entries = []
        for hpath in handoff_entries:
            if hpath.suffix != ".md" or not hpath.is_file():
                continue
            status = parse_frontmatter_status(hpath)
            if status in _RETIRED_HANDOFF_STATUSES:
                continue  # handoff is claimed/retired — not live
            try:
                parts.append(hpath.read_text(errors="replace"))
            except OSError as exc:
                _LOG.debug("archive_plans: could not read handoff %s: %s", hpath, exc)

    # Live plans (docs/plans/*.md, excluding sidecars and terminal plans).
    plans_dir = worktree_root / "docs" / "plans"
    if plans_dir.is_dir():
        try:
            plan_entries = sorted(plans_dir.iterdir())
        except OSError as exc:
            _LOG.warning(
                "archive_plans: cannot scan %s for live-reference text — %s; "
                "marking scan incomplete (fail-closed)", plans_dir, exc,
            )
            scan_incomplete = True
            plan_entries = []
        for lpath in plan_entries:
            if lpath.suffix != ".md" or not lpath.is_file():
                continue
            if _is_sidecar(lpath):
                continue  # sidecar — NOT a live plan
            status = parse_frontmatter_status(lpath)
            if status in _TERMINAL_STATUSES:
                continue  # terminal — not live
            try:
                parts.append(lpath.read_text(errors="replace"))
            except OSError as exc:
                _LOG.debug("archive_plans: could not read plan %s: %s", lpath, exc)

    return "\n".join(parts), scan_incomplete


def _build_moves_for_plan(
    worktree_root: Path,
    plans_dir: Path,
    candidate_id: str,
    plan_path: Path,
) -> Tuple[Optional[str], List[Move]]:
    """Build the Move list for one primary plan + its sidecars.

    Returns (skip_reason, moves) where:
      - skip_reason is non-None if the plan cannot be archived (no `YYYY-MM-DD`
        filename prefix → no archive destination); the caller classifies it as
        skipped, NOT failed.
      - moves[0] is the primary Move; moves[1:] are sidecar Moves.

    Sidecar Move.candidate_id uses _SIDECAR_PREFIX + primary_candidate_id + ":" + sidecar_filename
    so they can be filtered from the wire output after archive_and_commit.
    """
    fname = plan_path.name
    yyyy_mm = _derive_yyyy_mm(fname)
    if not yyyy_mm:
        return (
            f"cannot-derive-date: filename {fname!r} has no YYYY-MM-DD prefix",
            [],
        )

    arch_dir = worktree_root / "archive" / "specs" / yyyy_mm
    moves: List[Move] = [Move(src=plan_path, dst=arch_dir / fname, candidate_id=candidate_id)]

    # Sidecars: any <stem>.*.md alongside the primary in plans_dir.
    # glob.escape prevents wire-derived stem chars ([ ] ? *) from expanding as
    # glob metacharacters and capturing unintended files (MEDIUM glob-injection fix).
    stem = fname[:-3]  # strip .md suffix
    for scar in sorted(plans_dir.glob(f"{_glob.escape(stem)}.*.md")):
        scar_id = f"{_SIDECAR_PREFIX}{candidate_id}:{scar.name}"
        moves.append(Move(src=scar, dst=arch_dir / scar.name, candidate_id=scar_id))

    return (None, moves)


# ---------------------------------------------------------------------------
# Unreconciled-AC guard — see module docstring "Unreconciled-AC skip guard".
# ---------------------------------------------------------------------------

# Bare-pending Status-cell tokens (case-insensitive, after stripping emphasis/
# strikethrough markup and surrounding whitespace). Any OTHER text — including
# a qualified "pending (external)" — carries an explicit disposition/annotation
# and is NOT counted as unreconciled. Deliberately generous per module
# docstring: ambiguous reads are treated as reconciled.
_BARE_PENDING_AC_TOKENS: frozenset = frozenset({
    "", "☐", "[ ]", "[]", "pending", "open", "todo", "tbd", "○",
})

# Separator-row cell shape (e.g. "---", ":---", "---:", ":---:").
_AC_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _split_table_row(row: str) -> List[str]:
    """Split a markdown table row line into stripped cell strings.

    Tolerates an optional leading/trailing pipe (both are conventional but not
    required by the spec).
    """
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _normalize_ac_status_cell(raw: str) -> str:
    """Normalize a Status cell for bare-pending comparison.

    Strips markdown emphasis/strikethrough wrapper characters (*, _, ~, `)
    and surrounding whitespace, then lower-cases. Does NOT strip interior
    punctuation — "pending (external)" stays "pending (external)" and is
    correctly NOT equal to the bare "pending" token.
    """
    return raw.strip().strip("*_~`").strip().lower()


def _count_unreconciled_ac_rows(text: str) -> int:
    """Return the count of unreconciled ``## Acceptance Criteria`` table rows.

    Returns 0 when: no such section exists, the section has no markdown
    table, no "Status" column header can be identified, or every row already
    carries a disposition. See module docstring "Unreconciled-AC skip guard"
    for the full rationale — this is a bookkeeping-hole detector, not a
    completion gate.
    """
    lines = text.splitlines()

    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == "## Acceptance Criteria":
            start = i + 1
            break
    if start is None:
        return 0

    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    table_rows = [line for line in lines[start:end] if line.strip().startswith("|")]
    if len(table_rows) < 2:
        return 0  # need at least a header row + one data row

    header_cells = _split_table_row(table_rows[0])
    status_idx: Optional[int] = None
    for idx, cell in enumerate(header_cells):
        if _normalize_ac_status_cell(cell) == "status":
            status_idx = idx
            break
    if status_idx is None:
        return 0  # no identifiable Status column — ambiguous, treat as reconciled

    unreconciled = 0
    for row in table_rows[1:]:
        cells = _split_table_row(row)
        # Skip the header-separator row ("|---|---|---|" or ":---:" variants).
        if cells and all(_AC_TABLE_SEP_CELL_RE.match(c) for c in cells if c):
            continue
        # A struck-through leading (ID) cell marks the row as removed/void —
        # never counted regardless of its Status cell.
        if cells and cells[0].strip().startswith("~~"):
            continue
        if status_idx >= len(cells):
            continue  # malformed/short row — ambiguous, treat as reconciled
        if _normalize_ac_status_cell(cells[status_idx]) in _BARE_PENDING_AC_TOKENS:
            unreconciled += 1

    return unreconciled


def _plan_ac_unreconciled_count(plan_path: Path) -> int:
    """Return the unreconciled-AC row count for a plan file.

    Best-effort, NOT fail-closed (mirrors the dirty-tree guard): any read
    failure degrades to 0 rather than blocking archival.
    """
    try:
        text = plan_path.read_text(errors="replace")
    except OSError as exc:
        _LOG.debug(
            "archive_plans: could not read %s for AC-reconciliation check: %s "
            "— treating as reconciled (best-effort)", plan_path, exc,
        )
        return 0
    return _count_unreconciled_ac_rows(text)


# ---------------------------------------------------------------------------
# Dirty-tree + claim-liveness guards (Zone-A).  Both applied at BOTH T1
# (preview) and T3 (act) — mirrors how archive_handoffs.py's Checks 3/4 are
# applied uniformly at both stages.
# ---------------------------------------------------------------------------


async def _plan_worktree_dirty(worktree_root: Path, rel_path: str) -> bool:
    """Return True iff ``git status --porcelain -- <rel_path>`` is non-empty.

    Guards against git-mv'ing a plan file that holds uncommitted working-tree
    or index changes (e.g. an in-flight review-integration apply) out from
    under the editor (Incident: 2026-07-11 — see module docstring "Dirty-tree
    skip guard").

    Async per DR-211 D4 (asyncio.create_subprocess_exec; never blocking
    subprocess.run) — mirrors archive_handoffs._shipped_in_resolvable's
    pattern.  ``_make_git_env()`` (no idx_path — a read-only status call needs
    no private index) is the security perimeter.

    Best-effort: a git-status subprocess failure (non-zero exit, spawn error)
    degrades to "not dirty".  This guard is deliberately NOT the load-bearing
    fail-closed property (the claim-liveness guard below is) — a git-status
    call failing is an environment problem, not an ambiguous liveness read.
    """
    # asyncio deferred to first use here (not module scope) — this module is an
    # eager-loaded fleet op; module-scope `import asyncio` dragged asyncio.base_events
    # into every eager import. Spec: docs/plans/2026-07-24-canonical-resolution-engine.md
    # task W0-1.
    import asyncio

    env = _make_git_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain", "--", rel_path,
            cwd=str(worktree_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError as exc:
        print(f"skip: _plan_worktree_dirty: proc = await asyncio.create_subprocess_exec( failed: {exc}", file=sys.stderr)
        _LOG.debug(
            "archive_plans: git status subprocess failed for %s (%s) — "
            "treating as not-dirty (best-effort)", rel_path, exc,
        )
        return False
    if proc.returncode != 0:
        return False
    return bool(stdout.strip())


async def _plan_claim_live(common_dir: Path, plan_path: Path) -> bool:
    """Return True iff plan_path's plan-execution claim is held by a live
    session, OR the liveness check itself could not be evaluated.

    ⚠ LOAD-BEARING SAFETY PROPERTY — FAIL CLOSED: any exception raised while
    evaluating ``coordinator_core.session.liveness.claim_holder_live``
    (including the ValueError it raises on an empty/invalid claim_dir) is
    caught HERE and treated as "live, defer" — NEVER as "not live, archive"
    (the 2026-07-11 incident this guard exists to prevent). See module
    docstring "Claim-liveness skip guard".

    No claim dir on disk → not live (the fast, common path: no session has
    ever claimed this plan, or the claim was already released at a clean
    terminal).

    Async per DR-211 D4 — ``claim_holder_live`` does synchronous file I/O,
    wrapped in ``asyncio.to_thread`` so it never blocks the event loop.
    """
    import asyncio

    claim_dir = plan_claim_dir(common_dir, plan_path)
    if not claim_dir.is_dir():
        return False
    try:
        return await asyncio.to_thread(_session_liveness.claim_holder_live, str(claim_dir))
    except Exception as exc:
        _LOG.warning(
            "archive_plans: claim-liveness check errored for %s (%s) — cannot "
            "verify liveness, deferring archive of %s (fail-closed)",
            claim_dir, exc, plan_path.name,
        )
        return True


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("fleet.archive_completed_plans")
async def _archive_completed_plans(
    params: dict, repo_root=None
) -> dict:
    """JSON-RPC 'fleet.archive_completed_plans' handler.

    dry_run:true  → T1 preview: enumerate terminal + non-live-referenced plans;
                    return candidates[] (mutates nothing).
    dry_run:false → T3 act: re-verify each candidate_id, git-mv into
                    archive/specs/YYYY-MM/, co-move sidecars, commit.

    repo_root arg is the git common dir (from _OP_KEY_SCOPE = "common_dir").
    Worktree root is derived engine-side via main_worktree_root(common_dir).
    params.repo_root is the optional D3 consistency check ONLY — NOT the
    worktree source (Key Decision 5, contract §3.3).
    """
    # --- Param validation (exit_code:1 on any setup error) ---
    validated = validate_params(params)
    if isinstance(validated, dict):
        return validated  # setup-error envelope already built
    mode, dry_run, candidate_ids = validated

    # --- Derive worktree root from engine-supplied common_dir ---
    if repo_root is None:
        return build_setup_error_result(
            mode, dry_run,
            "fleet.archive_completed_plans: repo_root arg is None — "
            "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')",
        )
    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    # --- D3 repo_root consistency check (contract §3.3) ---
    d3_error = check_repo_root(params.get("repo_root"), common_dir)
    if d3_error:
        return build_setup_error_result(mode, dry_run, d3_error)

    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        # No plans directory — return empty result (consumer-project guard).
        _LOG.debug(
            "archive_plans: docs/plans/ not found at %s; returning empty result",
            worktree_root,
        )
        if dry_run:
            return build_dry_run_result(mode, [])
        return build_act_result(mode, [], [], [])

    if dry_run:
        return await _handle_preview(mode, worktree_root, plans_dir, common_dir)
    else:
        return await _handle_act(mode, worktree_root, plans_dir, candidate_ids, common_dir)


async def _handle_preview(
    mode: str, worktree_root: Path, plans_dir: Path, common_dir: Path
) -> dict:
    """T1 preview: enumerate terminal plans, apply the live-reference,
    dirty-tree, and claim-liveness guards, return candidates.

    Async — the dirty-tree guard (git status subprocess, DR-211 D4) and the
    claim-liveness guard (claim_holder_live file I/O via asyncio.to_thread)
    both need to run per-candidate.
    """
    live_ref_text, live_ref_incomplete = _collect_live_reference_text(worktree_root)
    candidates: List[dict] = []

    for path in sorted(plans_dir.glob("*.md")):
        if _is_sidecar(path):
            continue  # sidecars are not candidates

        status = parse_frontmatter_status(path)
        if status not in _TERMINAL_STATUSES:
            continue  # not terminal

        # Live-reference guard (Key Decision 4, T1 filter): exclude from preview entirely.
        if path.name in live_ref_text:
            _LOG.debug(
                "archive_plans: T1 skip live-referenced plan %s", path.name
            )
            continue

        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        if live_ref_incomplete:
            # The live-reference corpus (state/handoffs/ and/or docs/plans/)
            # could not be fully enumerated this call — a live reference to
            # this plan could be sitting in the unreadable subtree. Fail
            # closed: never archive on a corpus scan we know is partial.
            _LOG.warning(
                "archive_plans: T1 skip %s — live-reference scan incomplete "
                "(unreadable subtree); cannot rule out a live reference there, "
                "failing closed", path.name,
            )
            continue
        # --- end Tier 2 ---

        # cannot-derive-date guard (T1 filter, mirrors live-reference): a terminal plan
        # with no YYYY-MM-DD prefix has no archive destination — never present it as an
        # archivable candidate. WARNING so the misnamed plan is noticed and renamed.
        if _derive_yyyy_mm(path.name) is None:
            _LOG.warning(
                "archive_plans: T1 skip un-dateable terminal plan %s — "
                "rename with a YYYY-MM-DD prefix to make it archivable",
                path.name,
            )
            continue

        rel_path = rel_id(path, worktree_root)

        # Dirty-tree guard (T1 filter): exclude from preview entirely — see
        # module docstring "Dirty-tree skip guard".
        if await _plan_worktree_dirty(worktree_root, rel_path):
            _LOG.debug(
                "archive_plans: T1 skip dirty-tree plan %s", path.name
            )
            continue

        # Claim-liveness guard (T1 filter, FAIL-CLOSED on ambiguous reads) —
        # see module docstring "Claim-liveness skip guard".
        if await _plan_claim_live(common_dir, path):
            _LOG.debug(
                "archive_plans: T1 skip live-claimed plan %s", path.name
            )
            continue

        # Unreconciled-AC guard (T1 filter, best-effort) — see module
        # docstring "Unreconciled-AC skip guard".
        ac_unreconciled = _plan_ac_unreconciled_count(path)
        if ac_unreconciled:
            _LOG.debug(
                "archive_plans: T1 skip unreconciled-ac plan %s (%d pending row(s))",
                path.name, ac_unreconciled,
            )
            continue

        title = _extract_title(path) or path.stem
        candidates.append({
            "id": rel_path,
            "title": title,
            "status": status,
            "family": "plan",
            "terminal_since": None,  # not materialised for plans (contract allows null)
            "note": None,            # handoff-only per contract §2.1
        })

    return build_dry_run_result(mode, candidates)


async def _handle_act(
    mode: str,
    worktree_root: Path,
    plans_dir: Path,
    candidate_ids: List[str],
    common_dir: Path,
) -> dict:
    """T3 act: per-candidate D1 re-verify + live-reference/dirty-tree/claim-liveness/
    unreconciled-AC guards + git-mv + commit.

    For each candidate_id:
    1. Source gone → skipped reason:"already-archived" (idempotent replay, AC12).
    2. D1 terminality re-verify: drifted non-terminal → skipped reason:"terminality-drift:...".
    3. Live-reference guard at T3: still cited → skipped reason:"live-reference".
    4. Dirty-tree guard at T3: uncommitted working-tree changes →
       skipped reason:"dirty-tree: ...".
    5. Claim-liveness guard at T3 (FAIL-CLOSED on ambiguous reads): live
       plan-execution claim → skipped reason:"live-claim: ...".
    6. Unreconciled-AC guard at T3 (best-effort): bare-pending AC-table rows
       → skipped reason:"unreconciled-ac: N pending row(s)".
    7. No YYYY-MM prefix → skipped reason:"cannot-derive-date" (no archive
       destination; skip-and-report, NOT a failure — see live-reference precedent).
    8. Otherwise: build Move (primary + sidecars) and add to batch.

    After all checks, calls archive_and_commit once for the full batch (ONE commit).
    Sidecar results are filtered from wire output; sidecar failures are logged only.
    """
    # Collect live-reference text once (shared across all candidates in this act call).
    live_ref_text, live_ref_incomplete = _collect_live_reference_text(worktree_root)

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []

    # Map primary candidate_id → its Move list (index 0 = primary, 1+ = sidecars).
    candidate_moves: Dict[str, List[Move]] = {}

    # Resolve the allowed root once outside the loop (CRITICAL path-traversal fix).
    plans_dir_safe = (worktree_root / "docs" / "plans").resolve()

    for cid in candidate_ids:
        plan_path = worktree_root / cid

        # Path-traversal containment guard (CRITICAL 1): reject candidate_id that
        # resolves outside docs/plans/ — covers absolute-path override and ../ traversal.
        # Must run BEFORE any file read or git op on plan_path.
        resolved_plan = plan_path.resolve()
        if not resolved_plan.is_relative_to(plans_dir_safe):
            _LOG.warning(
                "archive_plans: rejecting path-traversal candidate_id %r "
                "(resolved %s escapes docs/plans/)", cid, resolved_plan,
            )
            failed.append({"id": cid, "reason": "path-traversal: candidate_id escapes docs/plans/"})
            continue
        plan_path = resolved_plan

        # Already-archived: source gone → idempotent skip (AC12; DR-211 D2(i)).
        if not plan_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # D1 act-time terminality re-verify (contract §3.1 D1).
        status = parse_frontmatter_status(plan_path)
        if status not in _TERMINAL_STATUSES:
            skipped.append({
                "id": cid,
                "reason": f"terminality-drift: status is now {status!r}",
            })
            continue

        # Live-reference guard at T3 (Key Decision 4 — applied at both T1 and T3).
        if plan_path.name in live_ref_text:
            skipped.append({"id": cid, "reason": "live-reference"})
            continue

        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        if live_ref_incomplete:
            # See _handle_preview's identical T1 guard: a partial live-reference
            # corpus scan must never be trusted to prove "not referenced".
            skipped.append({"id": cid, "reason": "live-reference-scan-incomplete"})
            continue
        # --- end Tier 2 ---

        rel_for_status = rel_id(plan_path, worktree_root)

        # Dirty-tree guard at T3 (applied at both T1 and T3) — see module
        # docstring "Dirty-tree skip guard".
        if await _plan_worktree_dirty(worktree_root, rel_for_status):
            skipped.append({
                "id": cid,
                "reason": "dirty-tree: uncommitted working-tree changes",
            })
            continue

        # Claim-liveness guard at T3 (FAIL-CLOSED on ambiguous reads; applied
        # at both T1 and T3) — see module docstring "Claim-liveness skip guard".
        if await _plan_claim_live(common_dir, plan_path):
            skipped.append({
                "id": cid,
                "reason": "live-claim: plan-execution claim held by live session",
            })
            continue

        # Unreconciled-AC guard at T3 (best-effort; applied at both T1 and T3)
        # — see module docstring "Unreconciled-AC skip guard".
        ac_unreconciled = _plan_ac_unreconciled_count(plan_path)
        if ac_unreconciled:
            skipped.append({
                "id": cid,
                "reason": f"unreconciled-ac: {ac_unreconciled} pending row(s)",
            })
            continue

        # Build move list for this candidate (primary + sidecars).
        skip_reason, moves = _build_moves_for_plan(
            worktree_root, plans_dir, cid, plan_path
        )
        if skip_reason:
            # cannot-derive-date: a terminal plan with no YYYY-MM-DD filename prefix has
            # no archive destination. Skip-and-report (like live-reference) — NOT a failure.
            # Hard-failing it would poison exit_code=2 fleet-wide on every session-init sweep.
            _LOG.warning(
                "archive_plans: T3 skip un-dateable terminal plan %s (%s) — "
                "rename with a YYYY-MM-DD prefix to make it archivable",
                cid, skip_reason,
            )
            skipped.append({"id": cid, "reason": skip_reason})
            continue

        candidate_moves[cid] = moves

    if not candidate_moves:
        # Nothing to move — all candidates were skipped or pre-failed.
        return build_act_result(mode, acted, skipped, failed)

    # Flatten all moves (primaries first within each candidate, then their sidecars).
    flat_moves: List[Move] = []
    for cid, moves in candidate_moves.items():
        flat_moves.extend(moves)

    # Primary candidate_id set for result mapping.
    primary_ids: Set[str] = set(candidate_moves.keys())

    # ONE atomic commit for the full batch (DR-211 D3/D4; Key Decision 2; AC4).
    # TOCTOU note: a narrow window exists between the per-candidate D1 terminality
    # re-verify above and the git-mv inside archive_and_commit below.  This is the
    # accepted DR-211 D1-at-act residual; the T3 re-verify already narrows the window
    # to the call-site gap.  Re-asserting terminality inside archive_and_commit would
    # require a larger refactor than the security benefit warrants here.
    # Review: code-reviewer (F8) — aligned prefix+count format with sibling handlers.
    n_plans = len(candidate_moves)
    subject = f"fleet: archive {n_plans} terminal plan(s) [fleet.archive_completed_plans]"
    raw_acted, raw_failed = await archive_and_commit(worktree_root, flat_moves, subject)

    raw_acted_ids: Set[str] = {a["id"] for a in raw_acted}
    raw_failed_by_id: Dict[str, str] = {f["id"]: f["reason"] for f in raw_failed}

    for cid, moves in candidate_moves.items():
        primary_move = moves[0]  # index 0 is always the primary plan's Move
        sidecar_moves = moves[1:]

        if primary_move.candidate_id in raw_failed_by_id:
            # Primary git-mv failed → plan was NOT archived.
            failed.append({
                "id": cid,
                "reason": raw_failed_by_id[primary_move.candidate_id],
            })
        elif primary_move.candidate_id in raw_acted_ids:
            # Primary succeeded → report acted regardless of sidecar outcome.
            acted.append({"id": cid, "archived": True})
            # Log any sidecar failures as warnings (not fatal for the primary).
            for scar_move in sidecar_moves:
                if scar_move.candidate_id in raw_failed_by_id:
                    _LOG.warning(
                        "archive_plans: sidecar move failed (primary plan still archived): "
                        "plan=%s sidecar=%s reason=%s",
                        cid,
                        scar_move.src.name,
                        raw_failed_by_id[scar_move.candidate_id],
                    )
        else:
            # Should not occur (primary not in acted or failed after archive_and_commit).
            # Treat as failed to avoid silent omission.
            _LOG.error(
                "archive_plans: primary plan %s absent from both acted and failed after "
                "archive_and_commit — treating as failed",
                cid,
            )
            failed.append({"id": cid, "reason": "unexpected: absent from acted and failed"})

    return build_act_result(mode, acted, skipped, failed)
