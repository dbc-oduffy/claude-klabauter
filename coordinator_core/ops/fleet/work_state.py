"""
coordinator_core.ops.fleet.work_state -- "fleet.work_state" op.

Purpose: aggregate `coordinator_core.session.work_state.build_work_state`'s
per-repo `{"held": [...], "unclaimed": [...], "review_due": [...]}` core across every registered
ACTIVE sibling (C5, docs/plans/2026-08-19-fleet-work-state-who-holds-which-
baton.md) -- "who holds which baton", fleet-wide, in one call, rather than
one `session.work_state` invocation per repo.

Sibling enumeration is via `coordinator_core.ops.fleet._memo_resolver.
read_registry_repos()` ONLY -- the one canonical machine-local-registry
reader (no second parser, no directory-scan fallback). "Active sibling" is
resolved exactly the way `coordinator_core.ops.fleet.capability_index`
already does: a registered `repos.*` key whose value is a non-empty path
that IS an existing directory (`Path.is_dir()`) -- see that module's own
docstring "Active sibling resolution" section, which this op follows rather
than inventing a parallel rule.

DEDUP BY RESOLVED PATH (AC9): the live registry has at least one case of two
`repos.*` keys resolving to one tree on disk. Every active sibling's path is
resolved (`Path.resolve()`) before aggregating, deduped via
`_memo_resolver.same_repo_path` (the one path-equality helper this tree
already carries for exactly this purpose -- `capability_index.py`'s own
`_enumerate_repo_paths` uses it identically), so a repo contributes exactly
one entry regardless of how many registry names point at it.

HOISTED ADDRESS RESOLUTION (AC9): `_resolve_send_message_addresses` (in
`coordinator_core.session.work_state`) takes one live-registry snapshot per
call, and that snapshot (the harness's peer-messaging registry) is
machine-global -- the same fact for every sibling, not a per-repo fact. This
op takes exactly ONE `harness_registry.snapshot()` read above the per-repo
loop and forwards it into every `build_work_state(..., messaging_snapshot=
snapshot)` call, rather than letting each call re-snapshot the same global
fact.

SKIP NON-GIT SIBLINGS BEFORE TOUCHING GIT (AC7, AC9b): a `repos.*` entry can
be a registered path that is a real, existing directory and still not be a
git repository at all (e.g. a fleet-root alias pointed at a bare
drive/volume root). "Active sibling" per `capability_index.py`'s resolution
is `Path.is_dir()` only -- it says nothing about `.git`. This op checks for
a git repository via `coordinator_core.git.repo_root.git_common_dir`, the
WALK-ONLY form (never spawns, unlike `coordinator_core.lifecycle.
git_common_dir`'s spawn fallback) BEFORE calling `build_work_state` on that
sibling at all; a sibling with no discoverable `.git` is routed straight
into the per-repo degrade path (an `errors[]` entry) rather than reaching
`build_work_state` -> `lifecycle.git_common_dir`'s real, unmemoized
`git rev-parse --git-common-dir` subprocess fallback.

SPAWN BUDGET (AC10): the per-repo loop below issues NO git subprocess call
of its own, and the walk-only pre-check above keeps a non-git sibling from
ever reaching one transitively through `build_work_state`. Passing the
static `test_no_unbatched_per_item_git_spawn.py` AST gate is NOT evidence of
zero-spawn on its own (that gate is a one-hop collector, blind to a spawn
reached through `git_common_dir`); the loop's own zero-spawn behaviour is
proven by this module's own test's AC9b runtime-fixture assertion, over a
sibling set that includes a non-git entry.

CLASSIFICATION: `OpClass.COMPUTE_ONLY` -- unlike
`fleet.aggregate_capability_index` (MUTATING, persists
`state/capabilities/fleet-index.json`), this op persists nothing and returns
its aggregated answer verbatim. `fleet.handoffs_for_plan` is the
COMPUTE_ONLY `fleet.*` precedent this op is classified alongside.

SCOPE: `"none"` (deliberately different from `session.work_state`'s
`"common_dir"`, and from `capability_index`'s `"common_dir"`, which reads
one published, schema-frozen artifact rather than aggregating a live
per-repo computation). This op is fleet-generic per
`coordinator_core.ops.session.reap`'s own documented convention
("Per-repo claim-reap PRIMITIVE" section): it is not scoped to the calling
repo's own tree at all -- it reaches across every registered sibling, and
`repo_root` (engine-injected, always `None` for scope `"none"`) is unused.

Self-registration: importing this module calls
register_op("fleet.work_state", ...) as a side-effect. Registered across the
same five surfaces as C3's `session.work_state`:
`coordinator_core/ops/__init__.py` (`_EAGER_OP_MODULES`),
`coordinator_core/ops/_registry_map.py` (`OP_MODULE_MAP`),
`coordinator_core/op_scopes.py` (scope `"none"`), and
`coordinator_core/authz/classification.py` (`OpClass.COMPUTE_ONLY`).

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C5.

Negative-spec:
    - Does NOT fork a second registry parser or add a directory-scan
      fallback -- `_memo_resolver.read_registry_repos()` is the ONE reader.
    - Does NOT re-derive or re-implement any of `build_work_state`'s own
      held/unclaimed/readiness logic -- this op's only job is per-repo
      fan-out plus the hoisted address-snapshot optimisation.
    - Does NOT spawn `git log`, `git rev-parse`, or `git status` anywhere in
      the per-repo loop (AC10) -- the walk-only pre-check plus
      `build_work_state`'s own already-zero-spawn-on-a-git-repo behaviour
      keep this loop off the spawn budget.
    - A genuine `RegistryReadError` (a PRESENT registry file that fails to
      parse, or `tomllib` unavailable) is NOT swallowed -- it propagates
      uncaught, same as `capability_index.build_fleet_index`'s own
      contract. Only "registry absent" (`read_registry_repos()` returning
      `{}`) is the empty-degrade case.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from coordinator_core.git.repo_root import git_common_dir as _walk_git_common_dir
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._memo_resolver import read_registry_repos, same_repo_path
from coordinator_core.session.work_state import build_work_state

_LOG = logging.getLogger(__name__)


def _resolve_active_sibling_paths() -> list[Path]:
    """Every registered, active, resolved-path-deduped sibling repo path.

    "Active" per `capability_index.py`'s own resolution: a registered
    `repos.*` key whose value is a non-empty path that IS an existing
    directory. Iterates `sorted(read_registry_repos().items())` so
    enumeration order (and therefore which registry key "wins" a dedup) is
    deterministic across runs rather than dict-order-dependent -- mirrors
    `capability_index._enumerate_repo_paths`'s own discipline.

    `RegistryReadError` propagates uncaught (module negative-spec).
    """
    resolved_paths: list[Path] = []
    for _key, path_str in sorted(read_registry_repos().items()):
        if not path_str:
            continue
        candidate = Path(path_str)
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if any(same_repo_path(resolved, seen) for seen in resolved_paths):
            continue
        resolved_paths.append(resolved)
    return resolved_paths


def build_fleet_work_state(
    *, snapshot: Optional[dict] = None
) -> dict[str, Any]:
    """Aggregate `build_work_state` across every active, deduped sibling.

    `snapshot`: an already-taken `harness_registry.snapshot()` read, for a
    caller that has already hoisted one for another purpose in the same
    call (e.g. a future combined fleet read). When omitted, this function
    takes exactly ONE snapshot itself, above the per-repo loop (AC9's own
    hoist requirement) -- never per-repo.

    Returns:
        {"repos": {<resolved-path-str>: {"held": [...], "unclaimed": [...], "review_due": [...]}},
         "errors": [{"target_root": <str>, "reason": <str>}, ...]}
        `errors` carries one entry per sibling skipped for not being a git
        repository (AC7/AC9b) or whose `build_work_state` call itself
        raised (the per-repo degrade path, AC9) -- never a dropped sibling
        with no trace.
    """
    if snapshot is None:
        try:
            from coordinator_core.session import harness_registry

            snapshot = harness_registry.snapshot()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("fleet.work_state: harness_registry.snapshot() failed -- %s", exc)
            snapshot = {}

    repos: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    for repo_path in _resolve_active_sibling_paths():
        key = str(repo_path)
        # Walk-only pre-check (never spawns) -- routes a non-git sibling
        # straight into the degrade path before touching build_work_state
        # (AC7, AC9b, AC10).
        if _walk_git_common_dir(str(repo_path)) is None:
            errors.append({"target_root": key, "reason": "not a git repository"})
            continue
        try:
            repos[key] = build_work_state(repo_path, messaging_snapshot=snapshot)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("fleet.work_state: build_work_state failed for %s -- %s", key, exc)
            errors.append({"target_root": key, "reason": str(exc)})

    return {"repos": repos, "errors": errors}


@register_op("fleet.work_state")
def _fleet_work_state(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.work_state" handler.

    Params: none consumed -- scope `"none"` means `repo_root` arrives as
    `None` (unused; this op is fleet-generic, not scoped to the calling
    repo's own tree).

    Returns: `build_fleet_work_state()`'s result verbatim.
    """
    return build_fleet_work_state()
