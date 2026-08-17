"""coordinator_core.install.tests.test_fleet_env_healthy_minor_check —
`_fleet_env_healthy` must treat a Python-minor mismatch against
`LOCK_PYTHON_MINOR` as unhealthy, not just missing-executable/failed-import.

Purpose: C6 (`a8724790302f`) flipped `LOCK_PYTHON_MINOR` from 3.12 to 3.14
and regenerated the lock, but `_fleet_env_healthy` never read
`LOCK_PYTHON_MINOR` at all — it checked only executability and whether every
`_FLEET_ENV_IMPORT_PROBES` module imported. A pre-existing 3.12 environment
imports its own contracted modules just fine, so the flip never propagated:
`ensure_fleet_env(check_only=True)` kept reporting `"ready"` on every box
that had not been manually forced to rebuild. This module proves the fix
(the minor check folded into the same target-interpreter subprocess probe
the import check already runs) actually gates on the interpreter's real
`sys.version_info`, not on a directory-name inference, which would just be
a second instance of the same "it runs" oracle this fix replaces.

Spec backlink: docs/plans/2026-08-17-machine-first-install-surface.md § C6
    follow-up (fleet-env minor health check)

Negative-spec:
    - Does NOT build or touch the real fleet environment — every case here
      runs against `sys.executable` (this test process's own interpreter),
      with `LOCK_PYTHON_MINOR` monkeypatched to control the pass/fail
      outcome, never a real `uv sync`.
    - Does NOT re-test the pre-existing executable/import-failure paths —
      those are `_fleet_env_healthy`'s original contract and are not what
      this fix touches; only the minor-mismatch path is new here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env


def _current_minor_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _mismatched_minor_string() -> str:
    # Guaranteed not to equal the running interpreter's own minor.
    return f"{sys.version_info.major}.{sys.version_info.minor + 1}"


def test_stale_minor_reports_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `python_bin` whose real interpreter minor does not match
    `LOCK_PYTHON_MINOR` must fail the health probe — this is the exact
    defect: a healthy-but-stale-minor environment must rebuild, not be
    honoured forever."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _mismatched_minor_string())
    # Isolate the minor check from package availability in the test venv --
    # the running interpreter almost certainly lacks torch/chromadb/etc.
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())

    assert fleet_env._fleet_env_healthy(Path(sys.executable)) is False


def test_matching_minor_with_no_probes_reports_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control case: when `LOCK_PYTHON_MINOR` matches the real interpreter
    and there are no import probes to fail, the probe must pass — proves
    the minor check itself is not spuriously always-False."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())

    assert fleet_env._fleet_env_healthy(Path(sys.executable)) is True


def test_missing_executable_still_reports_unhealthy_before_minor_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing contract, unchanged by this fix: a nonexistent/non-
    executable `python_bin` fails fast on the `is_executable` gate,
    never reaching the minor or import probes (this is what keeps a
    fresh/rebuilding environment's missing exec routine, not an error)."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())

    assert fleet_env._fleet_env_healthy(Path("/nonexistent/does-not-exist/python")) is False


def test_check_only_reports_would_rebuild_on_stale_minor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ensure_fleet_env(check_only=True)` must surface the mismatch as
    `"would-rebuild"`, not `"ready"` — this is the path an operator or
    doctor probe actually consults, and is the observed symptom (C6's
    handoff: `check_only=True` kept reporting `"ready"` on a stale-minor
    box)."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _mismatched_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())
    monkeypatch.setattr(fleet_env, "_is_windows_shell", lambda: False)

    env_root = tmp_path / "fleet-env"
    (env_root / "bin").mkdir(parents=True)
    real_bin = Path(sys.executable)
    (env_root / "bin" / "python").symlink_to(real_bin)

    monkeypatch.setattr(fleet_env, "resolve_environment_root", lambda **_: env_root)

    status = fleet_env.ensure_fleet_env(
        check_only=True, settings_home_factory=lambda: tmp_path / "settings-home"
    )
    assert status == "would-rebuild"
