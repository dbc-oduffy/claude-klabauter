"""
coordinator_core.authz.registration_quad — five-surface op-registration completeness
check (public symbol retained as `check_registration_quad`; "quad" is now historical —
see § Fifth surface below).

Purpose: registering a `coordinator_core` op means landing the same literal op-key
string in up to four places: `@register_op("x.y")` (the live `_REGISTRY`), an
`OP_CLASSIFICATION` entry (`coordinator_core/authz/classification.py`), an
`_OP_KEY_SCOPE` entry (`coordinator_core/op_scopes.py`), and an `OP_MODULE_MAP` entry
(`coordinator_core/ops/_registry_map.py`) — plus, for the op's owning module, an entry
in `_EAGER_OP_MODULES` (`coordinator_core/ops/__init__.py`), the fifth surface (see
below). `check_registration_quad()` is the single pure computation both the pytest
guard (`coordinator_core/authz/tests/test_registration_quad.py`) and the commit-time
tripwire consume, so the rule lives in one place instead of being restated as prose two
authors have to remember to keep in sync. Per `CLAUDE.md § North star`: *"for every
rule, what artifact discharges it? 'The operator remembers' is not an answer."*

Design: `check_registration_quad()` accepts all five tables as optional injected
mappings so it is testable against in-memory fixtures with NO git index and NO live
repo (AC3) — a planted violation is a plain dict literal, not a checked-out worktree.
When a param is omitted, this module resolves it from the live in-process table. For
`registry` specifically that first requires re-running the proven full-tree discovery
walk from `coordinator_core.tests.test_dispatch_message` (`_eager_import_all()` plus a
`pkgutil.walk_packages` over `coordinator_core.ops`) — a plain `import
coordinator_core.ops` under-discovers, because ops registered only via
`coordinator_core.ops.__init__`'s hand-maintained `_EAGER_OP_MODULES` list or reached
only by walking the on-disk package tree would otherwise never import, and therefore
never register, before the check runs. `classification`/`scope`/`module_map`/
`eager_modules` need no such walk — each is a plain literal populated the moment its
own defining module imports, so an omitted one resolves via a cheap deferred import
alone.

§ Fifth surface (`_EAGER_OP_MODULES`). `_registry_map.py`'s own module docstring states
the requirement explicitly: every `OP_MODULE_MAP` entry's module must ALSO be reachable
from `coordinator_core/ops/__init__.py::_EAGER_OP_MODULES`, or the op registers only
under whichever import order happens to pull its module in — an earlier revision of
this module deliberately declined to check that, reasoning `_EAGER_OP_MODULES` was
purely `_registry_map.py`'s own cold-start performance optimization and not a source of
registration truth. That reasoning missed the failure mode `_registry_map.py`'s
docstring already named: an op's `_REGISTRY`/`OP_MODULE_MAP` presence can depend on
import order alone when its module is absent from the eager list, verified live
(`roadmap.link_stubs`, 2026-08-05) by an ad-hoc direct-import probe that returned a
false positive because importing the module ran its `@register_op` as a side effect in
that one process. `check_registration_quad()` now treats `_EAGER_OP_MODULES` membership
of an op's `OP_MODULE_MAP` module path as the fifth surface, resolved via
`_live_eager_modules()` below, keyed by module dotted path (not op name) since that is
how `_EAGER_OP_MODULES` itself is keyed. An op with no `OP_MODULE_MAP` entry has no
module path to check against the eager list — that is already its own
`OP_MODULE_MAP`-missing violation and is not additionally reported as an eager-list
miss.

Each `QuadViolation` names not just which surface(s) an op is missing from but the
literal file path each missing entry belongs in (AC12) — a message that says "missing
OP_CLASSIFICATION entry" without naming `coordinator_core/authz/classification.py`
relocates the problem instead of discharging it.

§ Known coverage limitation. The commit-time tripwire this module feeds
(`coordinator_core/bash_guards/commit_tripwires.py`) fires only for commits made
through Claude Code's own Bash tool — a human running `git commit` directly in a
terminal, or committing via GitHub Desktop or any other non-agent client, bypasses it
entirely. CI (qsub-02/03) remains the only mechanism that catches a non-agent commit.
Do not overclaim coverage: this module plus its commit-time consumer is an
agent-authorship-time guardrail, not a repo-wide enforcement boundary — the pytest
guard and CI are what make the check unconditional.

The commit-time tripwire honors a `COORDINATOR_OVERRIDE_REGISTRATION_QUAD` environment
override token to bypass the block on an in-flight commit; this module does not read or
act on that token itself (`check_registration_quad()` is unconditional and
side-effect-free) — the token is consumed entirely by the commit-tripwire caller and is
named here only so this module's docstring is the one place both consumers' contracts
are recorded together, per AC13.

`_KNOWN_UNCLASSIFIED_OPS_DEBT` freezes the 65 op-keys registered but missing an
`OP_CLASSIFICATION` entry as measured at integration time (2026-07-25, full-walk
discovery). It is generated once as a literal, not regenerated on demand — a
self-refreshing baseline is not a baseline, it is a check that can never fail. Consumed
as the SINGLE source of truth by both the pytest guard (`test_registration_quad.py`)
and the commit-time tripwire; do not duplicate this list anywhere else. Follows the
three-tier convention at `coordinator_core/tests/test_no_bash_dependency.py:167`
verbatim (a header comment naming the fix path and owner, a pointer to the owning
debt-backlog entry). Owning debt-backlog entry:
`state/debt-backlog/2026-07-23-authz-drift-guard-ops-registered-without-52137f1ff6b9.yaml`
(P2, `authz-drift-guard`). Fix path: classify each op in
`coordinator_core/authz/classification.py` and remove it from this frozenset — adding
to this set is never a valid fix; a real change to its membership is a plan amendment,
never a local executor call, enforced by the never-grows guard in
`test_registration_quad.py`.

Spec backlink: pln-registration-quad-completeness-bf0d39 § C2, AC3,
AC4, AC12, AC13
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from typing import Mapping

# ---------------------------------------------------------------------------
# Target file paths — where an operator must add a missing entry. Repo-root-
# relative, POSIX-separated (matches every other spec-backlink path convention
# in this repo; not a filesystem path resolved at runtime).
# ---------------------------------------------------------------------------
_CLASSIFICATION_FILE = "coordinator_core/authz/classification.py"
_OP_KEY_SCOPE_FILE = "coordinator_core/op_scopes.py"
_OP_MODULE_MAP_FILE = "coordinator_core/ops/_registry_map.py"
_EAGER_OP_MODULES_FILE = "coordinator_core/ops/__init__.py"

# Surface name -> target file path, in canonical quad-check order. "Quad" now
# undercounts (five surfaces as of _EAGER_OP_MODULES coverage) but the name and
# public symbol are load-bearing for existing callers — see module docstring.
_SURFACE_FILES: Mapping[str, str] = {
    "OP_CLASSIFICATION": _CLASSIFICATION_FILE,
    "_OP_KEY_SCOPE": _OP_KEY_SCOPE_FILE,
    "OP_MODULE_MAP": _OP_MODULE_MAP_FILE,
    "_EAGER_OP_MODULES": _EAGER_OP_MODULES_FILE,
}


@dataclasses.dataclass(frozen=True)
class QuadViolation:
    """One registered op missing one or more of the four non-registry surfaces.

    surfaces_present / surfaces_missing use the canonical surface names ("OP_CLASSIFICATION",
    "_OP_KEY_SCOPE", "OP_MODULE_MAP", "_EAGER_OP_MODULES"). missing_surface_files pairs each
    missing surface with the literal file path an operator must edit to close it (AC12), in
    the same order as surfaces_missing.
    """

    op_key: str
    surfaces_present: tuple[str, ...]
    surfaces_missing: tuple[str, ...]
    missing_surface_files: tuple[tuple[str, str], ...]


def _is_test_like_module_name(dotted_name: str) -> bool:
    """True for a test/fixture-shaped module name the discovery walk must not import
    as if it were a production op module.

    Structural exclusion (a `tests` package component, a `test_*.py` leaf, or
    `conftest.py`) rather than a name skip-list — mirrors
    `coordinator_core.tests.test_dispatch_message._is_test_like_module_name` exactly,
    duplicated here rather than imported from a test module (production code must not
    import from `tests/`).
    """
    parts = dotted_name.split(".")
    leaf = parts[-1]
    return "tests" in parts or leaf.startswith("test_") or leaf == "conftest"


def _discover_all_ops() -> list[str]:
    """Import every op-registering module reachable from `coordinator_core.ops`,
    triggering each module's `register_op(...)` side effects. Returns the dotted
    names actually imported by pass 2 (diagnostic on a vacuous walk).

    Two deterministic passes, neither reliant on import order:
      1. `coordinator_core.ops._eager_import_all()` — reaches self-registering modules
         OUTSIDE `coordinator_core.ops` too (hooks, frontmatter.schema_cli,
         orientation.regenerate_cache, session_ledger, plugin_health, goals).
      2. A real `pkgutil.walk_packages` over `coordinator_core.ops` itself — closes the
         gap pass 1 cannot: a module that exists on disk under `coordinator_core/ops/`
         but was never added to `_EAGER_OP_MODULES`.

    A plain `import coordinator_core.ops` under-discovers relative to this and MUST
    NOT be substituted for it.

    # Review: code-reviewer (Finding 1) — mirrors
    # coordinator_core/tests/test_dispatch_message.py's
    # _import_all_ops_tree_modules()/test_op_key_scope_table_covers_all_registered_ops
    # pair: pass 2 imports are returned and asserted non-empty below, so a silently
    # broken walk (frozen/zipped distribution, mutated __path__, etc.) fails loud
    # instead of under-discovering ops one level above the registration table this
    # module exists to check.
    """
    import coordinator_core.ops as _ops_pkg

    _ops_pkg._eager_import_all()
    imported: list[str] = []
    for module_info in pkgutil.walk_packages(_ops_pkg.__path__, prefix=_ops_pkg.__name__ + "."):
        if _is_test_like_module_name(module_info.name):
            continue
        importlib.import_module(module_info.name)
        imported.append(module_info.name)
    assert imported, (
        "pkgutil walk imported zero modules under coordinator_core.ops — "
        "the walk itself is broken, not the tree"
    )
    return imported


# Review: code-reviewer (Finding 4) — one-line purpose docstring per helper, matching
# the RAG-bait convention every other function in this file already follows.
def _live_registry() -> Mapping[str, object]:
    """Live `_REGISTRY` table, deferred-imported to keep this module's own import cheap."""
    from coordinator_core.ipc import _REGISTRY

    return dict(_REGISTRY)


