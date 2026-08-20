"""Collector (class-shaped) for the `machine-local` CLI read-side shell-out.

Spec backlink: `docs/wiki/cost-budgets-and-the-kill-disposition.md` -- the
`machine-local` read-side shell-out is that page's own worked example of "why
line-by-line review never converges": ~66 read-side call sites across ~49
files, ~15 independently reinvented `_machine_local_get`-shaped helpers, in a
repo where `coordinator_core.machine_resolver.registry_get` already reads the
same `registry.local.toml` over `registry.toml` chain in-process. Structural
template: `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`
(git-argv-only by construction, so it cannot see this class -- this file is
its `machine-local` sibling, not a replacement).

THE GAP THIS COLLECTOR CLOSES. Nothing in this repo asserts, structurally,
that a NEW call site cannot shell out to the `machine-local` CLI to read a
registry value it could read in-process via `machine_resolver.registry_get`
/ `load_flat_registry_file` / `registry_dir`. `machine_resolver.py`'s own
negative-spec ("Do NOT shell out to a `machine-local` CLI/binary", 2026-08-05
PM directive "De-bash spawn-amplification hardening") lives in a docstring
only -- no test collector, no gate. This closes that: a static, AST-driven
scan for the shape `subprocess.<spawn-fn>(...)` where the call's own source
text names the `machine-local` binary AND a read subcommand (`get`/`keys`),
run over every `.py` file under `coordinator_core/` and `coordinator/bin/`.

WHY SOURCE-TEXT MATCHING, NOT `spawn_policy.detect`'s `argv0` RESOLUTION.
`spawn_policy.detect._resolve_argv0` cannot resolve the shape this class
actually uses -- `subprocess.run([*ml_argv, "get", key], ...)` -- because the
first list element is a `Starred` node, which `_resolve_argv0` does not
handle (falls through to `<dynamic>`, matching nothing). This collector does
not reuse `spawn_policy.detect`'s `SpawnSite`/`argv0` machinery for that
reason; it walks `ast.Call` nodes directly and matches on the call's own
`ast.unparse()` text, which sees through the `Starred` unpacking because it
prints the whole call expression, not just a resolved first token.

WHAT COUNTS AS A HIT: an `ast.Call` to `subprocess.run` / `subprocess.Popen`
/ `subprocess.check_output` / `subprocess.check_call` / `subprocess.getoutput`
whose unparsed source contains a `machine-local`-shaped binary reference
(`machine-local`, `machine_local`, `ml_argv`, `ml_bin`, `_resolve_machine_local`)
AND a bare `"get"` or `"keys"` string literal argument -- the two tokens the
CLI's own read surface is built from (`machine-local get <key>` /
`machine-local keys`). A WRITE call (`set`, `delete`, `unset`) is out of
scope by construction -- this collector's brief is read-side only; see the
negative-spec below.

NEGATIVE-SPEC:
  - Write-side `machine-local set|delete|unset` spawns are NOT flagged --
    scoped to reads per this class's own brief (`machine_resolver.registry_get`
    is a read-only substitute; there is no in-process write substitute audited
    here).
  - `machine_resolver.py`, `coordinator_core/machine_local_forwarder`-shaped
    forwarder/impl modules, and the CLI implementation itself
    (`coordinator/bin/lib/machine_local_impl_resolve.py`,
    `coordinator/bin/lib/machine_local_resolve.py`) are excluded: they ARE
    the resolution surface this collector protects call sites from bypassing,
    not a call site themselves.
  - A subprocess call that merely CHECKS machine-local's resolvability
    (`--help`, existence probe, no `get`/`keys` token) is not a read and is
    not flagged.
  - `check_machine_local_regeneratability.py::_ladder_resolves` was attempted
    for conversion in this dispatch and REVERTED: it shells
    `machine-local get <key>` and checks only the return code, and its own
    docstring records that rc=0 means "the ladder (autodiscovery OR another
    rung) can derive the value" -- the CLI's ladder is not a pure two-file
    TOML merge, it can autodiscover a repo path from disk state
    `registry_get` never consults. Swapping in `registry_get` changed
    observable behaviour (two of that module's own golden tests regressed:
    a key present only in the gitignored `registry.local.toml`, with no CLI
    binary on disk, must still be flagged as an install-surface-completeness
    gap -- `registry_get` found the local.toml value directly and wrongly
    treated it as "ladder resolved", collapsing the very distinction Check 2
    exists to draw). Correctness boundary as of that dispatch. SUPERSEDED
    2026-08-20: the site (renamed `_ladder_snapshot`) was later refactored to
    a single batched `machine-local dump --prefix repos --format json` call
    (W6/C6, 2026-08-19 amplification burn-down) instead of a per-key `get`
    probe -- `dump` is not `get`/`keys`, so it no longer matches this
    collector's brief at all and carries no inventory row (see the burn-down
    comment's "2026-08-20 C3 WIDENING -- DROPPED ROWS" note, and
    `test_collector_silent_on_regeneratability_now_dump_shaped` below).
"""
from __future__ import annotations

