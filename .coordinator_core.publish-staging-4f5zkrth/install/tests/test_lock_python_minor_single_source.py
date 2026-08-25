"""``fleet_env_lock.LOCK_PYTHON_MINOR`` has five consumption sites (this
module's ``requires-python`` emission and its ``uv lock --python`` argv,
``fleet_env.py``'s import of the constant, its ``uv sync --python`` argv, and
its derivation of ``lib/python{minor}/site-packages`` as a real filesystem
path) and, until this test, no test pinned the constant's value or proved
any consumption site actually reads it rather than carrying an independent
literal. C6 (docs/plans/2026-08-17-machine-first-install-surface.md) found
this the hard way: flipping the pin from 3.12 to 3.14 required a manual grep
across two files, and nothing would have caught a missed site.

This is the artifact that discharges that silence — not a guarantee no site
can ever drift again (a hand-written literal a future edit adds elsewhere
would not be caught), but every site enumerated above is exercised here
against the live constant, so an edit to only some of them fails loud instead
of resolving green.

Spec backlink: docs/plans/2026-08-17-machine-first-install-surface.md § C6
Contract: docs/reference/fleet-shared-environment-contract.md § The Python minor
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from coordinator_core.install import fleet_env, fleet_env_lock


def test_lock_python_minor_is_pinned_to_the_ratified_value():
    """Pins the current PM-ruled value (2026-08-17: flipped 3.12 -> 3.14) so
    a future edit to this constant is a deliberate, reviewed change to this
    test too, not a silent drift."""
    assert fleet_env_lock.LOCK_PYTHON_MINOR == "3.14"


def test_fleet_env_imports_the_same_constant_object_not_a_copy():
    """``fleet_env.py`` consumption site 1/5: the module docstring states it
    imports ``LOCK_PYTHON_MINOR`` from ``fleet_env_lock`` rather than
    re-declaring it — this proves that import binding actually holds, so a
    future edit changing one file's copy without the other's re-export
    cannot silently create two independent pins."""
    assert fleet_env.LOCK_PYTHON_MINOR is fleet_env_lock.LOCK_PYTHON_MINOR


def test_render_lock_pyproject_requires_python_uses_the_constant():
    """Consumption site 2/5: ``fleet_env_lock.render_lock_pyproject``'s
    ``requires-python`` emission."""
    text = fleet_env_lock.render_lock_pyproject(specs=["foo>=1"], override_specs=[])
    assert f'requires-python = ">={fleet_env_lock.LOCK_PYTHON_MINOR}"' in text


def test_generate_lock_python_argv_uses_the_constant(tmp_path, monkeypatch):
    """Consumption site 3/5: ``fleet_env_lock.generate_lock``'s
    ``uv lock --python`` argv. Stubs ``subprocess.run`` to capture the argv
    without spawning a real ``uv`` process, and fabricates the ``uv.lock``
    ``generate_lock`` reads back so no real resolution is needed."""
    req_in = tmp_path / "fleet-env-requirements.in"
    req_in.write_text("foo>=1  # some_repo:pyproject.toml\n", encoding="utf-8")
    overrides = tmp_path / "fleet-env-overrides.toml"
    overrides.write_text("", encoding="utf-8")
    lock_out = tmp_path / "fleet-env.lock"

    captured: List[List[str]] = []

    class _FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        captured.append(argv)
        # generate_lock reads uv.lock back from the temp project dir --
        # fabricate it so the function completes without a real `uv` spawn.
        project_dir = Path(kwargs["cwd"])
        (project_dir / "uv.lock").write_text(
            '[[package]]\nname = "foo"\nversion = "1.0.0"\n', encoding="utf-8"
        )
        return _FakeResult()

    monkeypatch.setattr(fleet_env_lock.subprocess, "run", fake_run)
    fleet_env_lock.generate_lock(
        requirements_in_path=req_in, overrides_path=overrides, lock_path=lock_out
    )

    assert len(captured) == 1
    argv = captured[0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == fleet_env_lock.LOCK_PYTHON_MINOR


def test_provision_uv_environment_python_argv_uses_the_constant(tmp_path, monkeypatch):
    """Consumption site 4/5: ``fleet_env._provision_uv_environment``'s
    ``uv sync --python`` argv. Same capture-argv-without-spawning approach as
    the ``generate_lock`` test above."""
    fake_lock_path = tmp_path / "fleet-env.lock"
    fake_lock_path.write_text(
        'version = 1\nrequires-python = ">=3.14"\n', encoding="utf-8"
    )
    monkeypatch.setattr(fleet_env, "_LOCK_PATH", fake_lock_path)
    monkeypatch.setattr(
        fleet_env, "load_requirements_in_specs", lambda: ["foo>=1"]
    )
    monkeypatch.setattr(fleet_env, "load_override_dependency_specs", lambda: [])

    captured: List[List[str]] = []

    class _FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return _FakeResult()

    monkeypatch.setattr(fleet_env.subprocess, "run", fake_run)
    fleet_env._provision_uv_environment(tmp_path / "build-dir")

    assert len(captured) == 1
    argv = captured[0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == fleet_env_lock.LOCK_PYTHON_MINOR


def test_site_packages_dir_derives_from_the_constant(monkeypatch):
    """Consumption site 5/5: ``fleet_env._site_packages_dir``'s
    ``lib/python{minor}/site-packages`` derivation — a real filesystem path,
    not merely a rendered string, which is exactly why C6's dispatch named
    this the highest-stakes of the five sites."""
    monkeypatch.setattr(fleet_env, "_is_windows_shell", lambda: False)
    result = fleet_env._site_packages_dir(Path("/fake/env/root"))
    assert result == Path(
        f"/fake/env/root/lib/python{fleet_env_lock.LOCK_PYTHON_MINOR}/site-packages"
    )
