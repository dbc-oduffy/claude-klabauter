
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.install import first_run


def _fake_run_factory(recorder, publish_rc=0, git_ok=True, write_stamp_on_publish=True, stamp_path=None):

    def _fake_run(cmd, timeout=20, **kwargs):
        cmd = list(cmd)
        recorder.append(cmd)
        rc = 0
        if cmd[0] == "git":
            rc = 0 if git_ok else 1
        elif len(cmd) > 1 and str(cmd[1]).endswith("publish.py"):
            rc = publish_rc
            if publish_rc == 0 and write_stamp_on_publish and stamp_path is not None:
                stamp_path.parent.mkdir(parents=True, exist_ok=True)
                stamp_path.write_text("sha:deadbeef\n", encoding="utf-8")
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    return _fake_run


@pytest.fixture(autouse=True)
def _fake_registry_set(monkeypatch, request):
    writes = []
    request.node.registry_writes = writes
    monkeypatch.setattr(first_run, "registry_set", lambda key, value: writes.append((key, value)))


def test_provisions_without_any_machine_local_cli(monkeypatch, tmp_path, request):
    build_dest = tmp_path / "settings-home" / "engine-build" / "claude-klabauter"
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    (claude_klabauter_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")
    assert not (claude_klabauter_root / "coordinator" / "bin" / "machine-local").exists()

    stamp_path = build_dest / "coordinator_core" / "_engine_stamp"
    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder, stamp_path=stamp_path))

    assert first_run.provision_stamped_engine(claude_klabauter_root) is True
    assert (first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(build_dest)) in request.node.registry_writes
    assert not any("machine-local" in str(part) for argv in recorder for part in argv)


def test_already_stamped_is_idempotent_noop(monkeypatch, tmp_path, request):
    dest = tmp_path / "existing-klabauter"
    stamp = dest / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("sha:cafef00d\n", encoding="utf-8")

    monkeypatch.setattr(
        "coordinator_core.machine_resolver.registry_get",
        lambda key: str(dest) if key == first_run._KLABAUTER_MIRROR_REGISTRY_KEY else None,
    )

    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder))

    result = first_run.provision_stamped_engine(tmp_path)

    assert result is True
    assert recorder == []
    assert request.node.registry_writes == [(first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(dest))]


def test_fresh_box_git_inits_registers_and_runs_publish(monkeypatch, tmp_path, request):
    build_dest = tmp_path / "settings-home" / "engine-build" / "claude-klabauter"

    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    (claude_klabauter_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")

    stamp_path = build_dest / "coordinator_core" / "_engine_stamp"
    recorder = []
    monkeypatch.setattr(
        first_run,
        "_run",
        _fake_run_factory(recorder, publish_rc=0, stamp_path=stamp_path),
    )

    result = first_run.provision_stamped_engine(claude_klabauter_root)

    assert result is True
    assert build_dest.is_dir()
    assert stamp_path.is_file()

    argvs = recorder
    assert any(a[:2] == ["git", "init"] for a in argvs)
    assert any(a[:1] == ["git"] and "commit" in a for a in argvs)
    assert any(str(claude_klabauter_root / "coordinator" / "bin" / "publish.py") in a for a in argvs)
    assert request.node.registry_writes == [
        (first_run._KLABAUTER_MIRROR_PATH_REGISTRY_KEY, str(build_dest)),
        (first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(build_dest)),
    ]


def test_publish_round_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys, request):
    build_dest = tmp_path / "settings-home" / "engine-build" / "claude-klabauter"
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    (claude_klabauter_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")

    recorder = []
    monkeypatch.setattr(
        first_run,
        "_run",
        _fake_run_factory(recorder, publish_rc=1, stamp_path=build_dest / "coordinator_core" / "_engine_stamp"),
    )

    result = first_run.provision_stamped_engine(claude_klabauter_root)

    assert result is False
    err = capsys.readouterr().err
    assert "publish round into" in err
    assert "exited 1" in err
    assert first_run._KLABAUTER_MIRROR_REGISTRY_KEY not in [
        key for key, _ in request.node.registry_writes
    ]


def test_git_init_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )
    claude_klabauter_root = tmp_path / "claude-klabauter"

    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder, git_ok=False))

    result = first_run.provision_stamped_engine(claude_klabauter_root)

    assert result is False
    assert "git init" in capsys.readouterr().err


def test_registry_write_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    def _raising_set(key, value):
        raise OSError("registry.local.toml is read-only")

    monkeypatch.setattr(first_run, "registry_set", _raising_set)

    claude_klabauter_root = tmp_path / "claude-klabauter"
    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder))

    assert first_run.provision_stamped_engine(claude_klabauter_root) is False
    err = capsys.readouterr().err
    assert first_run._KLABAUTER_MIRROR_PATH_REGISTRY_KEY in err
    assert "read-only" in err