import ast
import copy
import pathlib

import pytest

from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SPAWN_FUNCS = {"run", "Popen", "check_output", "check_call", "getoutput"}

_BINARY_MARKERS = (
    "machine-local",
    "machine_local",
    "ml_argv",
    "ml_bin",
    "_resolve_machine_local",
)

# Modules that ARE the machine-local resolution/CLI surface, not a caller of
# it -- excluded so the collector protects call sites without alarming on the
# implementation it is protecting them in favour of.
_SURFACE_MODULES = frozenset(
    {
        "coordinator_core/machine_resolver.py",
        "coordinator_core/ops/machine_local_forwarder.py",
        "coordinator/bin/lib/machine_local_impl_resolve.py",
        "coordinator/bin/lib/machine_local_resolve.py",
        "coordinator/bin/machine-local",
    }
)

# Known, not-yet-converted read-side sites -- the burn-down inventory.
# ANY entry here is a site the class census found still shelling out to
# `machine-local get`/`keys` for a read the direct registry reader could
# serve. New entries are refused (test_inventory_is_exhaustive below); the
# fix is to convert the site and remove its row, never to add rows freely.
KNOWN_UNCONVERTED_SITES: frozenset[str] = frozenset(
    {
        "coordinator/bin/claude-doe.py:368",
        "coordinator/bin/claude-doe.py:389",
        "coordinator/bin/lib/git_hook_install.py:159",
        "coordinator/lib/percolate/resolve_target.py:215",
        "coordinator/lib/percolate/targets.py:120",
        "coordinator/lib/resolve-coordinator-clone.py:223",
        "coordinator_core/engine_root.py:191",
        "coordinator_core/ops/gen_claude_doe_shim.py:414",
        "coordinator_core/ops/gen_doe_root_pointer.py:127",
        "coordinator_core/ops/new_project_scaffold.py:159",
        "coordinator_core/ops/render_template_tree.py:97",
        "coordinator_core/ops/repo_bootstrap.py:141",
        "coordinator_core/resolve_coordinator_clone.py:143",
        # 2026-08-20 C3 (resolver-call-indirection widening) -- this file's own
        # two sites, see "2026-08-20 C3 WIDENING" note below.
        "coordinator/bin/lib/coordinator_registry.py:160",
        "coordinator/bin/lib/coordinator_registry.py:431",
        # 2026-08-20 C3 widening also surfaced the pre-existing `_machine_local_get`
        # helper family below -- same shape, previously invisible. See note below.
        "coordinator/bin/coordinator-doc-new.py:481",
        "coordinator/bin/coordinator-doc-new.py:523",
        "coordinator/bin/coordinator-lesson-add.py:109",
        "coordinator/bin/fan-out-dispatch.py:360",
        "coordinator/bin/gen-claude-klabauter-root-pointer.py:139",
        "coordinator/bin/lib/cc_invoke.py:396",
        "coordinator/bin/lib/cli_shared.py:100",
        "coordinator/bin/lib/cli_shared.py:148",
        "coordinator/bin/tests/test_claude_machine_local.py:110",
        "coordinator/bin/workday-start-step0.py:145",
        "coordinator_core/ops/check_arch_audit_staleness.py:118",
        "coordinator_core/ops/check_weekly_staleness.py:119",
        "coordinator_core/ops/deliverable_rollup.py:133",
        "coordinator_core/ops/list_review_trail_records.py:127",
        "coordinator_core/ops/list_week_changelog.py:100",
        "coordinator_core/ops/queue_append.py:754",
        "coordinator_core/ops/workday_complete_backfill_scan.py:202",
        "coordinator_core/orientation/regenerate_cache.py:282",
        "coordinator_core/pyresolve.py:168",
        "coordinator_core/roadmap/audit.py:173",
    }
)
# Burn-down inventory (33 sites, 2026-08-20 census -- 13 carried forward from
# the 2026-08-16 census below, minus 2 that dropped out (see "2026-08-20 C3
# WIDENING -- DROPPED ROWS" at the end of this comment), plus 20 newly-visible
# under the C3 resolver-call-indirection widening -- see "2026-08-20 C3
# WIDENING" at the end of this comment for the new rows' disposition).
#
# Burn-down inventory (15 sites, 2026-08-16 census, post C7+C7b+C8 conversion,
# the same-day parallel-review-integration census widening below, AND the
# same-day repos.* ladder-loss fix below that -- 10 rows + 5 new rows).
# `_ladder_resolves` (`check_machine_local_regeneratability.py`),
# `_tier_a5`/`_publish_mirror_keys` (`discover_working_repos.py`),
# `_apply_machine_local_days_override`/`_default_parent_roots`
# (`cruft-sweep.py`), `_registry_repo_roots` (`git_hook_install.py`), and
# seven of the sixteen C7 rows (`bootstrap_repo.py`,
# `capture_fan_out_threshold.py`, `central_run_due.py`,
# `check_registry_codename_leak.py`, `gen_doe_root_pointer.py`'s
# `plugin.mirrors.coordinator-claude.source_path` row,
# `new_project_scaffold.py`'s `_register_repo` verify-read row,
# `setup_seed_health_ledger.py`) are fully converted (flat `registry_get`,
# no CLI subprocess at all) and NOT in this set. The other five C7
# `repos.*` rows (`gen_claude_doe_shim.py`, `gen_doe_root_pointer.py`'s
# `repos.doe_claude` row, `new_project_scaffold.py`'s `_resolve_doe_root`
# row, `render_template_tree.py`, `repo_bootstrap.py`) are back IN this set
# as ladder-preserving fallback compositions -- see the 2026-08-16
# REPOS.* LADDER-LOSS FIX note below for why.
# C7b converted the four C7 EXCEPTIONS (`coordinator_doe_root.py:158`,
# `ensure_doe_clone.py:68`, `install_shell_init_guard_seam.py:138`,
# `verify_ue_overrides.py:121`): each site's own test suite now seeds the
# machine-local registry FILE (`MACHINE_LOCAL_REGISTRY_DIR` + a scratch
# `registry.toml`) instead of faking the CLI as a real subprocess-invoked
# stub, so the in-process `registry_get` conversion no longer silently stops
# exercising those fakes -- see each production module's `_ml_get`/
# `_machine_local_get`/`_registry_get` docstring for the per-site detail.
# This is a TEST-SHAPE disposition, distinct from the
# `check_machine_local_regeneratability.py` exception below, which is a real
# correctness boundary (the CLI's autodiscovering ladder `registry_get`
# never consults) and stays unconverted permanently -- the two classes are
# not merged in this comment.
#
# C8 (this dispatch) converted `coordinator_core/snippet_sync/registry.py:468`
# (`_ml_get` now reads via `machine_resolver.registry_get` in-process; its own
# test module (`snippet_sync/tests/test_registry.py`,
# `snippet_sync/tests/test_verify.py`) never faked `machine-local` as a real
# subprocess-invoked stub -- it monkeypatches at the Python-attribute level or
# relies on `machine_local_bin=None` determinism, so nothing was silently
# stopped from being exercised) -- removed from the inventory, row above.
#
# `claude-doe.py:358`/`:379` were NOT converted: the module's own header and
# `_machine_local_argv` docstring state it is installed STANDALONE and cannot
# import `coordinator_core` -- the same correctness boundary as the
# `check_machine_local_regeneratability.py` exception above, just enforced by
# the install shape rather than a ladder-autodiscovery gap.
#
# `resolve_coordinator_clone.py:143` (the shared `_machine_local_get` helper)
# was NOT converted: both its callers (`_registry_doe_claude`,
# `_registry_live_path`) already try `machine_resolver.registry_get` FIRST,
# and the module's own docstrings on those two functions document the CLI
# subprocess as a genuine fallback rung for reset-safety (the CLI's
# reader/exec bits live under the resettable `~/.claude/bin/`, so a CLI
# failure doesn't mean the registry is unresolvable) and for values present
# under `machine-local`'s CLI-managed state but not yet mirrored into
# `registry.local.toml`/`registry.toml` -- `test_resolve_coordinator_clone.py::
# test_registry_live_path_falls_back_to_cli_when_registry_get_empty` asserts
# this exact fallback fires. Same disposition class as the
# `check_machine_local_regeneratability.py` ladder-autodiscovery exception:
# the CLI rung here covers state `registry_get` provably cannot reach.
#
# `test_engine_root_conformance.py:71` is a TEST whose own `_machine_local_get`
# mirrors `coordinator_core.engine_root`'s Rung 2 CLI fallback for
# `engine.working_repos.doe_claude` -- `coordinator_core/engine_root.py:179`
# is the production site it pins, and that site is held by the one-engine
# plan (this plan's Out of scope), so both stay unconverted together, in
# lockstep, until that plan converts the production site, not on its
# own.
#
# 2026-08-16 CENSUS WIDENING (parallel-review-integration pass, slice 3): the
# `_collect_all_hits` scan roots did not include `coordinator/lib/` until this
# pass -- AC9/AC10's "exhaustive"/"no row unexplained" claims were true only
# of `coordinator_core/`, `coordinator/bin/`, and `bin/`, not of the live
# tree. Widening the roots surfaced three sites, none previously inventoried:
#
# `coordinator/lib/resolve-coordinator-clone.py:225` (`_registry_doe_claude`)
# WAS PARTIALLY CONVERTED: it now tries `machine_resolver.registry_get`
# (via this module's own pre-existing `_import_registry_get()` bootstrap,
# the same rung `_registry_live_path` in this file already used) FIRST,
# zero-spawn on the happy path, falling through to the `machine-local` CLI
# shell-out only when the in-process reader is unavailable or empty. The
# `subprocess.run([..., "get", "repos.doe_claude"], ...)` shape is
# unavoidably still present in source, so the collector still flags it --
# same disposition class as `coordinator_core/resolve_coordinator_clone.py:143`
# (a genuine, deliberately-retained fallback rung), except this call site
# carries NO test of its own asserting the fallback fires (searched
# `coordinator/tests/test_resolve_coordinator_clone.py` and
# `coordinator/tests/test_resolve_coordinator_clone_source_mode.py` --
# neither exercises `_registry_doe_claude`), so the fallback is kept
# un-deleted rather than removed on no evidence it is safe to drop.
#
# `coordinator/lib/percolate/resolve_target.py:215` (`_machine_local_get`)
# was NOT converted: its whole contract (see its own docstring and
# `coordinator/tests/test_percolate_resolve_target.py::
# test_exec_failure_reported_as_transport_error_not_unset`,
# `::test_present_but_not_executable_is_transport_error_not_unset`,
# `::test_absent_machine_local_still_raises_bare_code_3`) is distinguishing
# rc 3 (CLI absent) from rc 4 (CLI present but exec itself failed, e.g. a
# non-executable file on disk) from rc 1 (CLI ran, key genuinely unset) --
# three observably different outcomes a pure TOML reader cannot produce,
# because there is no CLI exec to fail. Same correctness-boundary class as
# the `check_machine_local_regeneratability.py` exception above.
#
# `coordinator/lib/percolate/targets.py:120` (`_machine_local_get_multi`,
# gated by `_machine_local_has` + `is_executable(machine_local_bin)`) was NOT
# converted: unlike `resolve_target.py`'s sibling, this pair carries no
# rc-code contract of its own (failures are swallowed to `False`/`""`), so it
# is plausibly convertible in principle -- but `coordinator/tests/
# test_percolate_targets.py` fakes `machine-local` as a REAL subprocess-
# invoked bash/`.cmd` stub across every tier (rc1/rc2/rc3, legacy fallback,
# dedup) this key's read participates in, the same pre-C7b test shape C7b's
# four sites moved off of (seed a registry FILE instead of faking the CLI).
# Converting this site blind, without the matching C7b-style fixture
# rewrite, would silently stop exercising that fixture for the SUPPLEMENT
# tier -- left as a flagged, not-yet-converted site for a dedicated pass, not
# folded into this dispatch's two-finding fix scope.
#
# 2026-08-16 REPOS.* LADDER-LOSS FIX (parallel-review-integration, slice 2):
# a reviewer found that `machine-local get repos.<slug>` is NOT a flat
# registry read -- the CLI routes `repos.<slug>` keys through a 4-rung ladder
# (`REPO_<SLUG>` env, marker-based autodiscovery, `path-exceptions.toml`,
# then `registry.local.toml`), and `machine_resolver.registry_get` only ever
# reaches the last rung. This is the SAME correctness-boundary class as the
# `check_machine_local_regeneratability.py` exception above, generalized from
# one site to the whole `repos.*` key class -- it is NOT the C7b test-shape
# class (those four sites' own tests faked the CLI as a subprocess) and NOT
# the `resolve_coordinator_clone.py:143`/`resolve-coordinator-clone.py:225`
# reset-safety-fallback class (those predate this finding for an unrelated
# reason). The five sites below were re-given a `registry_get`-first,
# CLI-fallback-on-miss composition (mirroring `resolve-coordinator-clone.py`'s
# existing shape) rather than a flat revert, so the zero-spawn win is kept on
# the (common) explicitly-registered path and the ladder is only walked on a
# registry miss:
#
# `coordinator_core/ops/repo_bootstrap.py:141` (`_machine_local_get`) --
# HIGHEST severity of the five: feeds `clone_and_register_sibling_repo`'s
# `already_registered` idempotency check, so a flat-read false-negative here
# is destructive-adjacent (re-clone/re-register of a repo the CLI's ladder
# would have recognized).
#
# `coordinator_core/ops/gen_claude_doe_shim.py:414`,
# `coordinator_core/ops/gen_doe_root_pointer.py:127`,
# `coordinator_core/ops/new_project_scaffold.py:159`,
# `coordinator_core/ops/render_template_tree.py:96` -- all resolve
# `repos.doe_claude` for install/scaffold gating. Verified live, not
# hypothetical: the DoE-claude sibling repo (`repos.doe_claude`'s registry
# target) carries a repo-root `.coordinator-dev-repo` marker with
# `slug: doe-claude`, so the CLI's rung-2 autodiscovery is a real,
# load-bearing path for this exact key on a real machine -- the
# `REPO_DOE_CLAUDE` env rung each site already preserved does not cover it.
# `gen_doe_root_pointer.py`'s own module
# docstring negative-spec ("does NOT reimplement the machine-local
# registry.toml/registry.local.toml parser -- shells out to the
# `machine-local` CLI... so the registry-merge logic has exactly one
# implementation") had been silently broken by the flat conversion; the
# fallback restores it.
#
# NOT given the fallback, judged safe as flat `registry_get` conversions:
# `new_project_scaffold.py::_register_repo`'s round-trip verification read
# (immediately follows a `machine-local set` of the same key in the same
# call, so `registry.local.toml` -- the one rung `registry_get` reaches --
# is guaranteed fresh; no autodiscovery case exists to lose) and
# `bin/claude-klabauter-doctor-probe.py`'s two `repos.claude_klabauter` sites (rung-2
# registry read backed by the probe's own rung-3 git-root autodiscovery from
# its own `bin/` location, and its registry-key probe's whole documented
# purpose is checking explicit-registration state, not resolvability, per
# its own docstring: "the value itself is a registry read, not a shim-
# liveness check"). `verify_ue_overrides.py`'s three `repos.*` reads
# (C7b) are also left as flat conversions: its own module docstring states
# those keys "must be set in registry.local.toml" as a deliberate
# fail-loud diagnostic contract -- autodiscovery masking an unset key would
# defeat this manual tool's purpose, not serve it.
#
# 2026-08-20 C3 WIDENING -- DROPPED ROWS (code churn, not this dispatch's
# doing -- landed by `9245fd0d5`-adjacent and `b12a51e75` ("C14: port
# claude_klabauter_root fixture and replace machine-local CLI calls") between the
# 2026-08-16 census and this one):
#
# `coordinator_core/ops/check_machine_local_regeneratability.py:277`
# (`_ladder_resolves`, the sole permanent correctness exception) is GONE from
# the live census: the site was refactored to `_ladder_snapshot`, which now
# spawns `machine-local dump --prefix repos --format json` (one batched call
# replacing the old per-key `get` probe -- see that function's own docstring,
# "W6/C6, 2026-08-19 amplification burn-down"). `dump` is not `get`/`keys`, so
# `_is_read_subcommand` no longer matches it -- correctly: it is a different
# subcommand this collector's brief was never scoped to (see module
# docstring, "WHAT COUNTS AS A HIT"). Verified only one `subprocess.run` call
# remains in that file and it is the `dump` call. Removed from the inventory;
# `test_collector_still_flags_regeneratability_ladder_probe_exception` below
# updated to pin the new zero-hit reality instead of the stale line.
#
# `coordinator_core/tests/test_engine_root_conformance.py:71` is GONE from
# the live census: its own `_machine_local_get` (now just `registry_get(key)`
# at line 63) was converted to a pure in-process read -- the test module's
# own docstring states this is deliberate, "so the module-scope
# `_resolve_doe_root()` call below stays zero-spawn without a spawn-shaped
# call node anywhere in this file." The production site it used to pin,
# `coordinator_core/engine_root.py` (now line 191, held by the one-engine
# plan per this file's Out of scope), is UNCHANGED and still spawns --
# the "lockstep" pairing this comment used to describe is broken (test
# converted, production site did not), but that is upstream code churn this
# dispatch's writes-scope (this file only) cannot correct by re-coupling
# them; noted here rather than silently dropped.
#
# 2026-08-20 C3 WIDENING (this dispatch): the collector's binary-marker text
# match previously ran on the `subprocess.<fn>` call's own unparsed source
# only, so a call whose binary token reached it through a local variable --
# `impl = _machine_local_impl()` then `subprocess.run([sys.executable, impl,
# "get", key])`, or a `for`-loop target bound to a resolver call -- was
# invisible: the unparsed call text contains the bare name `impl` (or
# `_ml_cand`), not any `_BINARY_MARKERS` substring. C3 widened the matcher to
# resolve same-scope, single-assignment local bindings (incl. a `for`-loop
# target's iterable) one level into the call's first positional argument,
# feeding both `_is_read_subcommand` and `_names_machine_local` a resolved
# copy of the call (see the resolver-call-indirection block above
# `_spawn_func_name`). This surfaced 22 new sites (line numbers per this
# census; no conversion performed, all writes-scoped to this file only):
#
# `coordinator/bin/lib/coordinator_registry.py:160` (`_registry_machine_local_get`,
# the `cmd = [sys.executable, impl, "get", key]` -> `subprocess.run(cmd)` shape)
# and `coordinator/bin/lib/coordinator_registry.py:431` (the module-scope
# `for _ml_cand in _mlir_machine_local_bin_candidates(): ... subprocess.run(
# [_ml_cand, "get", "repos.doe_claude"])` bootstrap loop) are this dispatch's
# own two named targets (this plan's § Problem). Both already try
# `machine_resolver`/`machine_local_impl_resolve`'s in-process reader FIRST
# (`_mlir_registry_get`) and only fall through to the CLI spawn on a miss --
# reason: "ladder-preserving fallback composition," the same reason the five
# `repos.*` rows above carry (this plan's own § Anti-scope pins this reason
# for these exact two sites; NOT converted further per this plan's own
# Anti-scope: "Do not convert the CLI spawn away entirely").
#
# The remaining 20 newly-visible sites are a DIFFERENT, pre-existing shape --
# independently reinvented `_machine_local_get`-shaped helpers this module's
# own docstring already names ("~15 independently reinvented
# `_machine_local_get`-shaped helpers"), each following the identical
# `impl = _machine_local_impl()` (or an equivalent resolver-returning
# variable); `subprocess.run([sys.executable, impl, "get"/"keys", ...])`
# shape inline in the call, with no `cmd`-named intermediate:
# `coordinator/bin/coordinator-doc-new.py:481`, `:523`,
# `coordinator/bin/coordinator-lesson-add.py:109`,
# `coordinator/bin/fan-out-dispatch.py:360`,
# `coordinator/bin/gen-claude-klabauter-root-pointer.py:139`,
# `coordinator/bin/lib/cc_invoke.py:396`,
# `coordinator/bin/lib/cli_shared.py:100`, `:148`,
# `coordinator/bin/tests/test_claude_machine_local.py:110`,
# `coordinator/bin/workday-start-step0.py:145`,
# `coordinator_core/ops/check_arch_audit_staleness.py:118`,
# `coordinator_core/ops/check_weekly_staleness.py:119`,
# `coordinator_core/ops/deliverable_rollup.py:133`,
# `coordinator_core/ops/list_review_trail_records.py:127`,
# `coordinator_core/ops/list_week_changelog.py:100`,
# `coordinator_core/ops/queue_append.py:754`,
# `coordinator_core/ops/workday_complete_backfill_scan.py:202`,
# `coordinator_core/orientation/regenerate_cache.py:282`,
# `coordinator_core/pyresolve.py:168`,
# `coordinator_core/roadmap/audit.py:173`.
# Reason: NOT converted -- this plan's own § Out of scope names this exact
# class ("The other 15 rows in the collector's burn-down inventory. Widening
# the matcher may surface more; converting the pre-existing 15 is not this
# plan's deliverable (C3 dispositions newly-visible sites only)") -- these 20
# are that same pre-existing-helper family, simply not individually visible
# to the collector before this widening. Converting each to
# `machine_resolver.registry_get` in-process-first, one file at a time, with
# its own test-shape audit (per the C7b precedent: a site whose own test
# fakes `machine-local` as a real subprocess stub needs a fixture rewrite,
# not a blind swap), is a dedicated future pass' deliverable, not this one's.


