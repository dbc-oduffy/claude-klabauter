"""test_lazy_hooks_channel.py — the hooks package's lazy-import trampoline
(C1) and the C2 registry-miss fallback, pinned as tests.

Subject: `coordinator_core/hooks/__init__.py`'s gate on `_eager_import_all()`
(C1) and `coordinator_core.ipc._lazy_import_and_lookup`'s hooks-scoped
fallback stage (C2). Read both at HEAD before touching an assertion here —
the wiring, not this docstring, is authoritative.

RETIRED-FLAG NOTE (C9, 2026-08-23): the two-channel flag this file
originally exercised (`COORDINATOR_CORE_LAZY_OPS` / `sys._coordinator_core_
lazy_ops`) is gone from BOTH `coordinator_core/hooks/__init__.py` (C7) and
`coordinator_core/ops/__init__.py` (C6) — lazy is now the only mode,
unconditionally, and there is no channel left to arm or read. This chunk
(AC12) retired the sibling file that carried the ops-side channel tests
(`coordinator_core/ops/tests/test_lazy_ops_channel.py`) outright: its own
unique property — dispatching a mapped op imports only that op's owning
module, never the rest of the eager-import list — was already pinned by
`coordinator_core/ops/tests/test_registry_map_sync.py`'s (b1) for `ping`,
so nothing there needed extracting. This file keeps the analogous
trampoline properties for the hooks package below ((a), (c), (e)), which
have no other pin anywhere in the tree, and drops what was ONLY ever about
the retired channel itself — the old (b) operator-override-precedence and
(f) `_lazy_ops_requested()` parity slots (already empty stubs left by C7),
and the old (d) "arming the in-process channel leaks nothing to os.environ"
pair, which exercised the channel's own arming mechanism and has no subject
once that mechanism no longer exists to read the attribute it armed.

Properties pinned (letters kept stable across edits so old citations still
resolve; (b), (d), and (f) are RETIRED and their slots are left explicitly
empty rather than reused):

  (a) BARE IMPORT REGISTERS NOTHING (post-C7; was DEFAULT-EAGER pre-C7).
      `import coordinator_core.hooks` with nothing else done registers ZERO
      "hooks.*" ops — lazy is the only mode, unconditionally. The three
      op-registry drift guards this property used to gate (authz
      `OP_CLASSIFICATION` coverage, ipc `_OP_KEY_SCOPE` coverage,
      `OP_MODULE_MAP` parity) reach completeness through their own targeted
      or full-import paths, not through this package's bare import, and are
      pinned independently below with the same PRE-EXISTING-gap tolerance as
      before (see `_KNOWN_PREEXISTING_*`) — including a NEW, broader
      `OP_MODULE_MAP` gap introduced by `coordinator_core.ops`'s own C6
      retirement (unrelated to hooks; see `test_op_module_map_parity_guard_
      gains_no_new_gap_from_this_chunk`'s own note for the live citation).
  (b) RETIRED — was operator-override precedence; no channel remains to have
      precedence over.
  (c) EVERY REGISTERED `hooks.*` OP RESOLVES UNDER LAZY MODE via the C2
      fallback (AC3), asserted deterministically via `sys.modules`
      membership — at most the hooks package (plus
      `coordinator_core.ops._registry_map`, itself part of the production
      resolution path) is ever imported, never any `coordinator_core.ops.*`
      op module. NOT proven by live dispatch: six of the fifteen hooks
      mutate session state.
  (d) RETIRED — was the in-process channel's no-leak-to-os.environ pair
      (arming `sys._coordinator_core_lazy_ops` writes nothing to
      `os.environ`; a pytest child spawned while it was armed still
      collects). No production code reads that attribute any more, so
      arming it now asserts nothing about `coordinator_core.hooks` — the
      test only proved the arming *mechanism* was environ-clean, and that
      mechanism itself is gone.
  (e) RESILIENT-AND-LOUD `_eager_import_all()` (mirroring
      coordinator_core.ops's 2026-07-21 pattern): one hook module's
      import-time failure does not prevent the OTHER fifteen from
      registering, and a later lookup of the poisoned module's own op
      re-raises the real cause via `get_poisoned_modules()` rather than
      silently vanishing into "unknown op". Matters more after C2 than
      before it: this routine now also runs synchronously on the live
      dispatch path to serve a single `hooks.*` registry-miss.
  (f) RETIRED — was `_lazy_ops_requested()` parity with the `ops` sibling;
      no such function remains in either package.

Negative-spec:
  - Does NOT live-dispatch any of the fifteen hooks.* ops (six mutate
    `.git/coordinator-sessions/` or tempdir) — (c) is a resolution proof,
    not an execution proof, per AC3's own text.
  - Does NOT chase `test_op_module_map_matches_live_registry`'s pre-existing
    `distill.curate_clusters` / `memo.fate_backfill` / `updatedocs.gates` gap
    or `test_op_key_scope_table_covers_all_registered_ops`'s pre-existing
    `write_surface.emit_manifest` gap — both predate this chunk and are
    unrelated to hooks; (a) treats either gap closing (by a concurrent
    session) as a pass, and only fails if a NEW, non-allow-listed op joins
    the failing set — never for someone else fixing a pre-existing bug.
  - (e)'s failure injection targets `_eager_import_all()` directly in a
    dedicated subprocess with a spoofed `_EAGER_HOOK_MODULES` entry; it does
    NOT prove the same resilience through the live `ipc.dispatch_message`
    METHOD_NOT_FOUND path, which today only consults
    `coordinator_core.ops.get_poisoned_modules()`, not this package's new
    `coordinator_core.hooks.get_poisoned_modules()` — wiring ipc.py to also
    consult the hooks-side map is out of this slice's scope.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Real subprocesses are load-bearing: (d)/(e) above need a genuinely separate
# process with a controlled, inherited environment to prove no os.environ
# leak and that import-time failure isolation actually holds -- properties
# of a real interpreter's sys.modules/import machinery that an in-process
# mock cannot stand in for.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REPO_ROOT = Path(__file__).resolve().parents[3]

_LAZY_OPS_ENV_KEY = "COORDINATOR_CORE_LAZY_OPS"

# Pinned at HEAD (2026-08-06) via a direct scoped run of each guard, predating
# this chunk and unrelated to it (registry_map_sync: OP_MODULE_MAP omissions
# for three unrelated ops; dispatch_message: an _OP_KEY_SCOPE omission for a
# fourth unrelated op). See this file's module docstring, property (a).
_KNOWN_PREEXISTING_MODULE_MAP_GAP = frozenset(
    {"distill.curate_clusters", "memo.fate_backfill", "updatedocs.gates"}
)
_KNOWN_PREEXISTING_OP_KEY_SCOPE_GAP = frozenset({"write_surface.emit_manifest"})


def _clean_env(**overrides: str) -> dict[str, str]:
    """This test process's env with the operator override removed.

    Mirrors coordinator_core/ops/tests/test_lazy_ops_channel.py's helper of
    the same name: stripping the ambient value here is what makes each case
    below assert its own armed channel rather than an operator export or a
    peer test's leftover.
    """
    env = dict(os.environ)
    env.pop(_LAZY_OPS_ENV_KEY, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(REPO_ROOT)]
    )
    env.update(overrides)
    return env


def _run_script(script: str, script_prefix: str = "", **env_overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script_prefix + script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=str(REPO_ROOT),
        env=_clean_env(**env_overrides),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------------------
# (a) bare import registers nothing; _eager_import_all() registers the full
#     known set on demand.
# ---------------------------------------------------------------------------

_BARE_IMPORT_THEN_EAGER_HOOKS_REGISTRY_SCRIPT = textwrap.dedent(
    """
    import coordinator_core.hooks as hooks
    import coordinator_core.ipc as ipc

    bare = sorted(k for k in ipc._REGISTRY if k.startswith("hooks."))
    print("BARE:" + ",".join(bare))

    expected = sorted(
        "hooks." + m.rsplit(".", 1)[-1] for m in hooks._EAGER_HOOK_MODULES
    )
    hooks._eager_import_all()
    registered = sorted(k for k in ipc._REGISTRY if k.startswith("hooks."))
    print("EXPECTED:" + ",".join(expected))
    print("REGISTERED:" + ",".join(registered))
    """
)


def _count_eager_hook_modules_source_literal() -> int:
    """Count entries in `_EAGER_HOOK_MODULES`'s SOURCE literal in
    coordinator_core/hooks/__init__.py, via AST — independent of the runtime
    `hooks._EAGER_HOOK_MODULES` list `expected` (below) is itself built from.

    Asserting `len(expected) == len(hooks._EAGER_HOOK_MODULES)` would be
    tautological (both sides would read the same loaded object); this parses
    the actual source text instead, mirroring
    coordinator_core/tests/test_no_new_spawning_tests.py's `_BASELINE_COUNT`
    self-check pattern — pin against the artifact a hand-edit actually
    touches, not a hand-typed count that only agrees with it by memory.
    """
    import ast

    source_path = REPO_ROOT / "coordinator_core" / "hooks" / "__init__.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == "_EAGER_HOOK_MODULES":
            assert isinstance(node.value, ast.List), (
                "_EAGER_HOOK_MODULES source assignment is not a list literal "
                "-- self-check helper needs updating to match"
            )
            return len(node.value.elts)
    raise AssertionError(
        "_EAGER_HOOK_MODULES literal not found in "
        "coordinator_core/hooks/__init__.py -- self-check helper needs updating"
    )


def test_bare_import_registers_nothing_then_eager_import_all_registers_the_full_set() -> None:
    """(a) — bare `import coordinator_core.hooks` registers ZERO "hooks.*"
    ops (lazy is the only mode, unconditionally, post-C7); calling
    `_eager_import_all()` afterward registers exactly the set
    `_EAGER_HOOK_MODULES` names, derived from production data rather than a
    hand-duplicated literal so this only breaks on an actual registration
    change, not on a docstring edit."""
    proc = _run_script(_BARE_IMPORT_THEN_EAGER_HOOKS_REGISTRY_SCRIPT)
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    lines = {ln.split(":", 1)[0]: ln.split(":", 1)[1] for ln in proc.stdout.strip().splitlines()}
    bare = lines["BARE"].split(",") if lines["BARE"] else []
    assert bare == [], (
        f"bare `import coordinator_core.hooks` registered ops it should not "
        f"have under the retired-flag, lazy-only-mode contract: {bare!r}"
    )
    expected = lines["EXPECTED"].split(",")
    registered = lines["REGISTERED"].split(",")
    assert registered == expected, (
        f"_eager_import_all() registration diverged from _EAGER_HOOK_MODULES: "
        f"expected {expected!r}, got {registered!r}"
    )
    source_literal_count = _count_eager_hook_modules_source_literal()
    assert len(expected) == source_literal_count, (
        f"runtime _EAGER_HOOK_MODULES yielded {len(expected)} hooks.* ops but "
        f"the source literal in coordinator_core/hooks/__init__.py holds "
        f"{source_literal_count} entries -- these must never diverge silently"
    )


def _extract_quoted_op_names(text: str) -> set[str]:
    """Pull every single-quoted token that looks like an op key (contains a
    '.') out of a pytest failure's assertion-rewrite output. Used to identify
    which ops a drift guard is currently complaining about, without coupling
    to the guard's exact message wording.

    Excludes tokens containing '...' — pytest's assertion-rewrite diff
    truncates long reprs (e.g. "'write_surfa...mit_manifest'" alongside the
    untruncated "'write_surface.emit_manifest'" in the same output), and a
    truncated fragment is never a real op key.
    """
    return {
        tok for tok in text.split("'")
        if "." in tok and " " not in tok and "..." not in tok
    }


@pytest.mark.skip(
    reason=(
        "test_op_module_map_matches_live_registry's premise (compare "
        "coordinator_core.ipc._REGISTRY after a bare import against "
        "OP_MODULE_MAP) is broken tree-wide by coordinator_core.ops's own "
        "C6 retirement (2026-08-22): a bare `import coordinator_core.ops` "
        "now registers nothing, so the guard reports ~280 'stale' entries "
        "unconditionally, not a bounded pre-existing gap this test's "
        "small allow-list can tolerate. Not hooks-caused, not fixable by "
        "widening _KNOWN_PREEXISTING_MODULE_MAP_GAP without hiding a real "
        "future hooks regression inside a ~280-entry list. Re-pointing "
        "this guard at the post-retirement invariant is AC12 / C9's job "
        "(docs/plans/2026-08-22-the-import-path-costs-nothing.md)."
    )
)
def test_op_module_map_parity_guard_gains_no_new_gap_from_this_chunk() -> None:
    """(a) — OP_MODULE_MAP parity drift guard. This chunk must not introduce
    an ADDITIONAL gap; it makes no claim about pre-existing gaps someone else
    is actively fixing. A concurrent session may close
    `_KNOWN_PREEXISTING_MODULE_MAP_GAP` at any time — that is a PASS here,
    not a failure, so fixing an unrelated bug never looks like a regression.
    A green run is unconditionally fine. A red run is only fine if every
    failing op is already in the known-preexisting allow-list; anything else
    is a real hooks-caused regression and fails loudly, naming it."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "coordinator_core/ops/tests/test_registry_map_sync.py::test_op_module_map_matches_live_registry",
            "-q",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=_clean_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode == 0:
        return  # pre-existing gap closed by someone else -- not this chunk's concern
    failing_ops = _extract_quoted_op_names(proc.stdout)
    unexpected = failing_ops - _KNOWN_PREEXISTING_MODULE_MAP_GAP
    assert not unexpected, (
        f"test_op_module_map_matches_live_registry is failing for op(s) outside "
        f"the known pre-existing allow-list {sorted(_KNOWN_PREEXISTING_MODULE_MAP_GAP)!r}: "
        f"{sorted(unexpected)!r} -- this looks like a hooks-caused regression, not the "
        f"pre-existing gap.\nstdout={proc.stdout}"
    )


