"""coordinator_core.install.tests.test_fleet_env_probe_diagnostic —
`_fleet_env_healthy`'s opt-in `diagnostic` dict, and the pre-swap
`FleetEnvError` in `ensure_fleet_env` that renders it.

Purpose: the pre-swap caller rmtree's the build tree the moment the health
probe returns False, so before this the failure printed one canned line and
destroyed the only evidence of why (observed on the 2026-09-05 Linux cloud
dogfood, where the hidden cause turned out to be the 3.14 lock not building —
docs/reference/linux-cloud-dogfood-friction.md). This proves the probe's
output is captured on every False path and reaches the raised error.

Negative-spec:
    - Does NOT build a real fleet environment or spawn `uv` —
      `_provision_uv_environment` is stubbed to plant a `python` pointing at
      `sys.executable`, and the import probes are monkeypatched to one module
      that cannot exist.
    - Does NOT re-test the minor-mismatch gate; that is
      `test_fleet_env_healthy_minor_check.py`'s surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env

_MISSING_MODULE = "coordinator_fleet_env_probe_diagnostic_absent_module"


def _current_minor_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def test_failed_import_populates_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", (_MISSING_MODULE,))

    diagnostic: dict = {}
    assert fleet_env._fleet_env_healthy(Path(sys.executable), diagnostic=diagnostic) is False

    assert diagnostic["returncode"] != 0
    assert _MISSING_MODULE in diagnostic["stderr"]
    assert "ModuleNotFoundError" in diagnostic["stderr"]


def test_missing_executable_populates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    missing = Path("/nonexistent/does-not-exist/python")

    diagnostic: dict = {}
    assert fleet_env._fleet_env_healthy(missing, diagnostic=diagnostic) is False

    assert "not executable" in diagnostic["error"]
    assert "returncode" not in diagnostic


def test_healthy_probe_without_diagnostic_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())

    assert fleet_env._fleet_env_healthy(Path(sys.executable)) is True


def test_pre_swap_failure_names_the_probe_cause_and_discards_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", (_MISSING_MODULE,))
    env_root = tmp_path / "fleet-env"
    monkeypatch.setattr(fleet_env, "resolve_environment_root", lambda **_: env_root)

    built: list[Path] = []

    def _plant_interpreter(build_dir: Path, **_kwargs) -> None:
        python_bin = fleet_env._env_python_path(build_dir)
        python_bin.parent.mkdir(parents=True)
        python_bin.symlink_to(Path(sys.executable))
        built.append(build_dir)

    monkeypatch.setattr(fleet_env, "_provision_uv_environment", _plant_interpreter)

    with pytest.raises(fleet_env.FleetEnvError) as excinfo:
        fleet_env.ensure_fleet_env(settings_home_factory=lambda: tmp_path / "settings-home")

    message = str(excinfo.value)
    assert "failed the health probe" in message
    assert "probe exit: " in message
    assert "--- probe stderr ---" in message
    assert _MISSING_MODULE in message
    assert built and not built[0].exists()
