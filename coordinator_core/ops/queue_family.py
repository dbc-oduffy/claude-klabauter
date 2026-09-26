
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
    pass


def normalize_family(family: str) -> str:
    try:
        return FAMILY_TO_RECORD_TYPE[family]
    except KeyError:
        valid = ", ".join(sorted(FAMILY_TO_RECORD_TYPE))
        raise UnknownQueueFamilyError(
            f"unknown queue family {family!r} — valid families are: {valid}"
        ) from None


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
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def _resolve_worktree_root(repo_root: Path) -> Path:
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
    normalize_family(family)
    return FAMILY_FIELDS[family]