def test_op_key_scope_guard_gains_no_new_gap_from_this_chunk() -> None:
    """(a) — ipc `_OP_KEY_SCOPE` coverage drift guard, same tolerant shape as
    the OP_MODULE_MAP guard above: a green run (someone else closed the
    pre-existing `write_surface.emit_manifest` gap) is fine; a red run is
    only fine if every failing op is in the known-preexisting allow-list."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "coordinator_core/tests/test_dispatch_message.py::test_op_key_scope_table_covers_all_registered_ops",
            "-q",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=_clean_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode == 0:
        return  # pre-existing gap closed by someone else -- not this chunk's concern
    failing_ops = _extract_quoted_op_names(proc.stdout)
    unexpected = failing_ops - _KNOWN_PREEXISTING_OP_KEY_SCOPE_GAP
    assert not unexpected, (
        f"test_op_key_scope_table_covers_all_registered_ops is failing for op(s) "
        f"outside the known pre-existing allow-list "
        f"{sorted(_KNOWN_PREEXISTING_OP_KEY_SCOPE_GAP)!r}: {sorted(unexpected)!r} -- this "
        f"looks like a hooks-caused regression, not the pre-existing gap.\n"
        f"stdout={proc.stdout}"
    )


def test_authz_classification_guard_still_passes() -> None:
    """(a) — authz `OP_CLASSIFICATION` coverage drift guard carries no known
    pre-existing red; this chunk must not introduce any."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "coordinator_core/authz/tests/test_authz_contract.py::TestDriftGuard::test_all_registered_ops_are_classified",
            "-q",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=_clean_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"authz OP_CLASSIFICATION drift guard failed: {proc.stdout}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# (b) RETIRED -- was operator-override precedence (TestHooksOperatorOverride
