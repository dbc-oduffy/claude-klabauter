"""coordinator_core.data_root — native sibling of
coordinator/bin/lib/coordinator_data_root.py's `data_root()` resolver.

Purpose: coordinator_core (the engine plane) has callers that need a coordinator
DATA dir (schemas/, templates/, snippets/, docs/) that stayed content-resident under
DR-047 (contract/data lives with DoE, engine with claude-klabauter) after the 2026-07-22
executable-surface migration. `coordinator/bin/lib/coordinator_data_root.py`
already solves this for bin/ CLIs, but coordinator_core cannot import that module
— `coordinator/bin/lib/` is not on coordinator_core's import path, and bolting a
`sys.path` hack onto coordinator_core to reach sideways into a sibling tree's
`bin/lib/` would be exactly the kind of fragile cross-tree coupling the split
(DR-047) exists to avoid. This module provides the SAME two-rung contract
(co-located, then content-resident) as a coordinator_core-native function, so
coordinator_core callers get identical resolution semantics without the reach.

Two live layouts (mirrors coordinator_data_root.py's docstring):
  1. Co-located    — the data dir sits under a `coordinator/` directory
                     beside coordinator_core's own repo root (the pre-
                     migration DoE layout, and any OSS install that ships
                     both halves together). Free, no registration.
  2. Split-repo    — coordinator_core lives in claude-klabauter while the data
                     dir stayed in the content repo. Resolve the content root via
                     `coordinator_core.content_root.read_content_root()`.

Rung 1 first so the co-located case costs nothing and needs no registration.

Rung 2 here resolves the content root through
`coordinator_core.content_root.read_content_root()` — the single content-root
resolver (registry `repos.content_root`, pointer files, installed plugin root).

Negative-spec: this module does NOT reimplement `read_content_root()`'s
resolution chain — that chain lives in exactly one place, and this module calls
it rather than duplicating it.

Public API:
    data_root(dir_name: str) -> Path
        Resolve one of "snippets", "schemas", "templates", "docs" (or any other
        coordinator data-dir name) to its absolute, existing directory Path.
        Raises RuntimeError, naming the dir and both rungs tried, if neither
        rung resolves to an existing directory. Never returns a path that
        doesn't exist; never silently falls back to a wrong location.

Spec backlink: cross-repo/archive/2026-07-22-claude-central-em-executable-surface-migrated-and-76-op-ask.md
               (the originating memo; in cross-repo/inbox/ until the boot sweep moves it)
DR backlink:   docs/decisions/DR-047-content-engine-boundary-redraw-contract-vs-e.md (DoE-side)
Sibling:       coordinator/bin/lib/coordinator_data_root.py (bin/-side twin; the two
               modules MUST stay behaviorally consistent for the same dir_name,
               modulo the env-override-name divergence documented above)
"""
from __future__ import annotations

from pathlib import Path

try:
    from coordinator_core.content_root import read_content_root
except ImportError:  # pragma: no cover - exercised by the publish pre-swap gate
    # The publish pre-swap FUNCTION gate imports this file as a FLAT, top-level
    # `data_root` module in a hermetic, OSS-shaped subprocess whose PYTHONPATH is
    # the staging dir itself (`coordinator/bin/publish.py ::
    # _function_gate_modules_and_search_paths_for_repo_root` strips the
    # `coordinator_core` prefix for a row staged at its own root). There is no
    # `coordinator_core` package to import through there BY CONSTRUCTION, so a
    # module-level import of the package from inside one of its own members can
    # never satisfy the gate, and the engine row cannot publish while one stands.
    #
    # The module-level BINDING is load-bearing and is preserved: every test in
    # `coordinator_core/test_data_root.py` monkeypatches this module attribute,
    # and `_resolve_content_root()` below reads the global at call time so that keeps
    # working. Only the hard import-time FAILURE is removed; a real resolution
    # under a real package still imports here, at import time, unchanged.
    read_content_root = None  # type: ignore[assignment]


def _resolve_content_root():
    resolver = read_content_root
    if resolver is None:
        from coordinator_core.content_root import (  # noqa: PLC0415
            read_content_root as resolver,
        )
    return resolver()


