"""
coordinator_core.ops.queue_family — the queue-family read seam C3/C4/C5 share.

Purpose: normalize the three on-disk queue-family names (``improvement-queue``,
``debt-backlog``, ``bug-backlog``) onto the ``record_type`` values
``coordinator_core.ops.ceremony.records_query.query_records`` expects
(``improvement``, ``debt``, ``bug``), and expose ONE load function that
delegates every record-collection/parse/skip-unparseable concern to that
seam. Per-family required-field differences are a lookup table over the
records ``query_records`` already returns — never a second file-loading path.

Spec backlink: pln-queue-triage-terminus-ops-clus-043c40 § C1

Negative-spec:
  - Does NOT glob, walk directories, or parse YAML/frontmatter itself — all
    loading routes through ``query_records``. A caller reaching for
    ``Path.glob``/``Path.iterdir``/``yaml.safe_load`` instead of this module's
    ``load_family_records`` is reimplementing the read seam this module
    exists to prevent.
  - Does NOT build on ``coordinator_core.ops.queue_append._output_dir_for_schema``
    — that resolver belongs to the WRITE path (where a new entry gets
    written), not the read path. This module is READ-only.
  - Does NOT add a fourth queue family or a theme/subsystem field — the
    three families and their field tables below are the closed set.

Import cost for spawn-per-call callers: this module lives under
``coordinator_core.ops``, whose ``__init__`` eagerly imports every op module to
populate the registry by default — ~70ms a fresh CLI process pays without ever
dispatching an op. A caller that only wants this read seam sets
``sys._coordinator_core_lazy_ops = True`` before its first
``coordinator_core.ops`` import (see that package's ``_lazy_ops_requested``);
``load_family_records`` needs no registry and is unaffected. Measured 94ms ->
26ms on macOS/CPython 3.13. Not ``os.environ["COORDINATOR_CORE_LAZY_OPS"]`` —
that variable is the operator override, and writing it in-process hands the
flag to every child the caller later spawns, which is the 2026-07-28 leak.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.fleet._common import main_worktree_root

FAMILY_TO_RECORD_TYPE: dict[str, str] = {
    "improvement-queue": "improvement",
    "debt-backlog": "debt",
    "bug-backlog": "bug",
}
"""Queue-family directory name -> the ``record_type`` key ``query_records`` accepts."""


class UnknownQueueFamilyError(ValueError):
    """Raised by :func:`normalize_family` for a family name outside the closed set."""


def normalize_family(family: str) -> str:
    """Map a queue-family name to its ``query_records`` ``record_type``.

    Raises ``UnknownQueueFamilyError`` (a ``ValueError`` subclass) naming the
    offending value and the full set of valid families, for an unrecognized
    ``family``.
    """
    try:
        return FAMILY_TO_RECORD_TYPE[family]
    except KeyError:
        valid = ", ".join(sorted(FAMILY_TO_RECORD_TYPE))
        raise UnknownQueueFamilyError(
            f"unknown queue family {family!r} — valid families are: {valid}"
        ) from None


# Per-family field table over query_records()'s returned frontmatter dicts.
#
# ``required`` mirrors each family's JSON Schema ``required`` array
# (coordinator_core/frontmatter/schemas/{improvement-queue,debt-backlog,
# bug-backlog}.schema.json). ``optional`` lists the cross-family fields that
# are schema-declared but frequently absent on real entries (per the
# "read defensively" convention — a missing optional field is normal input).
# This is a lookup TABLE, not a per-family branch: callers index it by the
# already-normalized family name, they never write ``if family == ...``.
FAMILY_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "improvement-queue": {
        "required": (
            "created",
            "title",
            "body",
            "status",
            "surface",
            "proposed_action",
            "from_repo",
            "change_kind",
        ),
        "optional": ("tags", "initiative", "queue_scope", "evidence"),
    },
    "debt-backlog": {
        "required": (
            "created",
            "title",
            "body",
            "status",
            "source",
            "risk",
            "proposed_action",
        ),
        "optional": ("tags", "initiative", "severity", "surface", "from_repo", "evidence"),
    },
    "bug-backlog": {
        "required": (
            "created",
            "title",
            "body",
            "status",
            "surface",
            "severity",
        ),
        "optional": ("tags", "initiative", "from_repo", "proposed_action", "evidence"),
    },
}
"""family -> {"required": (...), "optional": (...)} field-name tuples."""


def load_family_records(
    family: str,
    repo_root: Path,
    *,
    where: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 0,
) -> list[dict]:
    """Load every record in ``family`` under ``repo_root`` via ``query_records``.

    ``family`` is normalized via :func:`normalize_family` first (raises
    ``UnknownQueueFamilyError`` on an unrecognized name); the resulting
    ``record_type`` is handed straight to ``query_records`` — no directory
    walk, no frontmatter parse, no unparseable-record handling lives here,
    all of it is ``query_records``'s.

    ``repo_root`` is normalized to the main worktree root before querying,
    because this function is handed either shape depending on the caller:

    - ``queue.cluster`` and ``queue.age_ping`` are registered ``"common_dir"``
      in ``coordinator_core.op_scopes._OP_KEY_SCOPE`` (matching ``queue.append``,
      which must stay shared-across-worktrees routing) — so the IPC engine's
      ``resolve_op_repo_key`` (``coordinator_core/ipc.py``) hands their handlers
      ``git_common_dir(caller_worktree)`` (i.e. ``<worktree>/.git``), never the
      worktree root itself.
    - Direct callers of this function (this module's own tests, and any future
      non-IPC caller) may hand it the worktree root directly.

    ``query_records`` looks for ``state/<family>/`` relative to whatever root
    it is given, so an un-derived common-dir ``repo_root`` silently finds
    nothing under ``<worktree>/.git/state/...`` and returns an empty list —
    no exception, no error, just wrong data (see the fix note below).

    ``coordinator_core.ops.fleet._common.main_worktree_root`` derives the
    worktree root from a common dir via a bare ``.parent`` — it performs no
    filesystem check of its argument, so calling it on an already-worktree-root
    path does NOT raise; it silently returns the WRONG directory (one level
    too high, the worktree's own parent). Verified against its implementation:
    it is unconditionally correct only when the input truly is a git common
    dir. It is therefore not safe to call unconditionally here — this function
    must first distinguish the two shapes itself, and it must never fall
    through silently when it can't: :func:`_resolve_worktree_root` classifies
    ``repo_root`` by its *contents* — does it look like a git common dir (has
    ``HEAD`` plus ``objects``/``refs`` directly present)? — rather than by its
    *name*. Anything that does NOT have that shape is trusted as an
    already-worktree-root path unchanged (this module's own tests, and any
    future non-IPC caller, routinely hand it a plain directory with no git
    structure at all — ``query_records`` needs no real git repo to function).
    Anything that DOES have the common-dir shape is derived via
    ``main_worktree_root``, and the DERIVED root is then verified to itself
    look like a worktree root (has a ``.git`` entry) before being trusted; if
    that verification fails, it raises ``ValueError`` naming what it was
    handed — never silently returns an empty list for a common dir it could
    not correctly derive from. (A ``.name == ".git"`` name check was tried and
    rejected: a bare repo's common dir, or a submodule's common dir at
    ``.git/modules/<name>/``, is not literally named ``.git`` but DOES have
    the ``HEAD``/``objects``/``refs`` shape, and its derived worktree root
    reliably fails the ``.git``-entry verification — so the failure is now
    caught instead of silently reproducing the exact bug this fix closes, one
    layer down.) An empty result for a *correctly resolved* root — e.g. a
    fresh consumer repo with no ``state/<family>/`` directory yet — remains a
    legitimate ``[]``, never an error; only a common-dir-shaped root whose
    derivation fails verification raises.

    Negative-spec (fixed 2026-07-23): a prior version of this function
    passed ``repo_root`` straight to ``query_records`` with no derivation at
    all. Because ``common_dir``-scoped ops receive the git common dir (not
    the worktree root) from the IPC engine, ``queue.cluster`` and
    ``queue.age_ping`` silently returned an empty result for every real
    caller — the bug was invisible because every existing test fixture
    handed this function a worktree path directly, never a common dir, so
    the missing derivation was never exercised. See
    ``coordinator_core/ops/tests/test_queue_family.py`` for the regression
    coverage that now calls this function with a fixture's actual common dir,
    and for the follow-up regression covering a common-dir shape the
    resolver cannot classify.
    """
    record_type = normalize_family(family)
    repo_root = Path(repo_root)
    worktree_root = _resolve_worktree_root(repo_root)
    return query_records(record_type, worktree_root, where=where, since=since, limit=limit)


def _looks_like_git_common_dir(path: Path) -> bool:
    """True iff ``path`` has the shape of a git common dir: ``HEAD`` plus ``objects``/``refs``.

    Contents-based, not name-based — a bare repo's common dir and a
    submodule's common dir (``.git/modules/<name>/``) both have this shape
    without being literally named ``.git``, which is exactly the case the
    rejected ``repo_root.name == ".git"`` heuristic missed.
    """
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def _resolve_worktree_root(repo_root: Path) -> Path:
    """Resolve ``repo_root`` (a worktree root OR a git common dir) to the worktree root.

    Only ``repo_root``s that look like a git common dir (``HEAD`` plus
    ``objects``/``refs`` directly present — see :func:`_looks_like_git_common_dir`)
    trigger derivation at all; anything else is trusted as an already-worktree-root
    path unchanged, exactly as before (this module's own tests, and any future
    non-IPC caller, routinely hand it a plain directory with no git structure of
    its own — ``query_records`` needs no git repo to function). Within the
    common-dir branch, this function raises ``ValueError`` rather than silently
    falling through to "treat as worktree root" whenever the DERIVED root fails
    its own verification — see :func:`load_family_records`'s docstring for why a
    name-based discriminator (``repo_root.name == ".git"``) was rejected in favor
    of this contents-based check: a bare repo or a submodule's common dir
    (``.git/modules/<name>/``) has the common-dir shape without being literally
    named ``.git``, and its derived worktree root reliably fails the ``.git``-entry
    verification below (a bare repo's or a submodule ``.git/modules/`` parent is
    never itself a worktree root), so the failure is caught rather than silently
    reproducing the original bug one layer down.
    """
    if not _looks_like_git_common_dir(repo_root):
        return repo_root
    candidate = main_worktree_root(repo_root)
    if (candidate / ".git").exists():
        return candidate
    raise ValueError(
        f"load_family_records: repo_root {repo_root!s} looks like a git common "
        f"dir, but its derived worktree root {candidate!s} has no '.git' entry — "
        "refusing to silently query the wrong directory. This can happen for a "
        "non-standard common-dir shape (bare repo, submodule, relocated "
        "GIT_COMMON_DIR) that main_worktree_root's bare '.parent' derivation "
        "does not support."
    )


def fields_for_family(family: str) -> dict[str, tuple[str, ...]]:
    """Return the ``{"required": (...), "optional": (...)}`` field tuple for ``family``.

    Raises ``UnknownQueueFamilyError`` for an unrecognized family — kept in
    sync with :func:`normalize_family` by construction (same validation,
    same closed set).
    """
    normalize_family(family)  # validates + raises the shared clear error
    return FAMILY_FIELDS[family]
