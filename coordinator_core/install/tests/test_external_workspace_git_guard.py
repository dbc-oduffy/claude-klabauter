"""Tests for coordinator_core.install.external_workspace_git_guard: include files and global include line."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.external_workspace import vcs_guard_contract as contract
from coordinator_core.install import external_workspace_git_guard as guard

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git_global(config: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "config", "--file", str(config), *args],
        capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[user]\n\tname = t\n", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    present = tmp_path / "present_ws"
    present.mkdir()
    missing = tmp_path / "missing_ws"
    (registry / "registry.local.toml").write_text(
        "[external_workspaces.present]\n"
        f"path = '{present.as_posix()}'\n"
        "[external_workspaces.missing]\n"
        f"path = '{missing.as_posix()}'\n"
        "url = 'https://example.invalid/x.git'\n"
        "[external_workspaces.p4one]\n"
        "path = '/p4/root'\nvcs = 'p4'\n"
        "[external_workspaces.relative]\n"
        "path = 'rel/dir'\n",
        encoding="utf-8",
    )
    return registry, global_config, present, missing


def test_apply_writes_include_for_present_and_missing_and_one_global_line(env):
    registry, global_config, present, missing = env

    report = guard.apply()

    include = (registry / contract.INCLUDE_FILE_NAME).read_text(encoding="utf-8")
    assert f'gitdir:{contract.to_git_posix(present)}/' in include
    assert f'gitdir:{contract.to_git_posix(missing)}/' in include
    assert "/p4/root" not in include
    assert "rel/dir" not in include
    assert (registry / contract.HOOKS_CONFIG_FILE_NAME).read_text(encoding="utf-8") == contract.render_hooks_config()
    lines = _git_global(global_config, "--get-all", "include.path").split()
    assert lines == [contract.to_git_posix(registry / contract.INCLUDE_FILE_NAME)]
    assert any(line.startswith("guarded 2 ") for line in report)


def test_apply_is_idempotent(env):
    registry, global_config, _present, _missing = env
    guard.apply()
    before = global_config.read_bytes()
    stamp = (registry / contract.INCLUDE_FILE_NAME).stat().st_mtime_ns

    report = guard.apply()

    assert global_config.read_bytes() == before
    assert (registry / contract.INCLUDE_FILE_NAME).stat().st_mtime_ns == stamp
    assert any("already set" in line for line in report)


def test_apply_dry_run_writes_nothing(env):
    registry, global_config, _present, _missing = env
    before = global_config.read_bytes()

    report = guard.apply(dry_run=True)

    assert not (registry / contract.INCLUDE_FILE_NAME).exists()
    assert global_config.read_bytes() == before
    assert any(line.startswith("would") for line in report)


def test_machine_mutation_switch_leaves_global_config_alone_but_still_refuses_nothing_else(env, monkeypatch):
    _registry, global_config, _present, _missing = env
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    before = global_config.read_bytes()

    report = guard.apply()

    assert global_config.read_bytes() == before
    assert any("machine mutation disabled" in line for line in report)


def test_unresolvable_settings_home_is_a_typed_refusal(env, monkeypatch):
    registry, global_config, _present, _missing = env

    def boom():
        raise OSError("no home")

    monkeypatch.setattr("coordinator_core.machine_resolver.registry_dir", boom)
    before = global_config.read_bytes()

    with pytest.raises(guard.ExternalWorkspaceGuardRefusal, match="COORDINATOR_SETTINGS_HOME"):
        guard.apply()

    assert global_config.read_bytes() == before
    assert not (registry / contract.INCLUDE_FILE_NAME).exists()


def test_no_registered_workspace_writes_nothing(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    global_config = tmp_path / "gitconfig"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    report = guard.apply()

    assert not global_config.exists()
    assert not (registry / contract.INCLUDE_FILE_NAME).exists()
    assert "nothing to guard" in report[0]


def test_drive_letter_registered_path_is_kept(env):
    registry, _global_config, _present, _missing = env
    drive_shaped = "Z" + ":\\fixture\\one"
    with (registry / "registry.local.toml").open("a", encoding="utf-8") as fh:
        fh.write(f"[external_workspaces.drive]\npath = '{drive_shaped}'\n")

    guard.apply()

    assert f"gitdir:{drive_shaped.replace(chr(92), '/')}/" in (registry / contract.INCLUDE_FILE_NAME).read_text(encoding="utf-8")