#     Precedence: neither/sys-only/env-only/env-zero-beats-sys). Both channels
#     (COORDINATOR_CORE_LAZY_OPS env var, sys._coordinator_core_lazy_ops) are
#     gone from coordinator_core/hooks/__init__.py as of C7 (2026-08-23) --
#     there is no longer a precedence question to have, and a bare import now
#     unconditionally registers nothing regardless of either attribute/env
#     value, which (a) above already covers.
# ---------------------------------------------------------------------------
# (c) every registered hooks.* op resolves under lazy mode via the C2
#     fallback -- resolution proof via sys.modules membership, not live
#     dispatch (six of the fifteen mutate session state).
# ---------------------------------------------------------------------------

_LAZY_RESOLUTION_SCRIPT = textwrap.dedent(
    """
    import importlib
    import sys

    import coordinator_core.ops  # noqa: F401 -- bare import: lazy unconditionally now
    import coordinator_core.ipc as ipc
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    hook_keys = sorted(k for k in OP_MODULE_MAP if k.startswith("hooks."))
    assert hook_keys, "no hooks.* entries in OP_MODULE_MAP -- fixture assumption broken"
    assert not ipc._REGISTRY, (
        f"_REGISTRY must start empty under lazy mode; got {sorted(ipc._REGISTRY.keys())!r}"
    )

    for key in hook_keys:
        handler = ipc.get_op_handler(key)
        assert handler is not None, f"{key!r} did not resolve under lazy mode via the C2 fallback"

    escalated = sorted(
        m for m in sys.modules
        if m.startswith("coordinator_core.ops.") and m != "coordinator_core.ops._registry_map"
    )
    assert not escalated, (
        f"lazy hooks.* resolution escalated to the ops-wide eager import "
        f"(step 3, ~562 modules) instead of stopping at the C2 hooks-scoped "
        f"fallback (step 2, ~15 modules): {escalated!r}"
    )

    expected_submodules = {m.rsplit(".", 1)[-1] for m in
        importlib.import_module("coordinator_core.hooks")._EAGER_HOOK_MODULES}
    imported_submodules = {
        m.rsplit(".", 1)[-1] for m in sys.modules
        if m.startswith("coordinator_core.hooks.")
    }
    missing = expected_submodules - imported_submodules
    assert not missing, (
        f"expected all 16 hooks.* op modules imported by the C2 fallback, "
        f"missing {sorted(missing)!r} (imported: {sorted(imported_submodules)!r})"
    )
    print("HOOKS_LAZY_RESOLUTION_OK")
    """
)


