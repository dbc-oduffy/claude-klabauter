"""
coordinator_core.ops.fleet.archive_queue_entry — "fleet.archive_queue_entry" op.

Purpose: single-entry git-mv of one closed state/improvement-queue/*.yaml
entry into archive/improvement-queue/YYYY-MM/, under a confirm→act
(dry_run:true preview / dry_run:false act) contract. Unlike the sibling
fleet.prune_closed_bugs (candidate-discovery over an entire directory by
terminality predicate), the caller already names the one entry to archive —
this op is thin candidate-discovery + confirm→act over a single path, not a
directory scan.

Wire contract (per state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv row "archive-queue-entry"):
    params:   {repo_root: str (optional D3 consistency check),
               entry_path: str (required unless entry_paths is given,
                           repo-relative or absolute path under
                           state/improvement-queue/),
               dry_run: bool (required)}
    response: {archived: bool, dest: str, exit_code: int, error: str|None}

Batch addition (2026-08-06, F9 fix — a sweep over N closed entries committing
per-entry raced HEAD 36-for-36 and lost one to a `cannot lock ref 'HEAD'`
collision; see state/handoffs/2026-08-06-claude-klabauter-dogfood-engine-fixes.md §D):
`params.entry_paths` (list[str], repo-relative or absolute, non-empty) is an
ADDITIVE, mutually-exclusive alternative to `entry_path` — the caller already
names every entry to archive (still not a directory scan), but ALL of them
land in ONE `archive_and_commit` call, so a sweep costs ONE commit instead of
N. This is a strictly NEW response shape, never returned unless the caller
opts in by sending `entry_paths`, so the singular `entry_path` contract above
(and every caller of it -- notably queue.close per DR-270, itself
gravestoned at `c07062c99`, kill-ledger `## K-049`, so this names the
contract's history and not a live caller) is untouched:
    params:   {repo_root: str (optional), entry_paths: list[str] (required,
               non-empty, mutually exclusive with entry_path), dry_run: bool}
    response: {exit_code: int, archived: None, dest: None,
               items: [{"id": str, "dest": str|None, "error": str|None}, ...]}
                 (dry_run:true — read-only; per-item dest preview, or an
                 error naming a path-traversal escape; exit_code 2 iff any
                 item errored)
              {exit_code: int, archived: None, dest: None,
               items: [{"id": str, "archived": bool, "dest": str|None,
                        "error": str|None}, ...]}
                 (dry_run:false — act; exit_code 1 iff any item failed to
                 archive, per-item detail always in items[], never
                 collapsed to one boolean the caller has to re-diff against
                 its own request)

Archive destination: archive/improvement-queue/YYYY-MM/<filename> where
YYYY-MM is derived from the filename prefix (YYYY-MM-DD-slug.yaml); falls
back to the entry's `created:` plain-YAML field, same two-source derivation
as fleet.prune_closed_bugs' _archive_month.

Zero new move/rename logic: the actual git-mv + single-commit mechanics are
`ops/fleet/_common.py::archive_and_commit`, reused verbatim (plan
docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
§ Wave 1 C1b — "ops/fleet/prune_bugs.py is the closest template ... write
zero new move/rename logic").

Spec backlinks:
  - Plan (C1b): docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 1
  - Oracle: state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv row "archive-queue-entry"
  - Classification: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv row "archive-queue-entry"
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4, five bounds)
  - Path containment: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4

Scope: registered "common_dir" (per the manifest's scope-verdict column) —
this op writes into the CALLER's own archive/improvement-queue/YYYY-MM/, so
the worktree root MUST be derived from the engine-supplied common_dir
(main_worktree_root), never from params.repo_root, or a cross-repo caller
would silently archive against claude-klabauter's own clone instead of the caller's.

Idempotency (AC7): a repeat dry_run:false call for an entry_path whose
source file no longer exists (already archived) or whose destination
already exists (concurrent archive) is a vacuous no-op — {archived: False,
exit_code: 0}, never a failed git-mv. Manifest rates this hazard low
("closed by candidate-discovery filtering to entries still present at the
source path before attempting the move").

Negative-spec:
  - Does NOT scan state/improvement-queue/ for terminal entries — the caller
    names every entry_path/entry_paths element to archive (unlike prune_bugs'
    directory scan), singular or batch. A batch-close ceremony that wants
    "archive every closed entry" enumerates the candidates itself (e.g. via
    queue_family.load_family_records) and passes them as entry_paths — it
    does not get a directory-scan discovery mode here.
  - Does NOT use blocking subprocess.run (DR-211 D4 async mandate) —
    archive_and_commit is the sole git-mutation path and is fully async.
  - Does NOT use git add -A or git add . — scoped exact-pathspec only,
    inside archive_and_commit (DR-211 D3 Invariant 4).
  - Does NOT modify rag's relational store (DR-211 D5 five bounds).
  - Does NOT use params.repo_root as worktree source — worktree is derived
    via main_worktree_root(repo_root) (same as prune_bugs); params.repo_root
    is the D3 consistency check only.
  - Does NOT rmdir an emptied state/improvement-queue/ — the directory is a
    perpetual queue (unlike a per-month archive bucket); the shell fence's
    "rmdir if empty" idiom has no live target here since improvement-queue
    entries live flat (no per-entry subdirectories to clean up).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import (
    Move,
    archive_and_commit,
    check_repo_root,
    main_worktree_root,
)
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)

# Filename date-prefix pattern: YYYY-MM-DD-slug.yaml (same convention as prune_bugs).
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")


# ---------------------------------------------------------------------------
# Plain-YAML reader — improvement-queue files are plain YAML (no --- fences),
# same shape as state/bug-backlog/*.yaml.
# ---------------------------------------------------------------------------

def _read_plain_yaml(path: Path) -> dict:
    """Read a plain-YAML improvement-queue file and return its content as a dict.

    Returns {} on any parse error, missing file, or non-mapping content —
    the `created:` fallback in `_archive_month` degrades gracefully when
    this returns {}.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _archive_month(path: Path) -> str:
    """Derive the YYYY-MM archive sub-directory from a queue-entry YAML path.

    Primary source: filename prefix YYYY-MM-DD-slug.yaml.
    Fallback: `created:` plain-YAML field (YYYY-MM-DD or YYYY-MM).
    Last resort: "unknown" — never raises.
    """
    m = _DATE_PREFIX_RE.match(path.name)
    if m:
        return m.group(1)

    meta = _read_plain_yaml(path)
    if meta:
        created = meta.get("created")
        if created:
            created_str = str(created)
            if re.match(r"^\d{4}-\d{2}", created_str):
                return created_str[:7]

    _LOG.warning("archive_queue_entry: could not derive YYYY-MM for %s; using 'unknown'", path.name)
    return "unknown"


