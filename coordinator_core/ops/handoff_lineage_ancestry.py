"""
coordinator_core.ops.handoff_lineage_ancestry — JSON-RPC "handoff.lineage_ancestry" operation.

Purpose: Read-only compute primitive that walks a fork handoff's ``origin_handoff``
provenance edge to compute its ancestry chain — the fork, then its origin handoff,
then that handoff's origin, transitively, until an edge is absent/sentinel or a
cycle is detected. Returns the ordered ancestry list. This is claude-klabauter's compute half
of the fork-provenance lineage story (the "handoff.has_live_children family"); the
durable, transitive, fleet-wide ancestry query ("everything under goal/plan/session
X") is example-retrieval-repo's surface per
docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md (successor to the
tree-local docs/decisions/2026-07-03-tri-plane-ownership-boundary.md) — OUT OF SCOPE here.
NOTE: which surface owns fleet-wide ancestry queries, now that custody of the underlying
bytes is reconciled to capability-vs-custody rather than fleet-SoR, is a DESIGNATED
FOLLOW-UP recorded in DR-236 — this disclaimer is not a resolved answer, just an
out-of-scope marker pending that follow-up.

COMPUTE_ONLY — this op reads handoff frontmatter via ``coordinator_core.dag``'s
cached frontmatter reader and returns a computed ordered list; it NEVER writes any
file, issues any git command, or mutates any coordinator substrate. The
COMPUTE_ONLY invariant (DR-208) is enforced by classification in
``coordinator_core/authz/classification.py``.

Self-registration: importing this module calls
``register_op("handoff.lineage_ancestry", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Compose, don't re-walk: this op is a thin wrapper over
``coordinator_core.dag.walk_forward(..., edge_kinds={'origin_handoff'})``. It does
NOT re-implement DFS, frontmatter parsing, or cycle detection — ``walk_forward``'s
existing gray/black cycle detection is relied on as-is (module docstring,
dag.py:29-33).

Walk-direction confirmation: ``origin_handoff`` is a PROVENANCE edge that points
from a fork toward its origin (parent) — see dag.py's module docstring
("origin_handoff provenance edge") and ``EDGE_KIND_META``. ``walk_forward`` follows
edges named in ``edge_kinds`` starting from ``start_path`` outward — so seeding it
with the fork's path and ``edge_kinds={'origin_handoff'}`` walks fork → origin
handoff → that handoff's origin, transitively. This is ancestry, not descendants:
``walk_forward`` has no notion of "children" here, it only ever follows the named
edge field forward from whatever node it is currently visiting, and
``origin_handoff`` names the parent. (There is no separate "backward" walk in
dag.py; reverse-membership / descendant discovery is what ``referenced_by`` is for,
which this op does not use.)

Worktree resolution mirrors ``handoff_match.py`` / ``handoff_children.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns ``{"ancestry": [], "terminated_early": ""}``
    with a logged warning rather than raising — empty is safe.

Namespace isolation (hard-won, DR-ratified): ``origin_handoff`` is explicitly NOT
part of the default ``walk_forward`` edge set (``{'predecessor'}``) or the default
``referenced_by`` edge set — it must never auto-union into the continuity/coverage/
archival walks. This op passes ``edge_kinds={'origin_handoff'}`` explicitly on
every call; it does not accept a caller-supplied edge_kinds override, precisely so
that this walk can never accidentally widen into lineage/coverage edges.

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C4
Sibling: coordinator_core/ops/handoff_children.py (handoff.has_live_children family)

Negative-spec:
- Does NOT build the durable/transitive fleet-wide ancestry query — that is
  example-retrieval-repo's surface (ratified tri-plane boundary). This op is the live,
  single-repo compute primitive only.
- Does NOT write any file, run git, or mutate coordinator substrate.
- Does NOT let ``origin_handoff`` auto-union into continuity/coverage/archival
  walks — walked ONLY via the explicit ``edge_kinds={'origin_handoff'}`` subset.
- Does NOT re-implement DFS/cycle-detection — composes ``dag.walk_forward`` as-is.
- Does NOT resolve ``handoff_id``/``path`` outside this repo's handoff trees.
  ``handoff_id`` is allowlist-validated (no path separators or traversal
  segments) before path composition; ``path`` is post-resolve containment-
  checked against ``state/handoffs/`` and ``archive/handoffs/`` only. Neither
  param can be used to read arbitrary files elsewhere on disk (Review:
  code-reviewer, Finding 1).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from coordinator_core.dag import walk_forward
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

#: The single provenance edge kind this op is allowed to walk. Fixed — not
#: caller-overridable — so this op can never accidentally widen into the
#: lineage/coverage edge namespace (predecessor / additional_predecessors /
#: forked_from). See module docstring "Namespace isolation".
_ORIGIN_EDGE_KINDS = {"origin_handoff"}


def _resolve_start_path(
    params: dict, worktree_root: Path
) -> Optional[Path]:
    """Resolve the caller-supplied starting handoff to an absolute path on disk.

    Accepts either ``handoff_id`` (filename stem under ``state/handoffs/``,
    mirroring ``handoff_match.py``'s ``handoff_id`` convention) or an explicit
    ``path`` (absolute, or relative to ``worktree_root``). ``handoff_id``, if
    supplied, is authoritative and exclusive — ``path`` is consulted only when
    ``handoff_id`` is absent, NOT when it fails to resolve (Review:
    code-reviewer, Finding 4 — a supplied-but-unresolvable ``handoff_id``
    returns ``None`` immediately; it does not fall through to ``path``).

    Returns ``None`` if neither param is usable or the resolved file does not
    exist on disk — callers must treat ``None`` as "cannot start the walk".

    Containment guarantee (Review: code-reviewer, Finding 1): both params are
    constrained to this repo's handoff trees. ``handoff_id`` is validated
    against an allowlist regex (no ``/``, ``\\``, or ``..`` — no path
    separators at all) before it is interpolated into a path, so a traversal
    value is rejected outright rather than resolved. ``path`` is
    post-resolve-checked to stay under ``state/handoffs/`` or
    ``archive/handoffs/`` (the ancestry walk can transitively resolve
    ``origin_handoff`` edges into the archive tree via
    ``dag._resolve_target``, so archived starting handoffs are a legitimate
    case) — anything else, including absolute paths elsewhere on disk or
    symlinks that escape via ``.resolve()``, is rejected.
    """
    handoff_id = params.get("handoff_id")
    if isinstance(handoff_id, str) and handoff_id.strip():
        handoff_id = handoff_id.strip()
        if not safe_id(handoff_id):
            return None
        candidate = worktree_root / "state" / "handoffs" / f"{handoff_id}.md"
        if candidate.is_file():
            return candidate
        return None

    raw_path = params.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        raw_path = raw_path.strip()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = worktree_root / candidate
        allowed_roots = [
            worktree_root / "state" / "handoffs",
            worktree_root / "archive" / "handoffs",
        ]
        if contained_path(candidate, allowed_roots) is None:
            return None
        if candidate.is_file():
            return candidate
        return None

    return None


@register_op("handoff.lineage_ancestry")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.lineage_ancestry" handler.

    Walks the starting handoff's ``origin_handoff`` provenance edge, transitively,
    to compute its ancestry chain (fork → origin handoff → that handoff's origin
    → ...), relying on ``dag.walk_forward``'s existing gray/black cycle detection.

    Params:
        handoff_id (str): Filename stem of the starting handoff under
                          ``state/handoffs/`` (mirrors ``handoff_match.py``'s
                          ``handoff_id`` convention). Authoritative and
                          exclusive if supplied — see ``path`` below.
        path (str):       Absolute or worktree-relative path to the starting
                          handoff file. Used only when ``handoff_id`` is
                          absent — NOT as a fallback when a supplied
                          ``handoff_id`` fails to resolve (Review:
                          code-reviewer, Finding 4).
        repo (str):       Accepted and ignored for path resolution — path
                          resolution uses ``repo_root`` (the router-supplied
                          git common dir), not ``repo``. Negative-spec: claude-klabauter
                          ops resolve paths from the router-supplied repo_root
                          — there is no cross-repo path registry in this op.

    Returns:
        {
            "ancestry": [
                {"path": str, "handoff_id": str, "title": str | None},
                ...
            ],
            "terminated_early": "" | "lineage-cycle" | "missing-link",
        }

    "ancestry" is ordered starting handoff-first (fork), then its origin, then
    that origin's origin, etc. — mirrors ``walk_forward``'s ``orderedPaths``
    first-encounter ordering. Empty list (with ``terminated_early: ""``) is the
    root-handoff case (no ``origin_handoff`` edge) and the "cannot resolve
    starting handoff" case alike — both are safe/empty, not errors.

    Worktree resolution (mirrors handoff_match.py / handoff_children.py):
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - None → return empty ancestry with logged warning (empty is safe)
    """
    if repo_root is None:
        _LOG.warning(
            "handoff.lineage_ancestry: no repo_root resolved — "
            "repo_root arg absent; returning empty ancestry"
        )
        return {"ancestry": [], "terminated_early": ""}

    worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    handoffs_dir = worktree_root / "state" / "handoffs"

    start_path = _resolve_start_path(params, worktree_root)
    if start_path is None:
        _LOG.warning(
            "handoff.lineage_ancestry: could not resolve a starting handoff — "
            "handoff_id=%r path=%r",
            params.get("handoff_id"),
            params.get("path"),
        )
        return {"ancestry": [], "terminated_early": ""}

    # Review: code-reviewer (Finding 5) — plural `handoffs_dir` (this module's
    # convention, mirrors handoff_match.py/handoff_children.py) vs. singular
    # `handoff_dir` (dag.walk_forward's param name) is an intentional naming
    # mismatch across the module boundary, not a typo.
    result = walk_forward(
        str(start_path),
        edge_kinds=_ORIGIN_EDGE_KINDS,
        handoff_dir=str(handoffs_dir),
    )

    ancestry = []
    for abs_path in result["orderedPaths"]:
        meta = result["nodes"].get(abs_path, {})
        title_val = meta.get("title")
        ancestry.append(
            {
                "path": abs_path,
                "handoff_id": os.path.splitext(os.path.basename(abs_path))[0],
                "title": title_val if isinstance(title_val, str) else None,
            }
        )

    return {
        "ancestry": ancestry,
        "terminated_early": result["terminatedEarly"],
    }