def _live_classification() -> Mapping[str, object]:
    """Live `OP_CLASSIFICATION` table, deferred-imported to keep this module's own import cheap."""
    from coordinator_core.authz.classification import OP_CLASSIFICATION

    return dict(OP_CLASSIFICATION)


def _live_scope() -> Mapping[str, str]:
    """Live `_OP_KEY_SCOPE` table, deferred-imported to keep this module's own import cheap."""
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    return dict(_OP_KEY_SCOPE)


def _live_module_map() -> Mapping[str, str]:
    """Live `OP_MODULE_MAP` table, deferred-imported to keep this module's own import cheap."""
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    return dict(OP_MODULE_MAP)


def _live_eager_modules() -> frozenset[str]:
    """Live `_EAGER_OP_MODULES` surface, deferred-imported to keep this module's own
    import cheap. `_EAGER_OP_MODULES` entries are `(module_path, note)` tuples; only the
    module_path half is a registration surface, so this returns the set of module
    dotted paths reachable from that list — matched against `OP_MODULE_MAP`'s value for
    an op, not against the op key itself (see module docstring § Fifth surface)."""
    from coordinator_core.ops import _EAGER_OP_MODULES

    return frozenset(module_path for module_path, _note in _EAGER_OP_MODULES)


def check_registration_quad(
    registry: Mapping[str, object] | None = None,
    classification: Mapping[str, object] | None = None,
    scope: Mapping[str, object] | None = None,
    module_map: Mapping[str, object] | None = None,
    eager_modules: "frozenset[str] | set[str] | None" = None,
) -> list[QuadViolation]:
    """For every op in `registry`, verify it also carries an entry in `classification`,
    `scope`, and `module_map`, and that `module_map`'s module path for it appears in
    `eager_modules`. Returns one `QuadViolation` per op missing at least one of those
    four surfaces, sorted by op_key.

    `eager_modules` is a set of module dotted paths (the module_path half of
    `_EAGER_OP_MODULES`'s `(module_path, note)` tuples), NOT keyed by op name — an op's
    membership is resolved by looking up its `OP_MODULE_MAP` entry first. An op absent
    from `module_map` has no module path to check and is reported only as an
    `OP_MODULE_MAP` miss, never additionally as an `_EAGER_OP_MODULES` miss (see module
    docstring § Fifth surface).

    Pure and side-effect-free with respect to disk/git/network: it only ever reads the
    five inputs passed in (or, for any left `None`, the corresponding live in-process
    table — `classification`/`scope`/`module_map`/`eager_modules` resolve via their own
    cheap `_live_*()` import; only a `None` `registry` triggers the full ops-tree
    discovery walk — see `_discover_all_ops`). No param is written to; no file is
    touched; no git command is run. Callers that want a fully offline check (AC3) pass
    all five params explicitly and never hit `_discover_all_ops()` at all.

    Does NOT filter against `_KNOWN_UNCLASSIFIED_OPS_DEBT` — that frozenset is a
    baseline for CALLERS (the pytest guard, the commit tripwire) to diff their own
    result against, not a suppression this function applies itself. This function
    always reports every violation it finds; hiding known debt is the caller's
    decision to make explicitly, not this function's to make silently.

    Does NOT detect "stale" entries (a surface entry with no corresponding registered
    op) — that is a different failure shape, owned by
    `coordinator_core/ops/tests/test_registry_map_sync.py`, not this quad check.
    """
    # Review: code-reviewer (Finding 3) — only `registry` depends on the ops-tree
    # discovery walk (op modules self-register into `_REGISTRY` via import-time side
    # effect). `classification`/`scope`/`module_map` are plain dict literals that
    # populate on their own defining module's import and never need the walk; gating
    # them on `needs_discovery` made a caller supplying three of four params still
    # pay the full walk to resolve the fourth.
    if registry is None:
        _discover_all_ops()

    resolved_registry = _live_registry() if registry is None else registry
    resolved_classification = _live_classification() if classification is None else classification
    resolved_scope = _live_scope() if scope is None else scope
    resolved_module_map = _live_module_map() if module_map is None else module_map
    resolved_eager_modules = _live_eager_modules() if eager_modules is None else frozenset(eager_modules)

    violations: list[QuadViolation] = []
    for op_key in sorted(resolved_registry):
        present: list[str] = []
        missing: list[str] = []
        if op_key in resolved_classification:
            present.append("OP_CLASSIFICATION")
        else:
            missing.append("OP_CLASSIFICATION")
        if op_key in resolved_scope:
            present.append("_OP_KEY_SCOPE")
        else:
            missing.append("_OP_KEY_SCOPE")
        if op_key in resolved_module_map:
            present.append("OP_MODULE_MAP")
            if resolved_module_map[op_key] in resolved_eager_modules:
                present.append("_EAGER_OP_MODULES")
            else:
                missing.append("_EAGER_OP_MODULES")
        else:
            missing.append("OP_MODULE_MAP")

        if missing:
            missing_files = tuple((surface, _SURFACE_FILES[surface]) for surface in missing)
            violations.append(
                QuadViolation(
                    op_key=op_key,
                    surfaces_present=tuple(present),
                    surfaces_missing=tuple(missing),
                    missing_surface_files=missing_files,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Known-incomplete-registrations allowlist — DEBT LEDGER, NOT AN EXEMPTION POLICY.
#
# Frozen 2026-08-11, measured by calling check_registration_quad() directly on
# HEAD (70 live QuadViolation entries at the time). Owning bug-backlog entry:
# state/bug-backlog/2026-08-11-check-registration-quad-is-red-on-70-ops-0c14fa26f522.yaml
#
# This exists ONLY to restore the gate's signal (a check red-by-default on 70
# pre-existing ops enforces nothing) — it forgives exactly the recorded gap per
# op, nothing more: an op on this list that is ALSO missing a surface NOT
# recorded here still trips the gate (see `_prune_known_incomplete` below,
# which subtracts only the recorded surfaces from `surfaces_missing`). This is
# distinct from `_KNOWN_UNCLASSIFIED_OPS_DEBT` above (a narrower,
# classification-only ledger from a separate, earlier debt-backlog entry with
# its own never-grows guard) — this ledger additionally covers ops missing
# `_OP_KEY_SCOPE` and/or `OP_MODULE_MAP`, which that older ledger has no shape
# for.
#
# Nothing should ever be ADDED to this mapping. An entry comes OFF it only by
# landing the real registration surface(s) it names (with, for
# OP_CLASSIFICATION specifically, the five-question affirmation
# `classification.py`'s own convention requires) and deleting the entry — never
# by an executor's local judgment call. The remaining 70 ops (67 missing only
# OP_CLASSIFICATION, tracked by `_KNOWN_UNCLASSIFIED_OPS_DEBT` above; the 6
# below needing a fuller registration) still need that real work; this ledger
# buys back the gate's legibility, it does not do the work.
# ---------------------------------------------------------------------------
_KNOWN_INCOMPLETE_REGISTRATIONS: Mapping[str, tuple[str, ...]] = {
    "install.detect_cmd_autorun_coverage": ("OP_CLASSIFICATION", "OP_MODULE_MAP"),
    "install.strip_cmd_autorun_guard": ("OP_CLASSIFICATION", "OP_MODULE_MAP"),
    "install.write_cmd_autorun_guard": ("OP_CLASSIFICATION", "OP_MODULE_MAP"),
    "distill.curate_clusters": ("OP_MODULE_MAP",),
    "memo.fate_backfill": ("OP_MODULE_MAP",),
    "updatedocs.gates": ("OP_MODULE_MAP",),
}


def prune_known_incomplete(
    violation: QuadViolation, baseline: Mapping[str, tuple[str, ...]] | None = None
) -> QuadViolation | None:
    """Drop from `violation.surfaces_missing` exactly the surfaces recorded for its
    `op_key` in `baseline` (default `_KNOWN_INCOMPLETE_REGISTRATIONS`), returning
    `None` if nothing punishable remains.

    Forgives only the recorded gap, never the op wholesale: an op on the baseline
    that is ALSO missing a surface not listed for it there is still reported for
    that residual surface. An op not on the baseline at all is returned unchanged
    (same object, no copy) — mirrors
    `coordinator_core.bash_guards.commit_tripwires._prune_baselined_classification`'s
    contract exactly, generalized from a single fixed surface (OP_CLASSIFICATION)
    to an arbitrary per-op surface set.
    """
    if baseline is None:
        baseline = _KNOWN_INCOMPLETE_REGISTRATIONS
    allowed = baseline.get(violation.op_key)
    if not allowed:
        return violation
    kept_missing = tuple(s for s in violation.surfaces_missing if s not in allowed)
    if not kept_missing:
        return None
    if kept_missing == violation.surfaces_missing:
        return violation
    kept_files = tuple((s, p) for s, p in violation.missing_surface_files if s in kept_missing)
    return dataclasses.replace(
        violation,
        surfaces_missing=kept_missing,
        missing_surface_files=kept_files,
    )


def filter_known_violations(
    violations: list[QuadViolation],
    *,
    classification_baseline: "frozenset[str] | None" = None,
    incomplete_baseline: Mapping[str, tuple[str, ...]] | None = None,
) -> list[QuadViolation]:
    """Combined gate-consumer filter: apply both known-debt allowlists
    (`_KNOWN_UNCLASSIFIED_OPS_DEBT` for the classification-only ledger,
    `_KNOWN_INCOMPLETE_REGISTRATIONS` for the fuller one) to a raw
    `check_registration_quad()` result, returning only the violations that
    survive both prunes. This is what makes the gate GREEN on today's known
    debt and RED on anything new or changed — see module docstring § Known
    coverage limitation and the `_KNOWN_INCOMPLETE_REGISTRATIONS` comment
    above for why two separate ledgers exist rather than one.

    Both pytest guard (`test_registration_quad.py`) and the commit-time
    tripwire (`commit_tripwires.py`) should route through this rather than
    re-deriving the two-prune sequence locally.
    """
    if classification_baseline is None:
        classification_baseline = _KNOWN_UNCLASSIFIED_OPS_DEBT
    if incomplete_baseline is None:
        incomplete_baseline = _KNOWN_INCOMPLETE_REGISTRATIONS

    result: list[QuadViolation] = []
    for v in violations:
        kept_missing = tuple(
            s
            for s in v.surfaces_missing
            if not (s == "OP_CLASSIFICATION" and v.op_key in classification_baseline)
            and s not in incomplete_baseline.get(v.op_key, ())
        )
        if not kept_missing:
            continue
        if kept_missing == v.surfaces_missing:
            result.append(v)
            continue
        kept_files = tuple((s, p) for s, p in v.missing_surface_files if s in kept_missing)
        result.append(
            dataclasses.replace(v, surfaces_missing=kept_missing, missing_surface_files=kept_files)
        )
    return result


# ---------------------------------------------------------------------------
# Known-debt baseline — the 65 op-keys registered but missing an OP_CLASSIFICATION
# entry, measured at integration time (2026-07-25, full-walk discovery). See module
# docstring for the three-tier-convention citation, owning debt-backlog entry, and
# fix path. A real addition here is a plan amendment, never an executor's local call
# — `test_registration_quad.py` asserts this set never grows.
# ---------------------------------------------------------------------------
_KNOWN_UNCLASSIFIED_OPS_DEBT: frozenset[str] = frozenset(
    {
        "baton.resolve_path_and_repo",
        "baton.resolve_swept_in_archive",
        "branch.merge_into_workstream",
        "bug_sweep.verify_fix_files_changed",
        "cartography.count_references",
        "cartography.stack",
        "ceremony.init_anchor_injection_state",
        "ceremony.scoped_git_commit",
        "ci.run_pip_audit",
        "ci.run_semgrep_scan",
        "ci.run_shellcheck_sweep",
        "cli.parse_date_flags",
        "cli.parse_flag",
        "commit.exec_bit_change",
        "completion.flip_to_released",
        "coverage.halt_on_uncovered",
        "dependency.detect_changed_manifests",
        "detect.plugin_layout",
        "detect.primary_languages",
        "doctrine.assert_cross_reference_counts",
        "fanout.poll_scratch_dir",
        "findings.self_persist_fallback",
        "fleet.archive_paper_trail",
        "fleet.archive_queue_entry",
        "fleet.archive_release_accumulator",
        "fleet.migrate_handoff_vocabulary",
        "git_branch.compute_descendant_tip",
        "git_branch.detect_unpushed_commits",
        "git_branch.list_unmerged_work",
        "git_branch.verify_commit_in_review_window",
        "install.clone_idempotent",
        "install.detect_python3_appx_stub",
        "install.probe_skill_frontmatter_valid",
        "install.probe_windows_terminal_presence",
        "install.wrapper_onto_path",
        "install.write_identity_file",
        "install.write_shell_rc_guard_block",
        "lessons.filter_undated_universal",
        "lessons.reject_orphan_strip_entries",
        "machine.hibernate",
        "mcp.resolve_server_cli_path",
        "merge.quiet_activity_gate",
        "percolate.check_inverse_drift",
        "percolate.list_files_newer_than_marker",
        "percolate.run_ci_smoke_check",
        "percolate.run_pre_ci_hooks",
        "percolate.scan_content_leakage_tiers",
        "plan.list_stale_executing",
        "release.cut_tag",
        "release.cut_tag_and_publish",
        "repo.clone_and_register",
        "repo.create_and_push_remote",
        "repo_setup.copy_console_subprocess_tripwire",
        "repo_setup.validate_target_root",
        "research.archive_workdir",
        "research.restructure_for_repeat_topic",
        "research.verify_scout_inventory_completeness",
        "review.snapshot_diff_and_head",
        "review_trail.scan_unresolved_ubt",
        "schema.drift_gate",
        "session.resolve_chain_terminal_disposition",
        "session.rotate_orphan_sweep_log",
        "update_docs.probe_fresh_repo_noop",
        "workday.stitch_sidecar_into_summary",
        "workday.surface_auto_push_failure_stats",
    }
)
