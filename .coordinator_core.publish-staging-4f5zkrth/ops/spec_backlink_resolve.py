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
YAML with no frontmatter fence), building two maps: `plan_id -> [path, ...]`
and `deliverable_id -> [path, ...]`. Both list-valued (Review: code-reviewer
P3 — `plan_id` was previously a single str with last-write-wins on
collision, asymmetric with `deliverable_id`'s typed AMBIGUITY) — this is what
makes the AMBIGUITY outcome (N>1 paths for one id) representable for either
id kind — arity is undefined at resolution time, not just at authoring time
(spine group-stamping can mint one `deliverable_id` onto multiple records,
and a copy-pasted `plan_id` is a corpus defect this resolver refuses to
paper over rather than a case it can rule out). A
per-citation filesystem scan is NOT an option here — that is the "glob over
586 plans" shape the plan's anti-scope names as the naive form that stalls on
the real corpus; this module builds the index exactly once per invocation and
resolves every subsequent lookup against the in-memory maps.

Peer-repo (`<repo>:`-qualified) resolution is LAZY: the peer index is built
only when a queried id actually carries a `<repo>:` prefix, never eagerly,
since most calls this plan drives before C7 (cross-repo citation) lands are
local-only. The peer root is resolved via
`coordinator/bin/lib/coordinator_registry.py::doe_root()` — NOT
`cc_invoke.py::_resolve_makima_root()`, which resolves the engine root (this
repo, dispatch axis) and has zero peer-repo awareness (see the plan's
enrich-once correction).

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

Spec backlink: pln-spec-backlinks-cite-a-stable-d-451b3e § C1

Negative-spec (hard-won):
  - Does NOT derive an id from a slug on a resolution miss. `mint_deliverable_id.py`'s
    suffix is `sha1(slug|epoch|pid|random)[:6]` — non-deterministic, not recoverable
    from a slug at read time (PM ruling, § PM rulings). An id-form citation to a
    record with neither a real `plan_id` nor a real `deliverable_id` on disk is a
    typed MISS until C2/C5 close that gap — never a guessed path.
  - Does NOT glob per-citation. The index is built exactly once per invocation
    (module-level `_build_index` call per handler invocation, not per lookup).
  - Does NOT eagerly build the peer-repo (DoE-claude) index. Only a `<repo>:`-prefixed
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
from coordinator_core.ops.deliverable_rollup import (
    RESOLVABLE_ARTIFACT_ROOTS,
    SIZINGS_ONLY_ROOT,
)
from coordinator_core.ops.fleet._common import main_worktree_root

logger = logging.getLogger(__name__)

_PLN_PREFIX = "pln-"
_DLV_PREFIX = "dlv-"

# Review: code-reviewer P2 — the only <repo>: qualifier resolve() accepts.
# Mirrors rewrite_spec_backlinks._PEER_REPO_NAME, the fixed literal the emit
# side ever produces; a queried_id carrying any OTHER qualifier is refused
# as a typed miss rather than silently routed to the DoE-claude peer index.
_RECOGNIZED_PEER_REPO = "DoE-claude"


# ---------------------------------------------------------------------------
# Typed outcome shapes
# ---------------------------------------------------------------------------


def _hit(path: str, queried_id: str) -> dict:
    return {"outcome": "hit", "queried_id": queried_id, "path": path}


def _miss(queried_id: str, reason: Optional[str] = None) -> dict:
    outcome = {"outcome": "miss", "queried_id": queried_id, "path": None}
    if reason is not None:
        outcome["reason"] = reason
    return outcome


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

    __slots__ = (
        "plan_id_to_paths",
        "deliverable_id_to_paths",
        "path_to_ids",
        "basename_to_paths",
    )

    def __init__(self) -> None:
        self.plan_id_to_paths: Dict[str, List[str]] = {}
        self.deliverable_id_to_paths: Dict[str, List[str]] = {}
        # Inverse of the two maps above: path -> {"plan_id": ..., "deliverable_id": ...}.
        # Only populated for records carrying at least one real id — this is
        # the seam `rewrite_spec_backlinks.resolve_citation` (C3) needs: given
        # a cited docs/plans/...md PATH, get the record's ids, not the other
        # direction id -> path this index otherwise serves.
        self.path_to_ids: Dict[str, Dict[str, Optional[str]]] = {}
        # basename (with .md extension, e.g. "2026-07-10-qsub-01-....md") ->
        # [abs path, ...]. The join key `assert_no_dangling_plan_backlinks.py`
        # already uses (`os.path.basename(rel)`) to match a citation's dated
        # filename against its archived twin -- reused here verbatim (not
        # re-derived as a looser stem/slug match) so a cited docs/plans/<x>.md
        # whose record now lives at archive/specs/<YYYY-MM>/<x>.md still
        # resolves, and the converse (an archive-path citation whose record
        # has moved back to docs/plans/) resolves too. Only populated for
        # records carrying at least one real id, mirroring `path_to_ids`.
        # More than one path sharing a basename is the existing typed
        # AMBIGUITY outcome, never a silent pick.
        self.basename_to_paths: Dict[str, List[str]] = {}

    def _add(self, plan_id: Optional[str], deliverable_id: Optional[str], path: str) -> None:
        if plan_id is not None:
            # Review: code-reviewer P3 — list-valued like deliverable_id_to_paths,
            # not a single str with last-write-wins. plan_id is documented as
            # per-file identity, never shared, but a genuine duplicate/copy-
            # pasted plan_id must surface as a typed AMBIGUITY in resolve_id(),
            # not silently resolve to whichever path an unordered directory
            # traversal happens to visit last — refuse rather than guess,
            # matching deliverable_id's own collision handling.
            self.plan_id_to_paths.setdefault(plan_id, []).append(path)
        if deliverable_id is not None:
            self.deliverable_id_to_paths.setdefault(deliverable_id, []).append(path)
        if plan_id is not None or deliverable_id is not None:
            self.path_to_ids[path] = {"plan_id": plan_id, "deliverable_id": deliverable_id}
            basename = os.path.basename(path)
            self.basename_to_paths.setdefault(basename, []).append(path)


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

    Scans the shared `RESOLVABLE_ARTIFACT_ROOTS` five-root surface (C10b,
    docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md) —
    docs/plans/*.md (flat), state/handoffs/*.md (flat),
    archive/handoffs/**/*.md (recursive), archive/specs/**/*.md (recursive),
    state/sizings/*.yaml (flat, whole-document YAML, `SIZINGS_ONLY_ROOT`) —
    imported from `coordinator_core.ops.deliverable_rollup` rather than
    hard-coded here a second time. `RESOLVABLE_ARTIFACT_ROOTS` already
    includes `SIZINGS_ONLY_ROOT` as its 5th entry, so the flat-root loop
    below dispatches to `_index_sizings_dir` for that one root by identity
    (mirroring `deliverable_rollup._scan_artifacts_by_deliverable_id`'s
    `is_sizings_root` branch) rather than scanning it a second time via a
    standalone call — a prior version of this function did both, which
    silently double-scanned state/sizings/ (harmless only because
    `_index_markdown_dir` filters to `.md` and sizings are `.yaml`).
    Building the index once per invocation — never per citation lookup — is
    the requirement the plan's own anti-scope names explicitly.
    """
    index = _BacklinkIndex()
    sizings_parts, _sizings_kind = SIZINGS_ONLY_ROOT
    sizings_dir = worktree_root.joinpath(*sizings_parts)
    for parts, kind in RESOLVABLE_ARTIFACT_ROOTS:
        base_dir = worktree_root.joinpath(*parts)
        if kind != "flat":
            _index_markdown_tree(index, base_dir)
        elif base_dir == sizings_dir:
            _index_sizings_dir(index, base_dir)
        else:
            _index_markdown_dir(index, base_dir)
    return index


def _doe_root_path() -> Optional[Path]:
    """Resolve the DoE-claude peer repo root via the canonical resolver.

    Reuses coordinator/bin/lib/coordinator_registry.py::doe_root() rather
    than re-deriving a ladder — see this module's docstring for why
    cc_invoke.py::_resolve_makima_root() is NOT the right function (it
    resolves the engine root, this repo, with zero peer-repo awareness).
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
        paths = index.plan_id_to_paths.get(queried_id)
        if not paths:
            return _miss(queried_id)
        if len(paths) > 1:
            return _ambiguity(list(paths), queried_id)
        return _hit(paths[0], queried_id)
    if queried_id.startswith(_DLV_PREFIX):
        paths = index.deliverable_id_to_paths.get(queried_id)
        if not paths:
            return _miss(queried_id)
        if len(paths) > 1:
            return _ambiguity(list(paths), queried_id)
        return _hit(paths[0], queried_id)
    return _miss(queried_id)


def _path_hit(cited_path: str, plan_id: Optional[str], deliverable_id: Optional[str]) -> dict:
    return {
        "outcome": "hit",
        "cited_path": cited_path,
        "plan_id": plan_id,
        "deliverable_id": deliverable_id,
    }


def _path_miss(cited_path: str) -> dict:
    return {"outcome": "miss", "cited_path": cited_path, "plan_id": None, "deliverable_id": None}


def _path_ambiguity(cited_path: str, paths: List[str]) -> dict:
    return {
        "outcome": "ambiguity",
        "cited_path": cited_path,
        "plan_id": None,
        "deliverable_id": None,
        "candidates": paths,
    }


def resolve_path_with_index(index: _BacklinkIndex, worktree_root: Path, cited_path: str) -> dict:
    """Resolve a cited `docs/plans/...md` PATH against an ALREADY-BUILT
    `index` (see `resolve_path` for the outcome shape and the id-recovery
    rationale). This is the multi-lookup twin: a caller resolving many
    citations against the same worktree_root in one batch (C3/C4's per-file
    fan-out) builds `index` exactly once via `build_index(worktree_root)` and
    threads it through every citation lookup here, rather than each lookup
    re-walking the corpus — the "glob over 586 plans x 2368 citations" shape
    the plan's anti-scope names is what this seam exists to avoid.
    """
    cited_path = (cited_path or "").strip()
    if not cited_path:
        return _path_miss(cited_path)
    abs_key = str(Path(worktree_root) / cited_path)
    ids = index.path_to_ids.get(abs_key)
    if ids is not None:
        return _path_hit(cited_path, ids.get("plan_id"), ids.get("deliverable_id"))

    # Exact-path miss: the cited path's record may have been git-mv'd by
    # fleet.archive_completed_plans (docs/plans/ -> archive/specs/YYYY-MM/),
    # or the converse. Fall back to a basename join against the SAME
    # already-built index -- no second filesystem pass.
    basename = os.path.basename(cited_path)
    candidates = index.basename_to_paths.get(basename)
    if not candidates:
        return _path_miss(cited_path)
    if len(candidates) > 1:
        return _path_ambiguity(cited_path, list(candidates))
    twin_path = candidates[0]
    twin_ids = index.path_to_ids[twin_path]
    return _path_hit(cited_path, twin_ids.get("plan_id"), twin_ids.get("deliverable_id"))


def resolve_path(worktree_root: Path, cited_path: str) -> dict:
    """Resolve a cited `docs/plans/...md` PATH to the record's `plan_id` /
    `deliverable_id` — the inverse of `resolve()` (id -> path). This is the
    seam `rewrite_spec_backlinks.resolve_citation` (C3) needs: it has a
    citation's PATH, not an id, and must recover whichever real ids that
    record carries so it can emit the `pln-`/`dlv-` preferred form.

    Typed outcomes mirror `resolve()`'s: a path matching no indexed record
    (wrong path, or a record with neither id present) is a typed MISS, never
    a guess. A path matching an indexed record is a typed HIT carrying
    `plan_id`/`deliverable_id` (either may be None if that record only has
    the other).

    Single-shot convenience wrapper: builds a fresh index for this one call.
    A caller resolving MANY citations in one run (a batch/multi-file rewrite)
    should build the index once via `build_index(worktree_root)` and call
    `resolve_path_with_index(index, worktree_root, cited_path)` per lookup
    instead — see that function's docstring.
    """
    cited_path = (cited_path or "").strip()
    if not cited_path:
        return _path_miss(cited_path)
    index = build_index(worktree_root)
    return resolve_path_with_index(index, worktree_root, cited_path)


def resolve(worktree_root: Path, queried_id: str) -> dict:
    """Resolve `queried_id` (optionally `<repo>:`-qualified) to a typed outcome.

    `worktree_root` is THIS repo's worktree root (already derived by the
    caller via main_worktree_root(repo_root)). A `<repo>:`-qualified id
    triggers a lazy peer-repo index build rooted at doe_root() instead of
    scanning worktree_root — the peer index is never built for a local-only
    query.

    The `<repo>:` qualifier is validated against `_RECOGNIZED_PEER_REPO`
    (Review: code-reviewer P2): an unrecognized qualifier (typo, wrong case,
    a repo this resolver has no peer index for) is a typed miss carrying
    `reason="unrecognized_repo_qualifier"`, never silently routed to the
    DoE-claude peer index — refuse rather than guess, matching the emit
    side, which only ever produces the fixed `"DoE-claude:"` literal.
    """
    queried_id = (queried_id or "").strip()
    if not queried_id:
        return _miss(queried_id)

    if ":" in queried_id:
        repo, _sep, bare_id = queried_id.partition(":")
        if repo != _RECOGNIZED_PEER_REPO:
            return _miss(queried_id, reason="unrecognized_repo_qualifier")
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
                   `<repo>:`-qualified for a named peer repo (e.g. DoE-claude).

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

    Params:
        paths (list[str]) — the explicit file list to rewrite. The rewriter
                            never walks the filesystem itself; the caller
                            supplies the scope.

    Returns C3's aggregate reported set:
        {"rewritten": {<path>: [<cited_path>, ...]},
         "unresolvable": {<path>: [<cited_path>, ...]}}

    repo_root is the .git common dir (common_dir keying, per op_scopes.py);
    the worktree is derived via main_worktree_root(repo_root), matching
    `spec_backlink.resolve` above — resolving against .git/docs/plans rather
    than <worktree>/docs/plans would make every citation a silent miss.
    """
    from coordinator_core.ops.rewrite_spec_backlinks import rewrite_spec_backlinks

    paths = params.get("paths") or []
    if repo_root is None:
        logger.warning(
            "spec_backlink.rewrite: repo_root is None — cannot derive worktree; "
            "returning an empty report rather than rewriting against a wrong root."
        )
        return {"rewritten": {}, "unresolvable": {}}
    worktree_root = main_worktree_root(repo_root)
    return rewrite_spec_backlinks(paths, worktree_root=worktree_root)
