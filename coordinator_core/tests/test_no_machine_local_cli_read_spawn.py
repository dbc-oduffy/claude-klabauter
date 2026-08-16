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
        "coordinator_core/claude_klabauter_root.py:178",
        "coordinator_core/ops/bootstrap_repo.py:230",
        "coordinator_core/ops/capture_fan_out_threshold.py:86",
        "coordinator_core/ops/central_run_due.py:132",
        "coordinator_core/ops/check_machine_local_regeneratability.py:277",
        "coordinator_core/ops/check_registry_codename_leak.py:181",
        "coordinator_core/ops/coordinator_doe_root.py:158",
        "coordinator_core/ops/ensure_doe_clone.py:68",
        "coordinator_core/ops/gen_claude_doe_shim.py:404",
        "coordinator_core/ops/gen_doe_root_pointer.py:114",
        "coordinator_core/ops/gen_doe_root_pointer.py:193",
        "coordinator_core/ops/install_shell_init_guard_seam.py:138",
        "coordinator_core/ops/new_project_scaffold.py:152",
        "coordinator_core/ops/new_project_scaffold.py:244",
        "coordinator_core/ops/render_template_tree.py:94",
        "coordinator_core/ops/repo_bootstrap.py:126",
        "coordinator_core/ops/setup_seed_health_ledger.py:139",
        "coordinator_core/ops/verify_ue_overrides.py:113",
        "coordinator_core/resolve_coordinator_clone.py:143",
        "coordinator_core/snippet_sync/registry.py:468",
        "coordinator_core/tests/test_engine_root_conformance.py:71",
    }
)
# Burn-down inventory (24 sites, 2026-08-16 census, post M-01 fan-out fix).
# `_ladder_resolves` (`check_machine_local_regeneratability.py`),
# `_tier_a5`/`_publish_mirror_keys` (`discover_working_repos.py`),
# `_apply_machine_local_days_override`/`_default_parent_roots`
# (`cruft-sweep.py`), and `_registry_repo_roots` (`git_hook_install.py`) are
# already converted and NOT in this set. Each remaining row is a real,
# re-verified-at-HEAD `machine-local get`/`keys` CLI read shell-out;
# converting one means removing its row here, not adding to it.
# `test_engine_root_conformance.py:71` is a TEST asserting the current CLI-spawn
# behavior of a production site elsewhere -- it will need updating in lockstep
# with whichever production site it pins once that site converts, not on its
# own.


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
    for root in (_REPO_ROOT / "coordinator_core", _REPO_ROOT / "coordinator" / "bin"):
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
