"""F42 exit-criterion falsifier: a sandbox-rooted setup.py install leaves machine-global state alone.

Drives `setup.install_host_sampler_task` and `setup.offer_warm_opt_in` (agent mode, default
args) with COORDINATOR_SETTINGS_HOME / CLAUDE_HOME / CLAUDE_CONFIG_DIR under tmp_path and
`Path.home()` pinned to a separate tmp "real home" holding a pre-existing sampler job. The
control case (no redirect) proves the drive does write and spawn, so the redirected
assertions cannot pass vacuously.

NEGATIVE-SPEC: starts no real server and touches no real home; the spawn seam is a recorder.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SETUP_PY = _REPO_ROOT / "scripts" / "setup.py"
_SEED = b"pre-existing sampler job bytes\n"
_ROOT_VARS = ("COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR")


def _load_setup():
    spec = importlib.util.spec_from_file_location("_claude_klabauter_setup_f42_falsifier", _SETUP_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def setup_mod():
    return _load_setup()


@pytest.fixture(params=["darwin", "linux"])
def box(request, monkeypatch, tmp_path, setup_mod):
    """Real home with a seeded job, the spawn recorder, and a clean redirect env."""
    from coordinator_core.install import _shared, engine_root_for_install, host_sampler_scheduler
    from coordinator_core.ops.ceremony import detached_spawn

    platform = request.param
    real_home = tmp_path / "home"
    if platform == "darwin":
        job = real_home / "Library" / "LaunchAgents" / "com.coordinator.hostsampler.plist"
    else:
        job = real_home / ".config" / "systemd" / "user" / "coordinator-host-sampler.timer"
    job.parent.mkdir(parents=True)
    job.write_bytes(_SEED)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(host_sampler_scheduler, "_IS_WINDOWS", False)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    for var in _ROOT_VARS:
        monkeypatch.delenv(var, raising=False)

    engine_root = tmp_path / "engine-root"
    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: SimpleNamespace(kind="published", root=engine_root, remediation=None),
    )
    spawned: list = []

    def fake_spawn(repo_root, script_path, args=None):
        spawned.append((repo_root, script_path))
        return True

    monkeypatch.setattr(detached_spawn, "spawn_detached", fake_spawn)

    served = json.dumps({"served": True, "coordinator_core_file": str(engine_root / "coordinator_core" / "__init__.py")})
    monkeypatch.setattr(
        setup_mod.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=served + "\n", stderr=""),
    )
    monkeypatch.setattr(setup_mod, "_resolve_coordinator_claude_root", lambda r, a: (tmp_path, None))
    monkeypatch.setattr(setup_mod, "_resolve_plugin_root_for_machine_local", lambda c: None)
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda p: ["machine-local"])
    return SimpleNamespace(platform=platform, job=job, spawned=spawned, tmp=tmp_path, engine_root=engine_root)


def _sandbox(monkeypatch, tmp_path):
    for var in _ROOT_VARS:
        monkeypatch.setenv(var, str(tmp_path / "sandbox" / var.lower()))


def _drive(setup_mod, tmp_path, repo_root=None):
    repo_root = repo_root or tmp_path / "sandbox-clone"
    repo_root.mkdir(exist_ok=True)
    setup_mod.install_host_sampler_task(repo_root, repo_root)
    setup_mod.offer_warm_opt_in(repo_root, SimpleNamespace(agent_mode=True))


def test_sandboxed_install_leaves_job_and_warm_engine_untouched(box, monkeypatch, capsys, setup_mod, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    _drive(setup_mod, tmp_path)

    out = capsys.readouterr().out
    assert box.job.read_bytes() == _SEED
    assert box.spawned == []
    assert "[ADVISORY] host-sampler job not registered: COORDINATOR_SETTINGS_HOME=" in out
    assert "[ADVISORY] warm engine not started: COORDINATOR_SETTINGS_HOME=" in out


def test_control_registered_clone_writes_job_and_spawns(box, capsys, setup_mod, tmp_path):
    _drive(setup_mod, tmp_path, repo_root=box.engine_root)

    out = capsys.readouterr().out
    assert box.job.read_bytes() != _SEED
    assert len(box.spawned) == 1
    assert "not registered" not in out
    assert "not started" not in out


def test_second_clone_on_real_home_leaves_job_and_warm_engine_untouched(box, capsys, setup_mod, tmp_path):
    _drive(setup_mod, tmp_path)

    out = capsys.readouterr().out
    assert box.job.read_bytes() == _SEED
    assert box.spawned == []
    for line in ("host-sampler job not registered", "warm engine not started"):
        advisory = next(ln for ln in out.splitlines() if line in ln)
        assert advisory.startswith("[ADVISORY]")
        assert str(tmp_path / "sandbox-clone") in advisory and str(box.engine_root) in advisory


def test_unresolvable_registry_leaves_job_and_warm_engine_untouched(box, monkeypatch, capsys, setup_mod, tmp_path):
    from coordinator_core.install import engine_root_for_install

    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: SimpleNamespace(kind="none", root=None, remediation="x"),
    )
    _drive(setup_mod, tmp_path, repo_root=box.engine_root)

    out = capsys.readouterr().out
    assert box.job.read_bytes() == _SEED
    assert box.spawned == []
    assert "[ADVISORY] host-sampler job not registered: no registered engine root" in out


def test_main_registers_clones_before_the_machine_global_legs(setup_mod):
    import inspect

    body = inspect.getsource(setup_mod.main)
    discovery = body.index("install_repo_discovery(")
    assert discovery < body.index("offer_warm_opt_in(")
    assert discovery < body.index("install_host_sampler_task(")