def _is_read_subcommand(call: ast.Call) -> bool:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and arg.value in ("get", "keys"):
            return True
        if isinstance(arg, (ast.List, ast.Tuple)):
            for elt in arg.elts:
                if isinstance(elt, ast.Constant) and elt.value in ("get", "keys"):
                    return True
    return False


def _names_machine_local(text: str) -> bool:
    lowered = text
    return any(marker in lowered for marker in _BINARY_MARKERS)


def _spawn_func_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_FUNCS:
        # Restrict to `subprocess.<fn>` / `<alias>.<fn>` attribute access,
        # not an arbitrary same-named method on an unrelated object -- the
        # sibling collector (`test_no_unbatched_per_item_git_spawn.py`)
        # accepts the same imprecision for the same reason: a project-wide
        # convention of importing the stdlib `subprocess` module directly.
        return func.attr
    if isinstance(func, ast.Name) and func.id in _SPAWN_FUNCS:
        return func.id
    return None


# ---------------------------------------------------------------------------
# Resolver-call indirection widening (this dispatch, C3).
#
# The collector requires a binary marker AND a `"get"`/`"keys"` literal in
# ONE unparsed call. Two real sites (`coordinator/bin/lib/coordinator_registry.py`
# `_registry_machine_local_get` and its module-scope bootstrap loop) both
# reach the binary marker through a name bound to a resolver call, one level
# removed from the call expression -- `cmd = [sys.executable, impl, "get",
# key]` then `subprocess.run(cmd, ...)`, and `for _ml_cand in
# _mlir_machine_local_bin_candidates(): ... subprocess.run([_ml_cand, "get",
# ...])`. Neither is visible to a scan of the call's own unparsed text alone.
#
# THE MECHANISM: before matching, build a map of simple local bindings for
# the immediately-enclosing scope only (function or module body, same-scope,
# single-assignment -- an `ast.Assign` to a bare `Name` target, or a `for`
# loop's `Name` target bound to its iterable expression). A name assigned
# more than once in scope is deliberately left unresolved (report it, don't
# guess which assignment reached the call). When a `subprocess.<fn>` call's
# first positional argument contains a bare `ast.Name` present in this map,
# substitute its bound value; when that bound value itself is a bare `Name`
# that substitution just produced (the `cmd` -> `impl` two-hop case), resolve
# one further level and stop -- deeper chains are not walked. This feeds a
# resolved COPY of the call node into BOTH existing legs -- `_is_read_subcommand`
# (AST-based, walks `call.args`) and `_names_machine_local` (text-based, reads
# the unparsed call source) -- not just the text leg, per this dispatch's
# brief: resolving only for one leg leaves the site invisible for a different
# reason than either named escape.
#
# NOT done: widening `_BINARY_MARKERS`. Both real sites' bound values already
# contain the existing `machine_local` marker as a substring once resolved
# (`_registry_machine_local_impl`, `_mlir_machine_local_bin_candidates`) --
# widening the marker list would not have closed either site and was
# considered and rejected for this dispatch (see this file's own inventory
# notes for the rejected marker candidates).
# ---------------------------------------------------------------------------