def _reply(*, exit_code: int, archived: bool, dest: Optional[str], error: Optional[str] = None) -> dict:
    """Build the {archived, dest, exit_code, error} response envelope."""
    return {"exit_code": exit_code, "archived": archived, "dest": dest, "error": error}


# ---------------------------------------------------------------------------
# Batch handler (2026-08-06, F9 fix) — see module docstring, § Batch addition.
# ---------------------------------------------------------------------------

async def _handle_batch(worktree: Path, queue_dir: Path, entry_paths: list, dry_run: bool) -> dict:
    """Batch variant of the single-entry handler: git-mv every caller-named
    closed improvement-queue entry into archive/improvement-queue/YYYY-MM/ in
    ONE commit, instead of one commit per entry.

    dry_run:true  → per-item dest preview; mutates nothing.
    dry_run:false → ONE archive_and_commit call over every item that resolved
                    to an existing, not-yet-archived source path; per-item
                    outcome always reported in items[], never collapsed.

    See module docstring § Batch addition for the full response shape.
    """
    items: List[dict] = []
    resolved: List[Tuple[str, Path, Path]] = []  # (id, contained_src, dest)

    for entry_path_raw in entry_paths:
        raw = entry_path_raw.strip() if isinstance(entry_path_raw, str) else ""
        if not raw:
            # Review: coordinator:code-reviewer — echo the same normalized
            # `raw` every other branch reports, not the unstripped/untyped
            # entry_path_raw, for a consistent per-item id shape.
            items.append({"id": raw, "dest": None, "error": "empty entry_path"})
            continue

        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = worktree / candidate

        contained = contained_path(candidate, [queue_dir])
        if contained is None:
            items.append({
                "id": raw, "dest": None,
                "error": f"entry_path escapes state/improvement-queue/: {raw!r}",
            })
            continue

        ym = _archive_month(contained)
        dest = worktree / "archive" / "improvement-queue" / ym / contained.name
        dest_rel = rel_id(dest, worktree)
        items.append({"id": raw, "dest": dest_rel, "error": None})
        resolved.append((raw, contained, dest))

    if dry_run:
        exit_code = 2 if any(it["error"] for it in items) else 0
        return {"exit_code": exit_code, "archived": None, "dest": None, "items": items}

    # ---- dry_run:false — act ----
    by_id = {it["id"]: it for it in items}
    moves: List[Move] = []

    for raw, contained, dest in resolved:
        # Already-archived (source gone) or destination already present
        # (concurrent archive / replay edge-case): idempotent no-op, same as
        # the single-entry path — never a failed git-mv.
        if not contained.exists() or dest.exists():
            by_id[raw]["archived"] = False
            continue
        moves.append(Move(src=contained, dst=dest, candidate_id=raw))

    if moves:
        commit_subject = (
            f"fleet: archive {len(moves)} closed improvement-queue "
            f"{'entry' if len(moves) == 1 else 'entries'} [fleet.archive_queue_entry]"
        )
        acted, failed = await archive_and_commit(worktree, moves, commit_subject)
        for a in acted:
            by_id[a["id"]]["archived"] = True
        for f in failed:
            by_id[f["id"]]["archived"] = False
            by_id[f["id"]]["error"] = f["reason"]

    for it in items:
        if "archived" not in it:
            # Never reached resolved/moves (e.g. an empty-id or path-traversal
            # item already carrying its own error) — always False, never
            # silently absent from the per-item report.
            it["archived"] = False

    exit_code = 1 if any(it.get("error") for it in items) else 0
    return {"exit_code": exit_code, "archived": None, "dest": None, "items": items}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("fleet.archive_queue_entry")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_queue_entry — archive one closed improvement-queue YAML entry,
    or (params.entry_paths) a batch of them in ONE commit.

    dry_run:true  → does not mutate; returns the {archived: False, dest: <computed>}
                    preview so a caller can confirm the destination before acting.
    dry_run:false → git-mv the entry into archive/improvement-queue/YYYY-MM/ + commit;
                    returns {archived: True, dest: <path>} on success, or a vacuous
                    no-op {archived: False, exit_code: 0} if the source is already
                    gone or the destination already exists (idempotent replay).

    See module docstring § Batch addition for the entry_paths (plural) shape,
    handled by `_handle_batch` — mutually exclusive with entry_path (singular).

    repo_root arg: the git common dir delivered by _OP_KEY_SCOPE="common_dir".
                   Handlers MUST NOT use ctx.repo_root (None in the global service).
                   Worktree root is derived via main_worktree_root(repo_root).
                   params.repo_root is the D3 consistency check only — NOT the
                   worktree-root resolution source.
    """
    entry_path_raw: str = (params.get("entry_path") or "").strip()
    entry_paths_raw = params.get("entry_paths")
    dry_run = params.get("dry_run")
    param_repo_root = params.get("repo_root")

    if not isinstance(dry_run, bool):
        return _reply(
            exit_code=2, archived=False, dest=None,
            error=f"'dry_run' must be bool, got {type(dry_run).__name__!r}",
        )

    batch_mode = entry_paths_raw is not None
    if batch_mode:
        if entry_path_raw:
            return _reply(
                exit_code=2, archived=False, dest=None,
                error="'entry_path' and 'entry_paths' are mutually exclusive",
            )
        if not isinstance(entry_paths_raw, list) or not entry_paths_raw:
            return _reply(
                exit_code=2, archived=False, dest=None,
                error="'entry_paths' must be a non-empty list",
            )
    elif not entry_path_raw:
        return _reply(exit_code=2, archived=False, dest=None, error="'entry_path' is required")

    if repo_root is None:
        _LOG.error(
            "fleet.archive_queue_entry: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production"
        )
        return _reply(
            exit_code=1, archived=False, dest=None,
            error="repo_root is None; cannot derive worktree root (keying-table misconfiguration)",
        )

    common_dir = Path(repo_root)
    mismatch = check_repo_root(param_repo_root, common_dir)
    if mismatch:
        return _reply(exit_code=1, archived=False, dest=None, error=mismatch)

    worktree = main_worktree_root(common_dir)
    queue_dir = worktree / "state" / "improvement-queue"

    if batch_mode:
        return await _handle_batch(worktree, queue_dir, entry_paths_raw, dry_run)

    candidate = Path(entry_path_raw)
    if not candidate.is_absolute():
        candidate = worktree / candidate

    contained = contained_path(candidate, [queue_dir])
    if contained is None:
        return _reply(
            exit_code=2, archived=False, dest=None,
            error=f"entry_path escapes state/improvement-queue/: {entry_path_raw!r}",
        )

    ym = _archive_month(contained)
    dest = worktree / "archive" / "improvement-queue" / ym / contained.name
    dest_rel = rel_id(dest, worktree)

    if dry_run:
        return _reply(exit_code=0, archived=False, dest=dest_rel)

    # ---- dry_run:false — act ----

    # Already-archived (source gone): idempotent replay.
    if not contained.exists():
        return _reply(exit_code=0, archived=False, dest=dest_rel)

    # Destination already present (concurrent archive or replay edge-case).
    if dest.exists():
        return _reply(exit_code=0, archived=False, dest=dest_rel)

    candidate_id = rel_id(contained, worktree)
    move = Move(src=contained, dst=dest, candidate_id=candidate_id)
    commit_subject = f"fleet: archive improvement-queue entry {candidate_id} [fleet.archive_queue_entry]"

    acted, failed = await archive_and_commit(worktree, [move], commit_subject)

    if failed:
        return _reply(exit_code=1, archived=False, dest=dest_rel, error=failed[0]["reason"])

    return _reply(exit_code=0, archived=True, dest=dest_rel)
