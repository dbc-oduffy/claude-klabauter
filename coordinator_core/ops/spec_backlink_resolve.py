"""
coordinator_core.ops.spec_backlink_resolve — JSON-RPC "spec_backlink.resolve" and
"spec_backlink.rewrite" COMPUTE_ONLY operations.

Purpose: resolve a `pln-<slug>-<hash>` / `dlv-<slug>-<hash>` id (optionally
`<repo>:`-qualified for a named peer repo) to the artifact's CURRENT on-disk
path, whether the artifact is live (`docs/plans/*.md`), archived
(`archive/specs/**/*.md`), or (once C2's backfill lands) a sizing object
(`state/sizings/*.yaml`). This is the seam that makes a spec-backlink citation
survive archival: citing an id instead of a path means the citation never
needs healing when `fleet.archive_completed_plans` moves the file.

Index build: one-shot pass per invocation over `docs/plans/*.md` and
`archive/specs/*/*.md` frontmatter (plus `state/sizings/*.yaml`, whole-document
YAML with no frontmatter fence), building two maps: `plan_id -> path` and
`deliverable_id -> [path, ...]`. The list-valued deliverable_id map is what
makes the AMBIGUITY outcome (N>1 paths for one id) representable — arity is
undefined at resolution time, not just at authoring time (spine
group-stamping can mint one `deliverable_id` onto multiple records). A
per-citation filesystem scan is NOT an option here — that is the "glob over
586 plans" shape the plan's anti-scope names as the naive form that stalls on
the real corpus; this module builds the index exactly once per invocation and
resolves every subsequent lookup against the in-memory maps.

Peer-repo (`<repo>:`-qualified) resolution is LAZY: the peer index is built
only when a queried id actually carries a `<repo>:` prefix, never eagerly,
since most calls this plan drives before C7 (cross-repo citation) lands are
local-only. The peer root is resolved via
`coordinator/bin/lib/coordinator_registry.py::doe_root()` — NOT
`cc_invoke.py::_resolve_claude_klabauter_root()`, which resolves CLAUDE_KLABAUTER_ROOT (this repo)
and has zero peer-repo awareness (see the plan's enrich-once correction).

Typed outcomes: every resolve call returns exactly one of three discriminated
shapes — HIT / MISS / AMBIGUITY — keyed by an `"outcome"` field so a
JSON-RPC caller can branch on all three without inferring shape from absence.
An id that resolves to nothing is a typed MISS, never a best-guess path and
never an exception that reads as a crash. An id that resolves to N>1 records
is a typed AMBIGUITY, distinct from both HIT and MISS — this is what makes
the `pln-`-over-`dlv-` citation preference order (§ PM rulings) an enforced
invariant rather than a merely advisory convention: `spec_backlink.rewrite`
(delegated to `coordinator_core.ops.rewrite_spec_backlinks`, C3's module)
refuses to emit a citation for an AMBIGUITY outcome rather than guessing.

`spec_backlink.rewrite` is a thin delegation: this module only registers the
op name and forwards to C3's rewriter implementation, imported lazily inside
the handler so this module imports cleanly regardless of import order between
the two chunks' files.

Self-registration: importing this module calls register_op("spec_backlink.resolve", ...)
and register_op("spec_backlink.rewrite", ...) as a side-effect. Both are listed
in coordinator_core/ops/__init__.py::_EAGER_OP_MODULES to trigger registration
at start_server() time.

Spec backlink: docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md § C1

Negative-spec (hard-won):
  - Does NOT derive an id from a slug on a resolution miss. `mint_deliverable_id.py`'s
    suffix is `sha1(slug|epoch|pid|random)[:6]` — non-deterministic, not recoverable
    from a slug at read time (PM ruling, § PM rulings). An id-form citation to a
    record with neither a real `plan_id` nor a real `deliverable_id` on disk is a
    typed MISS until C2/C5 close that gap — never a guessed path.
  - Does NOT glob per-citation. The index is built exactly once per invocation
    (module-level `_build_index` call per handler invocation, not per lookup).
  - Does NOT eagerly build the peer-repo (example-doctrine-repo) index. Only a `<repo>:`-prefixed
    query triggers it.
  - Does NOT treat literal YAML `null` or an absent key as a real id — both count as
    "record lacks an id" (mirrors AC2's definition) and are excluded from the index.
  - ZERO git subprocess. Reads on-disk frontmatter/YAML only; COMPUTE_ONLY.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root

logger = logging.getLogger(__name__)

_PLN_PREFIX = "pln-"
_DLV_PREFIX = "dlv-"


# ---------------------------------------------------------------------------
# Typed outcome shapes
# ---------------------------------------------------------------------------


def _hit(path: str, queried_id: str) -> dict:
    return {"outcome": "hit", "queried_id": queried_id, "path": path}


def _miss(queried_id: str) -> dict:
    return {"outcome": "miss", "queried_id": queried_id, "path": None}


def _ambiguity(paths: List[str], queried_id: str) -> dict:
    return {"outcome": "ambiguity", "queried_id": queried_id, "path": None, "candidates": paths}


# ---------------------------------------------------------------------------
# Sizing-object reader (whole-document YAML, no frontmatter fence)
# ---------------------------------------------------------------------------


def _read_sizing_yaml(path: Path) -> dict:
    """Read a `state/sizings/*.yaml` record. Returns {} on any error.

    Sizings are whole-document YAML (no `---` frontmatter fence) — reading
    them via `_read_meta`'s fence-scanning parser would silently return {}
    (correctly, but for the wrong reason: it never finds a closing fence).
    This reader is the whole-document-YAML twin, kept local to this module
    rather than importing `backfill_deliverable_spine.read_yaml_document` so
    this module has no runtime coupling to a peer chunk's concurrently-edited
    file — same "second parser class" distinction that module's own docstring
    names, re-derived minimally here.
    """
    try:
        import yaml
    except ImportError:
        logger.debug("spec_backlink_resolve: PyYAML unavailable; cannot read %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        logger.debug("spec_backlink_resolve: could not read %s: %s", path, exc)
        return {}
    except Exception as exc:  # noqa: BLE001 — malformed YAML degrades to empty, not a crash
        logger.debug("spec_backlink_resolve: could not parse %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _real_id(value: object) -> Optional[str]:
    """Return `value` iff it is a non-empty string not equal to a literal YAML null token.

    Mirrors AC2's "carries a real id" definition: literal `null`/`~` and an
    absent key both count as lacking one. `_read_meta`/`yaml.safe_load` already
    parse a bare YAML `null` to Python `None`, so the only extra case handled
    here is a quoted literal string `"null"`/`"~"` surviving as a str.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in {"null", "~"}:
        return None
    return stripped