def _record_binding(
    name: str,
    value: ast.expr,
    bindings: dict[str, ast.expr],
    multi: set[str],
) -> None:
    if name in multi:
        return
    if name in bindings:
        del bindings[name]
        multi.add(name)
    else:
        bindings[name] = value


def _collect_scope_bindings(
    stmts: list[ast.stmt],
    bindings: dict[str, ast.expr],
    multi: set[str],
) -> None:
    """Populate `bindings` with same-scope, single-assignment simple `Name`
    bindings found in `stmts`, flattening control-flow blocks (`if`/`for`/
    `while`/`try`/`with`) but never descending into a nested function or
    class body -- that is a different scope, handled separately by the
    caller.

    ORDER-INSENSITIVE BY DESIGN, NOT JUST IN EFFECT: this pass builds the
    whole binding map before `_scan_scope` walks any statement, so "single
    assignment" here means "exactly one assignment site anywhere in scope,"
    not "exactly one assignment site preceding the call." A call that
    source-precedes its resolved name's sole assignment would still resolve.
    This is deliberately not tightened to an execution-order check: real
    forward-reference use of a local name is a `NameError` at runtime (dead
    code), so there is no realistic false-positive path through this
    asymmetry -- see review-findings-s3.md minor note 1."""
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            _record_binding(stmt.targets[0].id, stmt.value, bindings, multi)
        if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
            _record_binding(stmt.target.id, stmt.iter, bindings, multi)
        for field in ("body", "orelse", "finalbody"):
            child = getattr(stmt, field, None)
            if isinstance(child, list):
                _collect_scope_bindings(child, bindings, multi)
        if isinstance(stmt, ast.Try):
            for handler in stmt.handlers:
                _collect_scope_bindings(handler.body, bindings, multi)


