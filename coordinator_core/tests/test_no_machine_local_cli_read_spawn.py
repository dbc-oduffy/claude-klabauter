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
    exists to draw). Correctness boundary per this dispatch's brief -- left
    as the sole read-side EXCEPTION in the inventory below, not converted.
"""
from __future__ import annotations

import ast
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
        "coordinator/bin/claude-doe.py:358",
        "coordinator/bin/claude-doe.py:379",
        "coordinator/bin/lib/git_hook_install.py:159",
        "coordinator/lib/percolate/resolve_target.py:215",
        "coordinator/lib/percolate/targets.py:120",
        "coordinator/lib/resolve-coordinator-clone.py:225",
        "coordinator_core/claude_klabauter_root.py:178",
        "coordinator_core/ops/check_machine_local_regeneratability.py:277",
        "coordinator_core/ops/gen_claude_doe_shim.py:414",
        "coordinator_core/ops/gen_doe_root_pointer.py:127",
        "coordinator_core/ops/new_project_scaffold.py:159",
        "coordinator_core/ops/render_template_tree.py:96",
        "coordinator_core/ops/repo_bootstrap.py:141",
        "coordinator_core/resolve_coordinator_clone.py:143",
        "coordinator_core/tests/test_engine_root_conformance.py:71",
    }
)
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
# mirrors `coordinator_core.claude_klabauter_root`'s Rung 2 CLI fallback for
# `engine.working_repos.doe_claude` -- `coordinator_core/claude_klabauter_root.py:178`
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


def _scan_file(rel_posix: str, text: str) -> list[tuple[str, int]]:
    if rel_posix in _SURFACE_MODULES:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _spawn_func_name(node) is None:
            continue
        if not _is_read_subcommand(node):
            continue
        try:
            unparsed = ast.unparse(node)
        except Exception:
            continue
        if _names_machine_local(unparsed):
            hits.append((rel_posix, node.lineno))
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


def test_collector_silent_on_converted_discover_working_repos() -> None:
    """`discover_working_repos.py` was converted in `9245fd0d5` to
    `_merged_flat_registry` (zero subprocesses) -- the collector must not
    flag it post-conversion."""
    path = _REPO_ROOT / "coordinator_core" / "ops" / "discover_working_repos.py"
    hits = _scan_file("coordinator_core/ops/discover_working_repos.py", path.read_text(encoding="utf-8"))
    assert hits == []


def test_collector_still_flags_regeneratability_ladder_probe_exception() -> None:
    """`_ladder_resolves` is the sole read-side EXCEPTION (see module
    docstring): conversion to `registry_get` was attempted and reverted
    because the CLI's ladder autodiscovers beyond the two-file TOML merge.
    It must remain a live hit, and its row must stay in
    `KNOWN_UNCONVERTED_SITES` -- this pins the exception so it cannot
    silently drop out of the inventory."""
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
    assert hits == [("coordinator_core/ops/check_machine_local_regeneratability.py", 277)]


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