# ---------------------------------------------------------------------------
# Index build — one shot per invocation
# ---------------------------------------------------------------------------


class _BacklinkIndex:
    """In-memory id -> path index. Built once per resolve call, never cached
    across calls (the plan/archive/sizing corpus can change between calls;
    this op is COMPUTE_ONLY and cheap enough per-invocation per the plan's
    own "build once per invocation" requirement — not "once per process").
    """

    __slots__ = ("plan_id_to_path", "deliverable_id_to_paths")

    def __init__(self) -> None:
        self.plan_id_to_path: Dict[str, str] = {}
        self.deliverable_id_to_paths: Dict[str, List[str]] = {}

    def _add(self, plan_id: Optional[str], deliverable_id: Optional[str], path: str) -> None:
        if plan_id is not None:
            # A collision here (two records sharing one plan_id) is not part
            # of this plan's measured corpus; last-write-wins is acceptable
            # since plan_id is documented as per-file identity, never shared.
            self.plan_id_to_path[plan_id] = path
        if deliverable_id is not None:
            self.deliverable_id_to_paths.setdefault(deliverable_id, []).append(path)


def _index_markdown_dir(index: _BacklinkIndex, base_dir: Path) -> None:
    """Index every *.md file directly under base_dir (flat, non-recursive)."""
    if not base_dir.is_dir():
        return
    try:
        entries = list(base_dir.iterdir())
    except OSError as exc:
        logger.warning("spec_backlink_resolve: cannot scan %s — %s", base_dir, exc)
        return
    for path in entries:
        if path.suffix == ".md" and path.is_file():
            _index_one_markdown(index, path)