class _CallCollector(ast.NodeVisitor):
    """Collects `ast.Call` nodes within a statement, WITHOUT descending into
    a nested function/class/lambda body -- those are separate scopes, walked
    by `_scan_scope`'s own recursion, each with their own binding map."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _substitute_descendant_names(node: ast.AST, bindings: dict[str, ast.expr]) -> ast.AST:
    """One pass over a deep copy of `node`: replace every bare `Name`
    descendant bound in `bindings` with its bound value. Does not revisit a
    freshly-substituted replacement's own descendants -- that is the "one
    further level" the caller applies explicitly, not open recursion."""

    class _Sub(ast.NodeTransformer):
        def visit_Name(self, n: ast.Name) -> ast.AST:
            if n.id in bindings:
                return copy.deepcopy(bindings[n.id])
            return n

    return _Sub().visit(copy.deepcopy(node))


def _resolve_arg0(arg0: ast.expr, bindings: dict[str, ast.expr]) -> ast.expr:
    resolved = _substitute_descendant_names(arg0, bindings)
    if isinstance(arg0, ast.Name) and arg0.id in bindings:
        # `arg0` itself was a bound Name (e.g. `cmd`) resolved to its bound
        # value in the pass above -- that bound value's OWN descendant Names
        # (e.g. `impl` inside `[sys.executable, impl, "get", key]`) were not
        # visited by that pass (they belong to a value substituted IN, not
        # walked as part of the original tree), so resolve one further level.
        resolved = _substitute_descendant_names(resolved, bindings)
    return resolved  # type: ignore[return-value]


