"""coordinator_core.install.tests.test_fleet_env_healthy_release_level_check —
`_fleet_env_healthy` must treat a PRE-RELEASE target interpreter as unhealthy,
even when its minor matches `LOCK_PYTHON_MINOR` exactly.

Purpose: the contracted minor is a version, and a version is satisfied by a
release candidate. On a container whose `uv` download catalog had gone stale,
`uv sync --python 3.14` selected `3.14.0rc2` — the only 3.14 that catalog
carried — and the environment then failed the locked dependency set on a
signature that changed between the candidate and the release (`pydantic`
could not construct a model, because `typing._eval_type` was still carrying
a pre-release keyword name). The minor check passed throughout: `3.14 ==
3.14`. Nothing in the lock pins against a candidate, so accepting one
provisions an interpreter no dependency was ever resolved against, and the
failure surfaces as an unrelated-looking import error deep in the stack
rather than as the provisioning fault it is.

Negative-spec:
    - Does NOT build or touch the real fleet environment, and never runs
      `uv`. Every case drives `_fleet_env_healthy` against a shim
      interpreter that execs the probe under a doctored `sys.version_info`.
    - Does NOT assert on the probe's source text. The gate is proven by the
      health verdict a pre-release interpreter actually receives, not by
      grepping the string that produces it.
    - Does NOT re-test the minor check — `LOCK_PYTHON_MINOR` is pinned to
      the shim's own reported minor in every arm here precisely so a
      mismatch cannot be what fails.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the shim interpreter is a shebang script; the equivalent Windows "
    "shim would prove nothing this module does not already prove on POSIX",
)

#: The shim reports this minor, and `LOCK_PYTHON_MINOR` is pinned to it, so
#: the minor check is satisfied in every arm and only the release level varies.
_SHIM_MINOR = "3.14"


def _shim_interpreter(tmp_path: Path, release_level: str, serial: int) -> Path:
    """An executable that runs `-c <code>` under a doctored `sys.version_info`.

    `_fleet_env_healthy` probes the TARGET interpreter in a subprocess, which
    is the whole point of it (a different environment's site-packages is not
    importable from this process). So the release level cannot be
    monkeypatched into the probe — it has to be what the probed executable
    genuinely reports about itself, which is what this shim supplies.
    """
    shim = tmp_path / f"python-{release_level}"
    shim.write_text(
        "#!" + sys.executable + "\n"
        "import collections, sys\n"
        "_V = collections.namedtuple(\n"
        "    '_V', 'major minor micro releaselevel serial'\n"
        ")\n"
        f"sys.version_info = _V(3, 14, 0, {release_level!r}, {serial!r})\n"
        "exec(sys.argv[2], {'__name__': '__main__'})\n",
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(0o755)
    return shim


@pytest.fixture(autouse=True)
def _pin_minor_and_drop_import_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _SHIM_MINOR)
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())


def test_a_release_candidate_interpreter_is_unhealthy(tmp_path: Path) -> None:
    """The defect: minor matches, so every prior gate passes, and the
    environment was accepted."""
    shim = _shim_interpreter(tmp_path, "candidate", 2)

    assert fleet_env._fleet_env_healthy(shim) is False


def test_a_released_interpreter_of_the_same_minor_is_healthy(tmp_path: Path) -> None:
    """Control: proves the new gate keys on the release level and is not
    simply always-False for the contracted minor."""
    shim = _shim_interpreter(tmp_path, "final", 0)

    assert fleet_env._fleet_env_healthy(shim) is True


@pytest.mark.parametrize("release_level", ["alpha", "beta", "candidate"])
def test_every_pre_release_level_is_rejected_not_just_candidate(
    tmp_path: Path, release_level: str
) -> None:
    """`final` is the only acceptable value. Enumerating the rejects keeps a
    future `releaselevel` from being waved through by an allow-list that
    only ever named `candidate`."""
    shim = _shim_interpreter(tmp_path, release_level, 1)

    assert fleet_env._fleet_env_healthy(shim) is False


def test_the_failure_names_the_provisioner_as_the_thing_to_fix(tmp_path: Path) -> None:
    """The evidence a caller keeps. `ensure_fleet_env` rmtree's the build
    tree the moment the probe returns False, so this diagnostic is the only
    record of why — and the actionable fault is the stale provisioner
    catalog, not the contracted minor. A message that named only the
    interpreter would invite retreating the pin instead."""
    shim = _shim_interpreter(tmp_path, "candidate", 2)
    diagnostic: dict = {}

    assert fleet_env._fleet_env_healthy(shim, diagnostic=diagnostic) is False

    detail = fleet_env._probe_failure_detail(diagnostic)
    assert "pre-release interpreter" in detail
    assert "candidate2" in detail
    assert "uv self update" in detail
