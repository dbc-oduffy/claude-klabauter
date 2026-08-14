"""coordinator_core.data_root — native sibling of
coordinator/bin/lib/coordinator_data_root.py's `data_root()` resolver.

Purpose: coordinator_core (the engine plane) has callers that need a coordinator
DATA dir (schemas/, templates/, snippets/, docs/) that stayed DoE-resident under
DR-047 (contract/data lives with DoE, engine with claude-klabauter) after the 2026-07-22
executable-surface migration. `coordinator/bin/lib/coordinator_data_root.py`
already solves this for bin/ CLIs, but coordinator_core cannot import that module
— `coordinator/bin/lib/` is not on coordinator_core's import path, and bolting a
`sys.path` hack onto coordinator_core to reach sideways into a sibling tree's
`bin/lib/` would be exactly the kind of fragile cross-tree coupling the split
(DR-047) exists to avoid. This module provides the SAME two-rung contract
(co-located, then DoE-resident) as a coordinator_core-native function, so
coordinator_core callers get identical resolution semantics without the reach.

Two live layouts (mirrors coordinator_data_root.py's docstring):
  1. Co-located    — the data dir sits under a `coordinator/` directory
                     beside coordinator_core's own repo root (the pre-
                     migration DoE layout, and any OSS install that ships
                     both halves together). Free, no registration.
  2. Split-repo    — coordinator_core lives in claude-klabauter while the data
                     dir stayed in DoE-claude. Resolve the DoE root via
                     `coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`.

Rung 1 first so the co-located case costs nothing and needs no registration.

Deliberate divergence from coordinator_data_root.py, not an oversight: rung 2
here delegates to `coordinator_doe_root()` — the already-ratified, richer
full-ladder DoE-root resolver (REPO_DOE_CLAUDE env override -> machine-local
registry `repos.doe_claude` (canonical) -> `plugin.mirrors.coordinator-claude.
live_path` fallback -> the native `resolve_coordinator_clone` port, which itself
covers the `.doe-root` pointer file and flat-layout rungs) that 9+ other
coordinator_core callers already bind to — NOT
`coordinator_core.doe_root_pointer.read_doe_root_pointer()` (a narrower 3-rung
resolver: registry -> durable-file -> legacy-file, with no env-var rung of its
own) and NOT a re-implementation of coordinator_data_root.py's own
`DOE_ROOT`-env + `coordinator_registry.doe_root()` chain. Re-deriving that
DOE_ROOT-env check here would mint a SECOND override name for the same concept
inside one process — worse than the cross-file naming divergence it would
"fix". `REPO_DOE_CLAUDE` is coordinator_core's own already-established
override convention (see `coordinator_core.ops.coordinator_doe_root`'s
docstring and its existing importers); this module reuses it rather than
adding a competing one.

Negative-spec: this module does NOT reimplement `coordinator_doe_root()`'s
resolution chain (env -> registry -> mirror fallback -> clone-root port) —
that chain lives in exactly one place, and this module calls it rather than
duplicating it.

Public API:
    data_root(dir_name: str) -> Path
        Resolve one of "snippets", "schemas", "templates", "docs" (or any other
        coordinator data-dir name) to its absolute, existing directory Path.
        Raises RuntimeError, naming the dir and both rungs tried, if neither
        rung resolves to an existing directory. Never returns a path that
        doesn't exist; never silently falls back to a wrong location.

Spec backlink: cross-repo/archive/2026-07-22-claude-central-em-executable-surface-migrated-and-76-op-ask.md
               (the originating memo; in cross-repo/inbox/ until the boot sweep moves it)
DR backlink:   docs/decisions/DR-047-doe-claude-klabauter-boundary-redraw-contract-vs-e.md (DoE-side)
Sibling:       coordinator/bin/lib/coordinator_data_root.py (bin/-side twin; the two
               modules MUST stay behaviorally consistent for the same dir_name,
               modulo the env-override-name divergence documented above)
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root


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
    consulting DoE-claude's `coordinator/docs/` — no error, just the wrong
    answer. See `coordinator_core/test_data_root.py`'s parity test.
    """
    return Path(__file__).resolve().parent.parent / "coordinator"


def data_root(dir_name: str) -> Path:
    """Resolve `dir_name` (e.g. "snippets", "schemas", "templates", "docs") to
    its absolute, existing directory Path.

    Resolution chain (two-rung, co-located -> DoE-resident):
      1. Co-located — `<coordinator-root>/<dir_name>`, where `<coordinator-
         root>` is computed identically to `_colocated_root()` above. Free,
         no registration, wins whenever both halves ship together.
      2. DoE-resident — `<coordinator_doe_root()>/coordinator/<dir_name>`
         (private layout), falling back to `<coordinator_doe_root()>/<dir_name>`
         (OSS-flat layout, F2 fix 2026-08-08 -- see below), delegating the
         REPO_DOE_CLAUDE/registry/mirror/clone-root resolution to
         `coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`
         (never reimplemented here — see module docstring).

    Raises RuntimeError, naming `dir_name` and all candidate paths tried
    (or the DoE-resolution failure reason), if neither rung resolves to an
    existing directory. Never returns a path that doesn't exist.
    """
    colocated = _colocated_root() / dir_name
    if colocated.is_dir():
        return colocated

    doe = coordinator_doe_root()
    if not doe:
        raise RuntimeError(
            f"coordinator_core.data_root: cannot resolve data dir {dir_name!r}. "
            f"Rung 1 (co-located) tried: {colocated} (not found). "
            "Rung 2 (DoE-resident) failed: coordinator_doe_root() could not "
            "resolve REPO_DOE_CLAUDE (env override, machine-local registry "
            "repos.doe_claude, plugin.mirrors.coordinator-claude.live_path, "
            "and the resolve_coordinator_clone fallback all unresolved)."
        )

    # F2 fix (2026-08-08, hermetic-ac-reverify) -- `coordinator_doe_root()`
    # (and its `_cf_codename_free_root()` ladder in particular, see that
    # module) accepts EITHER published manifest layout: the private DoE-repo
    # shape (`<root>/coordinator/schemas/...`) AND the OSS-flat shape
    # (`<root>/schemas/...`, no `coordinator/` segment). This terminal join
    # previously ALWAYS inserted `coordinator/`, so a correctly-resolved
    # OSS-flat root (e.g. a real marketplace-cache install) produced a path
    # that cannot exist -- `data_root()` was dead for every `dir_name` on
    # that layout. Try the private-shape join first (unchanged default for
    # every existing caller/test resolving a private-layout root), then the
    # OSS-flat shape -- see F2 in
    # state/review-findings/2026-08-08-successor-partitioned/hermetic-ac-reverify.md.
    private_candidate = Path(doe) / "coordinator" / dir_name
    if private_candidate.is_dir():
        return private_candidate

    flat_candidate = Path(doe) / dir_name
    if flat_candidate.is_dir():
        return flat_candidate

    raise RuntimeError(
        f"coordinator_core.data_root: cannot resolve data dir {dir_name!r}. "
        f"Rung 1 (co-located) tried: {colocated} (not found). "
        f"Rung 2 (DoE-resident) tried: {private_candidate} (private layout, not found), "
        f"{flat_candidate} (OSS-flat layout, not found)."
    )