def _resolved_call(call: ast.Call, bindings: dict[str, ast.expr]) -> ast.Call:
    if not call.args:
        return call
    resolved_arg0 = _resolve_arg0(call.args[0], bindings)
    try:
        unchanged = ast.dump(resolved_arg0) == ast.dump(call.args[0])
    except Exception:
        unchanged = False
    if unchanged:
        return call
    new_call = copy.deepcopy(call)
    new_call.args[0] = resolved_arg0
    return new_call


def _scan_scope(
    stmts: list[ast.stmt],
    bindings: dict[str, ast.expr],
    hits: list[tuple[str, int]],
    rel_posix: str,
) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner_bindings: dict[str, ast.expr] = {}
            _collect_scope_bindings(stmt.body, inner_bindings, set())
            _scan_scope(stmt.body, inner_bindings, hits, rel_posix)
            continue
        if isinstance(stmt, ast.ClassDef):
            _scan_scope(stmt.body, {}, hits, rel_posix)
            continue
        collector = _CallCollector()
        collector.visit(stmt)
        for call in collector.calls:
            if _spawn_func_name(call) is None:
                continue
            resolved = _resolved_call(call, bindings)
            if not _is_read_subcommand(resolved):
                continue
            try:
                unparsed = ast.unparse(resolved)
            except Exception:
                continue
            if _names_machine_local(unparsed):
                hits.append((rel_posix, call.lineno))


