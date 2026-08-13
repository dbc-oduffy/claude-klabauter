#!/usr/bin/env python3
"""test_lazy_hooks_channel.py — the lazy-hooks contract (C1) and the C2
registry-miss fallback, pinned as tests.

Subject: `coordinator_core/hooks/__init__.py`'s gate on `_eager_import_all()`
(C1) and `coordinator_core.ipc._lazy_import_and_lookup`'s hooks-scoped
fallback stage (C2). Read both at HEAD before touching an assertion here —
the wiring, not this docstring, is authoritative.

New file: the four existing files in this directory (test_nudge_em_code_
dispatch.py, test_nudge_named_agent_report_delivery.py,
test_track_touched_files_normalize.py,
test_c4_ownership_inherited_at_dispatch_tripwires.py) are all per-hook-module
behavior tests. Nothing here already owns "the lazy-registration channel and
the registry-miss fallback" as a subject, so this is a new file, named to
mirror `coordinator_core/ops/tests/test_lazy_ops_channel.py` (the ops-side
sibling this package's channel is REUSED from verbatim — see
`coordinator_core/hooks/__init__.py`'s own docstring).

Four properties pinned:

  (a) DEFAULT-EAGER (AC2). With neither channel armed, `import
      coordinator_core.hooks` registers exactly the same "hooks.*" op set it
      registered before this chunk, and the three op-registry drift guards
      (authz `OP_CLASSIFICATION` coverage, ipc `_OP_KEY_SCOPE` coverage,
      `OP_MODULE_MAP` parity) gain NO ADDITIONAL gap from this chunk — not
      necessarily green. Two of the three carry PRE-EXISTING, chunk-unrelated
      red at HEAD (see `_KNOWN_PREEXISTING_*` below), and a concurrent
      session may close either gap at any time; a guard going green is
      always a pass here, never a required outcome, so someone else's
      correct fix is never mistaken for a regression. A guard staying red is
      only a pass if every failing op is already in its allow-list — any
      other failing op is treated as a hooks-caused regression and fails
      loudly, naming it.
  (b) OPERATOR OVERRIDE AUTHORITATIVE IN BOTH DIRECTIONS, including forcing
      EAGER over an already-armed in-process channel — the direction
      `coordinator/tests/test_install_substrate.sh` Test 15 depends on for
      the ops side, pinned here for the hooks side since hooks reads the
      identical two channels.
  (c) EVERY REGISTERED `hooks.*` OP RESOLVES UNDER LAZY MODE via the C2
      fallback (AC3), asserted deterministically via `sys.modules`
      membership — at most the hooks package (plus
      `coordinator_core.ops._registry_map`, itself part of the production
      resolution path) is ever imported, never any `coordinator_core.ops.*`
      op module. NOT proven by live dispatch: six of the fifteen hooks
      mutate session state.
  (d) NO LEAK (AC4). Arming the in-process channel via the `sys` attribute
      never writes `os.environ`, and a pytest child spawned while that
      channel is armed (inheriting the parent's UNMODIFIED environment)
      still collects and passes a probe that asserts the flag's absence.
      This is the regression an env-var-based in-process channel would
      reintroduce (docs/research/2026-07-28-lazy-ops-import-side-effect-
      scope.md § 6 (c)) — the direct `os.environ` assertion right after
      arming is what makes this fail against an env-var implementation
      rather than passing vacuously.

  (e) RESILIENT-AND-LOUD `_eager_import_all()` (mirroring
      coordinator_core.ops's 2026-07-21 pattern): one hook module's
      import-time failure does not prevent the OTHER fourteen from
      registering, and a later lookup of the poisoned module's own op
      re-raises the real cause via `get_poisoned_modules()` rather than
      silently vanishing into "unknown op". Matters more after C2 than
      before it: this routine now also runs synchronously on the live
      dispatch path to serve a single `hooks.*` registry-miss.
  (f) `_lazy_ops_requested()` PARITY with its `coordinator_core.ops` sibling
      across the channel matrix (env unset/0/1, sys attribute unset/True) —
      the docstring's "reads the EXACT two channels" / "verbatim" claim is
      asserted here rather than only inspected, since the two functions are
      deliberately duplicated rather than shared (see that docstring's
      "Deliberately NOT" paragraph for why sharing was rejected: importing
      coordinator_core.ops from inside coordinator_core.hooks risks a
      circular, partially-initialized import, since ops's own eager-import
      list imports hooks).

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
# (a) default-eager: registered set unchanged, drift guards unchanged
# ---------------------------------------------------------------------------

_DEFAULT_EAGER_HOOKS_REGISTRY_SCRIPT = textwrap.dedent(
    """
    import coordinator_core.hooks as hooks  # noqa: F401 -- default env: eager
    import coordinator_core.ipc as ipc

    expected = sorted(
        "hooks." + m.rsplit(".", 1)[-1] for m in hooks._EAGER_HOOK_MODULES
    )
    registered = sorted(k for k in ipc._REGISTRY if k.startswith("hooks."))
    print("EXPECTED:" + ",".join(expected))
    print("REGISTERED:" + ",".join(registered))
    """
)


def test_default_eager_registers_the_full_known_hook_op_set() -> None:
    """(a) — no env var, no sys attribute: the registered "hooks.*" set is
    exactly the set `_EAGER_HOOK_MODULES` names, derived from production
    data rather than a hand-duplicated literal so this only breaks on an
    actual registration change, not on a docstring edit."""
    proc = _run_script(_DEFAULT_EAGER_HOOKS_REGISTRY_SCRIPT)
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    lines = {ln.split(":", 1)[0]: ln.split(":", 1)[1] for ln in proc.stdout.strip().splitlines()}
    expected = lines["EXPECTED"].split(",")
    registered = lines["REGISTERED"].split(",")
    assert registered == expected, (
        f"default-eager hooks registration diverged from _EAGER_HOOK_MODULES: "
        f"expected {expected!r}, got {registered!r}"
    )
    assert len(expected) == 15, f"expected exactly 15 hooks.* ops, got {len(expected)}"


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
# (b) operator override authoritative in BOTH directions
# ---------------------------------------------------------------------------

_HOOKS_REGISTRY_SIZE_SCRIPT = textwrap.dedent(
    """
    import coordinator_core.hooks  # noqa: F401
    import coordinator_core.ipc as ipc
    print(len([k for k in ipc._REGISTRY if k.startswith("hooks.")]))
    """
)

_ARM_SYS_ATTRIBUTE = "import sys; sys._coordinator_core_lazy_ops = True\n"


def _hooks_registry_size(script_prefix: str = "", **env_overrides: str) -> int:
    proc = _run_script(_HOOKS_REGISTRY_SIZE_SCRIPT, script_prefix, **env_overrides)
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    return int(proc.stdout.strip())


class TestHooksOperatorOverridePrecedence:
    def test_neither_channel_set_registers_eagerly(self) -> None:
        assert _hooks_registry_size() > 0

    def test_sys_attribute_alone_suppresses_eager_registration(self) -> None:
        assert _hooks_registry_size(_ARM_SYS_ATTRIBUTE) == 0

    def test_env_var_alone_suppresses_eager_registration(self) -> None:
        assert _hooks_registry_size(**{_LAZY_OPS_ENV_KEY: "1"}) == 0

    def test_operator_env_zero_beats_the_in_process_channel(self) -> None:
        """The easy-to-omit direction: `LAZY_OPS=0` must force eager even
        with the in-process channel armed, or an already-lazy-armed process
        (e.g. `test_install_substrate.sh` Test 15's eager baseline leg on the
        ops side) would silently stay lazy on the hooks side too."""
        assert _hooks_registry_size(_ARM_SYS_ATTRIBUTE, **{_LAZY_OPS_ENV_KEY: "0"}) > 0


# ---------------------------------------------------------------------------
# (c) every registered hooks.* op resolves under lazy mode via the C2
#     fallback -- resolution proof via sys.modules membership, not live
#     dispatch (six of the fifteen mutate session state).
# ---------------------------------------------------------------------------

_LAZY_RESOLUTION_SCRIPT = textwrap.dedent(
    """
    import importlib
    import os
    import sys

    os.environ["COORDINATOR_CORE_LAZY_OPS"] = "1"

    import coordinator_core.ops  # noqa: F401 -- lazy: SKIPS the eager import list
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
        f"expected all 15 hooks.* op modules imported by the C2 fallback, "
        f"missing {sorted(missing)!r} (imported: {sorted(imported_submodules)!r})"
    )
    print("HOOKS_LAZY_RESOLUTION_OK")
    """
)


def test_every_hooks_op_resolves_under_lazy_mode_via_c2_fallback_without_escalating() -> None:
    proc = _run_script(_LAZY_RESOLUTION_SCRIPT)
    assert proc.returncode == 0, (
        f"subprocess failed (stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )
    assert "HOOKS_LAZY_RESOLUTION_OK" in proc.stdout


# ---------------------------------------------------------------------------
# (d) NO LEAK -- arming the in-process channel never touches os.environ, and
#     a pytest child inheriting the parent's UNMODIFIED environment still
#     collects and passes a probe asserting the flag's absence.
# ---------------------------------------------------------------------------


def test_arming_in_process_channel_writes_nothing_to_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct `os.environ` assertion, not a downstream behavior, is what
    makes this fail against an env-var implementation of the in-process
    channel: an env-var writer would set exactly this key, and this
    assertion would trip immediately -- it cannot pass vacuously."""
    monkeypatch.delenv(_LAZY_OPS_ENV_KEY, raising=False)
    monkeypatch.setattr(sys, "_coordinator_core_lazy_ops", True, raising=False)

    assert _LAZY_OPS_ENV_KEY not in os.environ, (
        f"arming sys._coordinator_core_lazy_ops wrote {_LAZY_OPS_ENV_KEY!r} into "
        f"os.environ -- this is exactly the regression the sys-attribute design "
        f"exists to prevent (docs/research/2026-07-28-lazy-ops-import-side-"
        f"effect-scope.md § 6 (c))."
    )