def _colocated_root() -> Path:
    """The coordinator root this module's rung-1 base resolves to.

    coordinator_core/data_root.py -> parent.parent == the claude-klabauter repo root
    (file -> coordinator_core/ -> repo root), then `/ "coordinator"` to land
    on the SAME `<coordinator-root>/<dir_name>` namespace
    coordinator_data_root.py's `_colocated_root()` resolves (that module walks
    up from coordinator/bin/lib/ to coordinator/ — `parents[2]` instead of
    `parent.parent`, because it ships one directory deeper down its own tree,
    but both MUST land on the same `coordinator/` directory).

    Negative-spec (bug this fixes, 2026-07-22): this previously returned the
    claude-klabauter REPO root bare (`parent.parent`, no `/ "coordinator"` suffix), so
    rung 1 probed `<repo>/<dir_name>` instead of `<repo>/coordinator/<dir_name>`
    — a DIFFERENT namespace than the bin/lib twin, which resolves
    `<repo>/coordinator/<dir_name>`. For `dir_name="docs"` that silently
    returned claude-klabauter's OWN `docs/` tree (which exists) instead of ever
    consulting coordinator-content-repo's `coordinator/docs/` — no error, just the wrong
    answer. See `coordinator_core/test_data_root.py`'s parity test.
    """
    return Path(__file__).resolve().parent.parent / "coordinator"


def data_root(dir_name: str) -> Path:
    """Resolve `dir_name` (e.g. "snippets", "schemas", "templates", "docs") to
    its absolute, existing directory Path.

    Resolution chain (two-rung, co-located -> content-resident):
      1. Co-located — `<coordinator-root>/<dir_name>`, where `<coordinator-
         root>` is computed identically to `_colocated_root()` above. Free,
         no registration, wins whenever both halves ship together.
      2. Content-resident — `<read_content_root()>/coordinator/<dir_name>`
         (private layout), falling back to `<read_content_root()>/<dir_name>`
         (OSS-flat layout, F2 fix 2026-08-08 -- see below), delegating the
         registry/pointer/plugin-root resolution to
         `coordinator_core.content_root.read_content_root()`
         (never reimplemented here — see module docstring).

    Raises RuntimeError, naming `dir_name` and all candidate paths tried
    (or the content-root resolution failure reason), if neither rung resolves to an
    existing directory. Never returns a path that doesn't exist.
    """
    colocated = _colocated_root() / dir_name
    if colocated.is_dir():
        return colocated

    content = _resolve_content_root()
    if not content:
        raise RuntimeError(
            f"coordinator_core.data_root: cannot resolve data dir {dir_name!r}. "
            f"Rung 1 (co-located) tried: {colocated} (not found). "
            "Rung 2 (content-resident) failed: read_content_root() resolved "
            "nothing (registry repos.content_root, content-root pointer files "
            "and the installed plugin root all unresolved)."
        )

    private_candidate = Path(content) / "coordinator" / dir_name
    if private_candidate.is_dir():
        return private_candidate

    flat_candidate = Path(content) / dir_name
    if flat_candidate.is_dir():
        return flat_candidate

    raise RuntimeError(
        f"coordinator_core.data_root: cannot resolve data dir {dir_name!r}. "
        f"Rung 1 (co-located) tried: {colocated} (not found). "
        f"Rung 2 (content-resident) tried: {private_candidate} (private layout, not found), "
        f"{flat_candidate} (OSS-flat layout, not found)."
    )

# `content_root_for` and its marker are
# PURE, no-intra-package-import primitives, moved to a leaf module so any
# `coordinator_core` module can import them at module level without risking
# the cycle that used to force `resolve_coordinator_clone.py` to hand-expand
# the join instead (findings 4, 8). Re-exported here so every existing
# `from coordinator_core.data_root import content_root_for` (and
# `FLAT_CONTENT_ROOT_MARKER`) keeps working unchanged. See
# `coordinator_core/_content_root_primitive.py` for the implementation and the
# full rationale.
#
# `resolved_content_root()` (the
# no-arg convenience wrapper around this) is deleted: it had zero call sites
# anywhere in the diff that introduced it, in either twin.
from coordinator_core._content_root_primitive import (  # noqa: E402,F401
    FLAT_CONTENT_ROOT_MARKER,
    content_root_for,
    content_root_or_private,
)