def _scan_file(rel_posix: str, text: str) -> list[tuple[str, int]]:
    if rel_posix in _SURFACE_MODULES:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[tuple[str, int]] = []
    module_bindings: dict[str, ast.expr] = {}
    _collect_scope_bindings(tree.body, module_bindings, set())
    _scan_scope(tree.body, module_bindings, hits, rel_posix)
    return hits


def _collect_all_hits() -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for root in (
        _REPO_ROOT / "coordinator_core",
        _REPO_ROOT / "coordinator" / "bin",
        _REPO_ROOT / "coordinator" / "lib",
        _REPO_ROOT / "bin",
    ):
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel_posix, file_path in discovered:
            text = file_path.read_text(encoding="utf-8")
            repo_rel = (root / rel_posix).relative_to(_REPO_ROOT).as_posix()
            hits.extend(_scan_file(repo_rel, text))
    return hits


def test_collector_fires_on_the_pre_conversion_shape() -> None:
    """Proof-of-fire: reproduces the exact pre-`9245fd0d5` `_tier_a5` shape
    (`subprocess.run([*ml_argv, "get", key], ...)`) as a source fixture and
    asserts the collector flags it -- "a gate that cannot see the defect is
    not covering it" (this dispatch's own instructions)."""
    fixture = '''
import subprocess

def _tier_a5():
    ml_argv = _machine_local_launch_argv(ml_bin)
    for key in keys:
        get_proc = subprocess.run(
            [*ml_argv, "get", key],
            capture_output=True,
        )
'''
    hits = _scan_file("coordinator_core/ops/_fixture_unconverted.py", fixture)
    assert hits, "collector failed to flag the known pre-conversion `machine-local get` shape"