def _index_markdown_tree(index: _BacklinkIndex, base_dir: Path) -> None:
    """Index every *.md file under base_dir, recursively (archive/specs/YYYY-MM/*.md)."""
    if not base_dir.is_dir():
        return
    walk_errors: List[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(base_dir, onerror=walk_errors.append):
        for fn in filenames:
            if fn.endswith(".md"):
                path = Path(dirpath) / fn
                if path.is_file():
                    _index_one_markdown(index, path)
    if walk_errors:
        for exc in walk_errors:
            logger.warning(
                "spec_backlink_resolve: cannot walk %s — %s",
                getattr(exc, "filename", base_dir),
                exc,
            )


def _index_one_markdown(index: _BacklinkIndex, path: Path) -> None:
    try:
        fm = _read_meta(str(path))
    except Exception as exc:  # noqa: BLE001 — parity with deliverable_rollup's quarantine
        logger.debug("spec_backlink_resolve: could not read frontmatter from %s: %s", path, exc)
        return
    if not fm:
        return
    plan_id = _real_id(fm.get("plan_id"))
    deliverable_id = _real_id(fm.get("deliverable_id"))
    if plan_id is None and deliverable_id is None:
        return
    index._add(plan_id, deliverable_id, str(path))


def _index_sizings_dir(index: _BacklinkIndex, base_dir: Path) -> None:
    """Index every *.yaml file directly under base_dir (state/sizings/, flat)."""
    if not base_dir.is_dir():
        return
    try:
        entries = list(base_dir.iterdir())
    except OSError as exc:
        logger.warning("spec_backlink_resolve: cannot scan %s — %s", base_dir, exc)
        return
    for path in entries:
        if path.suffix in (".yaml", ".yml") and path.is_file():
            doc = _read_sizing_yaml(path)
            if not doc:
                continue
            deliverable_id = _real_id(doc.get("deliverable_id"))
            plan_id = _real_id(doc.get("plan_id"))
            if plan_id is None and deliverable_id is None:
                continue
            index._add(plan_id, deliverable_id, str(path))


def build_index(worktree_root: Path) -> _BacklinkIndex:
    """Build the local (this-repo) id -> path index, one shot.

    Scans docs/plans/*.md (flat), archive/specs/*/*.md (recursive), and
    state/sizings/*.yaml (flat, whole-document YAML). Building the index
    once per invocation — never per citation lookup — is the requirement
    the plan's own anti-scope names explicitly.
    """
    index = _BacklinkIndex()
    _index_markdown_dir(index, worktree_root / "docs" / "plans")
    _index_markdown_tree(index, worktree_root / "archive" / "specs")
    _index_sizings_dir(index, worktree_root / "state" / "sizings")
    return index


def _doe_root_path() -> Optional[Path]:
    """Resolve the example-doctrine-repo peer repo root via the canonical resolver.

    Reuses coordinator/bin/lib/coordinator_registry.py::doe_root() rather
    than re-deriving a ladder — see this module's docstring for why
    cc_invoke.py::_resolve_claude_klabauter_root() is NOT the right function (it
    resolves CLAUDE_KLABAUTER_ROOT, this repo, with zero peer-repo awareness).
    """
    try:
        from coordinator.bin.lib.coordinator_registry import doe_root
    except ImportError as exc:
        logger.warning("spec_backlink_resolve: could not import doe_root(): %s", exc)
        return None
    try:
        root = doe_root()
    except Exception as exc:  # noqa: BLE001 — a peer-root resolution failure is a typed miss
        logger.warning("spec_backlink_resolve: doe_root() failed: %s", exc)
        return None
    if not root:
        return None
    return Path(root)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_id(index: _BacklinkIndex, queried_id: str) -> dict:
    """Resolve a bare (non peer-qualified) `pln-*`/`dlv-*` id against `index`.

    Dispatches on prefix. An id with neither prefix, or one that resolves to
    nothing, is a typed MISS. An id resolving to N>1 paths is a typed
    AMBIGUITY. Never raises on an unknown id.
    """
    if queried_id.startswith(_PLN_PREFIX):
        path = index.plan_id_to_path.get(queried_id)
        if path is None:
            return _miss(queried_id)
        return _hit(path, queried_id)
    if queried_id.startswith(_DLV_PREFIX):
        paths = index.deliverable_id_to_paths.get(queried_id)
        if not paths:
            return _miss(queried_id)
        if len(paths) > 1:
            return _ambiguity(list(paths), queried_id)
        return _hit(paths[0], queried_id)
    return _miss(queried_id)


def resolve(worktree_root: Path, queried_id: str) -> dict:
    """Resolve `queried_id` (optionally `<repo>:`-qualified) to a typed outcome.

    `worktree_root` is THIS repo's worktree root (already derived by the
    caller via main_worktree_root(repo_root)). A `<repo>:`-qualified id
    triggers a lazy peer-repo index build rooted at doe_root() instead of
    scanning worktree_root — the peer index is never built for a local-only
    query.
    """
    queried_id = (queried_id or "").strip()
    if not queried_id:
        return _miss(queried_id)

    if ":" in queried_id:
        _repo, _sep, bare_id = queried_id.partition(":")
        peer_root = _doe_root_path()
        if peer_root is None or not peer_root.is_dir():
            return _miss(queried_id)
        peer_index = build_index(peer_root)
        outcome = resolve_id(peer_index, bare_id)
        # Re-key the outcome under the originally-queried (qualified) id so the
        # caller sees back exactly what it asked for.
        outcome["queried_id"] = queried_id
        return outcome

    local_index = build_index(worktree_root)
    return resolve_id(local_index, queried_id)


# ---------------------------------------------------------------------------
# JSON-RPC handlers
# ---------------------------------------------------------------------------


@register_op("spec_backlink.resolve")
def _resolve_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "spec_backlink.resolve" handler — COMPUTE_ONLY, sync.

    Params:
        id (str) — a `pln-<slug>-<hash>` or `dlv-<slug>-<hash>` id, optionally
                   `<repo>:`-qualified for a named peer repo (e.g. Example-doctrine-repo).

    Returns one of three typed outcome shapes:
        {"outcome": "hit",       "queried_id": ..., "path": "<str>"}
        {"outcome": "miss",      "queried_id": ..., "path": None}
        {"outcome": "ambiguity", "queried_id": ..., "path": None, "candidates": [...]}

    repo_root is the .git common dir (common_dir keying, per op_scopes.py);
    the worktree is derived via main_worktree_root(repo_root), never from
    repo_root directly (scanning .git/docs/plans instead of <worktree>/docs/plans
    would be a silent scan-miss).
    """
    queried_id: str = (params.get("id") or "").strip()
    if repo_root is None:
        logger.warning(
            "spec_backlink.resolve: repo_root is None — cannot derive worktree; "
            "returning typed miss."
        )
        return _miss(queried_id)
    worktree_root = main_worktree_root(repo_root)
    return resolve(worktree_root, queried_id)


@register_op("spec_backlink.rewrite")
def _rewrite_handler(params: dict, repo_root: Optional[Path] = None):
    """JSON-RPC "spec_backlink.rewrite" handler — thin delegation to C3's rewriter.

    This module only registers the op name; the actual rewrite logic
    (converting a path-form citation to id-form in place, per the
    `pln-`-over-`dlv-` preference order) lives in
    `coordinator_core.ops.rewrite_spec_backlinks` (C3's module). Imported
    lazily inside this handler so this module imports cleanly regardless of
    whether C3's file has landed yet.
    """
    from coordinator_core.ops.rewrite_spec_backlinks import rewrite

    return rewrite(params, repo_root=repo_root)