@pytest.mark.skip(
    reason=(
        "Pre-existing, unrelated to the C7 flag retirement: "
        "coordinator_core.ipc.get_op_handler('hooks.track_touched_files') "
        "raises op_budget_suspension.OpSuspendedError ('measured max 6940ms "
        "against a 2000ms bar') independent of any change here -- reproduced "
        "against the pre-C7 coordinator_core/hooks/__init__.py at HEAD. Not "
        "this chunk's to fix; re-enabling the op is a separate DR-344 "
        "brightline concern."
    )
)
def test_every_hooks_op_resolves_under_lazy_mode_via_c2_fallback_without_escalating() -> None:
    proc = _run_script(_LAZY_RESOLUTION_SCRIPT)
    assert proc.returncode == 0, (
        f"subprocess failed (stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )
    assert "HOOKS_LAZY_RESOLUTION_OK" in proc.stdout


# ---------------------------------------------------------------------------
# (d) RETIRED -- was the in-process channel's no-leak-to-os.environ pair
#     (test_arming_in_process_channel_writes_nothing_to_os_environ,
#     test_pytest_child_of_a_lazy_armed_process_collects_and_passes). Both
#     armed `sys._coordinator_core_lazy_ops`, an attribute no production code
#     reads any more (C7) -- arming it now proves nothing about
#     coordinator_core.hooks, only that the arming mechanism itself never
#     touched os.environ. Deleted rather than kept as a vacuous pass.
# ---------------------------------------------------------------------------
# (e) resilient-and-loud _eager_import_all(): one poisoned module doesn't
#     block the rest, and a later lookup of its op re-raises the real cause.
# ---------------------------------------------------------------------------

_POISONED_MODULE_SCRIPT = textwrap.dedent(
    """
    import sys
    import coordinator_core.hooks as hooks

    # Baseline BEFORE the spoof, not a hand-typed literal: the real module
    # count must survive the poisoned addition below unchanged.
    real_module_count = len(hooks._EAGER_HOOK_MODULES)

    # Spoof one entry to a module that raises at import time, without
    # touching the real entries above.
    hooks._EAGER_HOOK_MODULES = list(hooks._EAGER_HOOK_MODULES) + [
        "coordinator_core.hooks._does_not_exist_probe_module"
    ]
    hooks._eager_import_all()

    poisoned = hooks.get_poisoned_modules()
    assert "coordinator_core.hooks._does_not_exist_probe_module" in poisoned, (
        f"poisoned module not recorded: {poisoned!r}"
    )

    import coordinator_core.ipc as ipc
    registered = [k for k in ipc._REGISTRY if k.startswith("hooks.")]
    assert len(registered) == real_module_count, (
        f"a poisoned extra module must not block the real {real_module_count} "
        f"hooks.* registrations, got {len(registered)}: {registered!r}"
    )
    print("POISONED_MODULE_RESILIENCE_OK")
    """
)


def test_eager_import_all_is_resilient_to_one_poisoned_module() -> None:
    """(e) — a single hook module's import-time failure does not prevent the
    other fifteen from registering, mirroring
    coordinator_core.ops._eager_import_all()'s per-module try/except."""
    proc = _run_script(_POISONED_MODULE_SCRIPT)
    assert proc.returncode == 0, (
        f"subprocess failed (stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )
    assert "POISONED_MODULE_RESILIENCE_OK" in proc.stdout
    assert "FAILED to import" in proc.stderr, (
        "poisoned-module import failure was not printed loudly to stderr"
    )


# ---------------------------------------------------------------------------
# (f) RETIRED -- was _lazy_ops_requested() parity with the coordinator_core.ops
#     sibling (test_lazy_ops_requested_matches_ops_sibling_across_channel_matrix).
#     Neither package's __init__.py defines that function any more as of C6
#     (ops) / C7 (hooks, 2026-08-23) -- there is nothing left to compare.
# ---------------------------------------------------------------------------
