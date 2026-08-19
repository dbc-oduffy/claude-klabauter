"""Tests for `coordinator_core.install.first_run.provision_stamped_engine`
(docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md chunk C1).

Purpose: pin that a fresh box reaches a registered, STAMPED engine root
without a human running publish — the prerequisite that keeps C4's
fail-closed change from bricking a new install. Every subprocess boundary
(`git`, `machine-local`, `coordinator/bin/publish.py`) is mocked: this suite
exercises the provisioning LOGIC (destination resolution, idempotence,
registry writes, failure handling), never a real publish round.

Negative spec: does not exercise `scripts/setup.py`'s call site directly —
that is a thin, one-branch call into this same function (see
`register_claude_klabauter_root`'s C1 addition), and `scripts/test_setup.py` already
covers the identity/registration branching this hooks into.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.install import first_run


def _fake_run_factory(recorder, publish_rc=0, git_ok=True, ml_set_ok=True, write_stamp_on_publish=True, stamp_path=None):
    """Build a stand-in for `first_run._run` that records every argv and
    fakes each subprocess boundary this function crosses."""

    def _fake_run(cmd, timeout=20, **kwargs):
        cmd = list(cmd)
        recorder.append(cmd)
        rc = 0
        if cmd[0] == "git":
            rc = 0 if git_ok else 1
        elif cmd[0] == "machine-local":
            rc = 0 if ml_set_ok else 1
        elif len(cmd) > 1 and str(cmd[1]).endswith("publish.py"):
            rc = publish_rc
            if publish_rc == 0 and write_stamp_on_publish and stamp_path is not None:
                stamp_path.parent.mkdir(parents=True, exist_ok=True)
                stamp_path.write_text("sha:deadbeef\n", encoding="utf-8")
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    return _fake_run


@pytest.fixture(autouse=True)
def _fake_machine_local(monkeypatch, tmp_path):
    """Every test gets a resolvable machine-local CLI (fake argv) unless a
    test explicitly overrides `_resolve_machine_local_argv`."""
    monkeypatch.setattr(first_run, "_resolve_machine_local_argv", lambda claude_klabauter_root: ["machine-local"])


def test_no_machine_local_warns_and_returns_false(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(first_run, "_resolve_machine_local_argv", lambda claude_klabauter_root: None)
    result = first_run.provision_stamped_engine(tmp_path)
    assert result is False
    assert "machine-local CLI not found" in capsys.readouterr().err


def test_already_stamped_is_idempotent_noop(monkeypatch, tmp_path):
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
    # Only the idempotent re-registration write happens — no git init, no publish.
    assert len(recorder) == 1
    assert recorder[0][-2:] == [first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(dest)]


def test_fresh_box_git_inits_registers_and_runs_publish(monkeypatch, tmp_path):
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
    assert any(
        a[-3:] == ["set", first_run._KLABAUTER_MIRROR_PATH_REGISTRY_KEY, str(build_dest)] for a in argvs
    )
    assert any(str(claude_klabauter_root / "coordinator" / "bin" / "publish.py") in a for a in argvs)
    assert any(
        a[-3:] == ["set", first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(build_dest)] for a in argvs
    )


def test_publish_round_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys):
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
    # Never registered as the engine when publish failed.
    assert not any(
        a[-3:] == ["set", first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(build_dest)] for a in recorder
    )


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