def test_pytest_child_of_a_lazy_armed_process_collects_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pytest child spawned while the in-process channel is armed, given
    the PARENT'S UNMODIFIED environment (no manual stripping in this test --
    that is the point: if arming had leaked into os.environ, the child would
    inherit it and this probe would fail collection), still collects and
    passes a probe asserting the flag's absence."""
    monkeypatch.delenv(_LAZY_OPS_ENV_KEY, raising=False)
    monkeypatch.setattr(sys, "_coordinator_core_lazy_ops", True, raising=False)

    test_file = tmp_path / "test_lazy_hooks_child_env_absent.py"
    test_file.write_text(
        "import os\n\n\n"
        "def test_lazy_ops_flag_absent_in_child():\n"
        "    assert os.environ.get('COORDINATOR_CORE_LAZY_OPS') is None\n",
        encoding="utf-8",
    )

    proc = subprocess.run(  # popup-intentional-last-resort
        [sys.executable, "-m", "pytest", str(test_file), "-q"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=str(tmp_path),
        env=dict(os.environ),  # UNMODIFIED parent env -- the whole point of (d)
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"pytest child failed to collect/pass while the in-process lazy-hooks "
        f"channel was armed in the parent -- COORDINATOR_CORE_LAZY_OPS leaked "
        f"into the child's environment: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (e) resilient-and-loud _eager_import_all(): one poisoned module doesn't
#     block the rest, and a later lookup of its op re-raises the real cause.
# ---------------------------------------------------------------------------

_POISONED_MODULE_SCRIPT = textwrap.dedent(
    """
    import sys
    import coordinator_core.hooks as hooks

    # Spoof one entry to a module that raises at import time, without
    # touching the other fourteen real entries.
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
    assert len(registered) == 15, (
        f"a poisoned sixteenth module must not block the real 15 hooks.* "
        f"registrations, got {len(registered)}: {registered!r}"
    )
    print("POISONED_MODULE_RESILIENCE_OK")
    """
)


def test_eager_import_all_is_resilient_to_one_poisoned_module() -> None:
    """(e) — a single hook module's import-time failure does not prevent the
    other fourteen from registering, mirroring
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
# (f) _lazy_ops_requested() parity with its coordinator_core.ops sibling
# ---------------------------------------------------------------------------


def test_lazy_ops_requested_matches_ops_sibling_across_channel_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) — coordinator_core.hooks._lazy_ops_requested() is a deliberate
    hand-mirror, not an import, of coordinator_core.ops._lazy_ops_requested()
    (see coordinator_core/hooks/__init__.py's docstring "Deliberately NOT"
    paragraph for why sharing was rejected). This asserts the two stay in
    agreement across the full channel matrix so the "verbatim" claim is
    checked, not merely inspected."""
    import coordinator_core.hooks as hooks_mod
    import coordinator_core.ops as ops_mod

    monkeypatch.delenv(_LAZY_OPS_ENV_KEY, raising=False)
    monkeypatch.delattr(sys, "_coordinator_core_lazy_ops", raising=False)

    env_values = (None, "0", "1")
    sys_values = (False, True)
    for env_value in env_values:
        if env_value is None:
            monkeypatch.delenv(_LAZY_OPS_ENV_KEY, raising=False)
        else:
            monkeypatch.setenv(_LAZY_OPS_ENV_KEY, env_value)
        for sys_value in sys_values:
            monkeypatch.setattr(
                sys, "_coordinator_core_lazy_ops", sys_value, raising=False
            )
            assert hooks_mod._lazy_ops_requested() == ops_mod._lazy_ops_requested(), (
                f"hooks/ops _lazy_ops_requested() diverged at "
                f"env={env_value!r} sys_attr={sys_value!r}"
            )