def test_collector_flags_call_bound_name_indirection() -> None:
    """Real site-1 shape (`coordinator_registry.py::_registry_machine_local_get`):
    a Call-bound Name (`impl`) reached through a list-literal local (`cmd`) --
    two hops from the `subprocess.run` call to the binary-marker text."""
    fixture = '''
import subprocess

def _registry_machine_local_get(key):
    impl = _registry_machine_local_impl()
    cmd = [sys.executable, impl, "get", key]
    subprocess.run(cmd)
'''
    hits = _scan_file("coordinator_core/ops/_fixture_call_bound_name.py", fixture)
    assert hits, "collector failed to flag the Call-bound-Name-through-a-list-literal shape"


def test_collector_flags_for_loop_target_indirection() -> None:
    """Real site-2 shape (module-scope bootstrap): a `for`-loop target
    (`_cand`) reached through its iterable expression, inline inside the
    `subprocess.run` argument list."""
    fixture = '''
import subprocess

for _ml_cand in _mlir_machine_local_bin_candidates():
    subprocess.run([_ml_cand, "get", key])
'''
    hits = _scan_file("coordinator_core/ops/_fixture_for_loop_target.py", fixture)
    assert hits, "collector failed to flag the for-loop-target-through-its-iterable shape"


def test_collector_silent_on_converted_discover_working_repos() -> None:
    """`discover_working_repos.py` was converted in `9245fd0d5` to
    `_merged_flat_registry` (zero subprocesses) -- the collector must not
    flag it post-conversion."""
    path = _REPO_ROOT / "coordinator_core" / "ops" / "discover_working_repos.py"
    hits = _scan_file("coordinator_core/ops/discover_working_repos.py", path.read_text(encoding="utf-8"))
    assert hits == []


def test_collector_silent_on_regeneratability_now_dump_shaped() -> None:
    """`_ladder_resolves` was the sole read-side EXCEPTION as of the
    2026-08-16 census (see the burn-down comment's "2026-08-20 C3 WIDENING --
    DROPPED ROWS" note): it has since been refactored (`_ladder_snapshot`) to
    a single batched `machine-local dump --prefix repos --format json` call,
    not a per-key `get`. `dump` is not `get`/`keys`, so it is out of this
    collector's brief by construction (module docstring, "WHAT COUNTS AS A
    HIT") and must NOT be flagged -- this pins the post-refactor reality so a
    future re-add of a `get`-shaped probe in this file is caught by
    `test_inventory_is_exhaustive_and_matches_known_sites` below, not silently
    re-exempted by a stale assertion here."""
    path = (
        _REPO_ROOT
        / "coordinator_core"
        / "ops"
        / "check_machine_local_regeneratability.py"
    )
    hits = _scan_file(
        "coordinator_core/ops/check_machine_local_regeneratability.py",
        path.read_text(encoding="utf-8"),
    )
    assert hits == []


def test_inventory_is_exhaustive_and_matches_known_sites() -> None:
    """The live census must equal `KNOWN_UNCONVERTED_SITES` exactly -- a new,
    unlisted hit fails closed (regrowth caught), and a listed site that no
    longer appears must be removed from the list (burn-down is visible, not
    silently stale). This is the completeness pin beside the ratchet: a new
    row cannot enter below this gate's reach."""
    hits = {f"{path}:{lineno}" for path, lineno in _collect_all_hits()}
    stale = KNOWN_UNCONVERTED_SITES - hits
    new = hits - KNOWN_UNCONVERTED_SITES
    assert not stale, f"sites converted but still listed -- remove from KNOWN_UNCONVERTED_SITES: {sorted(stale)}"
    assert not new, f"new machine-local read-side shell-out(s) -- convert to machine_resolver, do not add here: {sorted(new)}"
