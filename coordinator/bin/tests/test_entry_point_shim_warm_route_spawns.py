"""coordinator.bin.tests.test_entry_point_shim_warm_route_spawns -- the
import-closure legs that launch a real interpreter.

SPLIT OUT 2026-08-27. `_run_import_closure_probe` spawns, and a spawn site in
a non-test function forces the module-level tier form (spawn ratchet Rule 4 --
a marker on a helper is inert). Undivided, that form would have tiered the
eleven in-process route assertions off the fast tier to declare these two.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"

for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402
import cc_invoke  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Shared with the in-process half of this suite. Imported rather than
#: duplicated so the two files cannot drift apart.
from coordinator.bin.tests.test_entry_point_shim_warm_route import (  # noqa: E402
    _FORBIDDEN_IMPORT_SUBSTRINGS,
    _IMPORT_CLOSURE_PROBE,
)


def _run_import_closure_probe() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_CLOSURE_PROBE],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"import-closure probe failed: rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_excludes_heavy_modules():
    modules = _run_import_closure_probe()
    offenders = [
        m for m in modules if any(bad in m for bad in _FORBIDDEN_IMPORT_SUBSTRINGS)
    ]
    assert offenders == [], (
        f"importing coordinator_core.merge_assemble.cli pulled in {offenders!r} -- "
        "AC1's warm-path import-cost promise is broken (this is the runtime "
        "gate AC10's AST check on cli.py alone cannot see, since these modules "
        "load via merge_assemble/__init__.py's own module-scope imports, not "
        "cli.py's)"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_gate_can_actually_fail(tmp_path):
    """Proves the gate above is not vacuous: temporarily restores a heavy
    module-scope import into `merge_assemble/__init__.py`, re-runs the SAME
    probe against that mutated file (via a throwaway copy of the package
    directory so the real tree is never touched), and asserts the probe's
    own `sys.modules` snapshot now contains the offending module -- i.e.
    the assertion above would have gone red had the mutation landed in the
    real tree.

    Mutate-and-check happens against a COPY, never the real
    `merge_assemble/__init__.py` (out of this chunk's `writes:` scope) --
    no revert-of-real-file step is needed because nothing real was ever
    changed.
    """
    import shutil

    real_pkg_dir = _REPO_ROOT / "coordinator_core" / "merge_assemble"
    real_core_dir = _REPO_ROOT / "coordinator_core"

    mutant_root = tmp_path / "mutant_root"
    mutant_core = mutant_root / "coordinator_core"
    shutil.copytree(real_core_dir, mutant_core, ignore=shutil.ignore_patterns("__pycache__"))

    init_path = mutant_core / "merge_assemble" / "__init__.py"
    original_text = init_path.read_text(encoding="utf-8")
    anchor = "from __future__ import annotations\n"
    assert anchor in original_text, "expected __init__.py to open with a __future__ import"
    mutated_text = original_text.replace(
        anchor,
        anchor
        + "from coordinator_core.contract.decision_object.judgment import "
        "build_judgment_point  # mutation-test-only\n",
        1,
    )
    init_path.write_text(mutated_text, encoding="utf-8")

    probe = textwrap.dedent(
        """
        import json
        import sys
        import coordinator_core.merge_assemble.cli  # noqa: F401
        print(json.dumps(sorted(sys.modules.keys())))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(mutant_root),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"mutant probe failed to even run: {proc.stderr}"
    modules = json.loads(proc.stdout)
    offenders = [m for m in modules if any(bad in m for bad in _FORBIDDEN_IMPORT_SUBSTRINGS)]
    assert offenders, (
        "mutation did not reproduce a forbidden import -- the gate's fail "
        "path cannot be trusted until this reproduces red"
    )


# ---------------------------------------------------------------------------
# COORDINATOR_ENGINE_ROOT override pin.
#
# Spec backlink: state/lessons/2026-08-30-run-target-in-process-still-loads-
# the-published-engine.yaml -- `entry_point_shim._import_engine_module`
# resolves via `cc_invoke._resolve_claude_klabauter_root()`'s pointer/registry ladder,
# which has no self-location rung and so returns the PUBLISHED MIRROR by
# default. `COORDINATOR_ENGINE_ROOT` is the one rung that outranks it
# (engine_bootstrap.py `_resolve_engine_root` rung 1). This pins that
# override so a future change to the ladder's rung order breaks this test
# instead of the next session's worktree verification silently testing the
# mirror again.
#
# Negative-spec: does NOT assert anything about `resolve_engine_root
# (script_file)` (a DIFFERENT resolver, LOCATOR axis, self-location-first) --
# the lesson's whole point is that resolver is not proof of anything about
# `_import_engine_module`'s DISPATCH-axis ladder.
# ---------------------------------------------------------------------------

_ENGINE_ROOT_OVERRIDE_PROBE = textwrap.dedent(
    """
    import json
    import sys
    sys.path.insert(0, {lib_dir!r})
    import entry_point_shim
    mod = entry_point_shim._import_engine_module("coordinator_core.pickup_assemble")
    resolved_root = entry_point_shim._cc_invoke_resolve_claude_klabauter_root()()
    print(json.dumps({{"module_file": mod.__file__, "resolved_root": resolved_root}}))
    """
)


def _probe_engine_root_override(cwd: Path, env: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _ENGINE_ROOT_OVERRIDE_PROBE.format(lib_dir=str(_LIB_DIR))],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"engine-root override probe failed: rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_engine_module_honours_coordinator_engine_root(tmp_path):
    """`_import_engine_module` (and, via the same `_cc_invoke_resolve_claude_klabauter_root()`
    primitive, `_merge_assemble_dispatch`'s identical ladder call) must bind
    the engine named by `COORDINATOR_ENGINE_ROOT`, not whatever the pointer-
    file/registry ladder would otherwise answer.

    Run from a scratch `cwd` OUTSIDE this checkout (never the repo's own
    directory) so a self-location rung cannot accidentally supply the right
    answer for the wrong reason -- the override must be doing the work.
    """
    import os

    scratch_cwd = tmp_path / "outside_checkout"
    scratch_cwd.mkdir()

    env_with_override = dict(os.environ)
    env_with_override["COORDINATOR_ENGINE_ROOT"] = str(_REPO_ROOT)
    with_override = _probe_engine_root_override(scratch_cwd, env_with_override)

    assert with_override["resolved_root"] == str(_REPO_ROOT), (
        f"COORDINATOR_ENGINE_ROOT={_REPO_ROOT!s} did not win the ladder -- "
        f"resolved {with_override['resolved_root']!r} instead"
    )
    assert Path(with_override["module_file"]).resolve().is_relative_to(_REPO_ROOT.resolve()), (
        f"coordinator_core.pickup_assemble imported from "
        f"{with_override['module_file']!r}, not under {_REPO_ROOT!s} -- "
        "COORDINATOR_ENGINE_ROOT was set but not honoured"
    )

    env_without_override = dict(os.environ)
    env_without_override.pop("COORDINATOR_ENGINE_ROOT", None)
    without_override = _probe_engine_root_override(scratch_cwd, env_without_override)

    assert without_override["resolved_root"] != str(_REPO_ROOT), (
        "the no-override baseline also resolved to this checkout -- the test "
        "cannot tell an honoured override from a coincidence; rerun from a "
        "box where the pointer/registry ladder answers something else, or "
        "adjust the scratch cwd"
    )
