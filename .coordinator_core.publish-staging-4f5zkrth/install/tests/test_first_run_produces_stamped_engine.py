"""Tests for `coordinator_core.install.first_run.provision_stamped_engine`
(docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md chunk C1).

Purpose: pin that a fresh box reaches a registered, STAMPED engine root
without a human running publish — the prerequisite that keeps C4's
fail-closed change from bricking a new install. Every subprocess boundary
(`git`, `coordinator/bin/publish.py`) is mocked, and the registry write is
faked at `first_run.registry_set`: this suite exercises the provisioning
LOGIC (destination resolution, idempotence, registry writes, failure
handling), never a real publish round.

Negative spec: nothing here may reinstate a `machine-local` CLI boundary.
An earlier revision had an autouse fixture handing every test a resolvable
`_resolve_machine_local_argv`, which mocked in a binary deleted from the
repo in `3bd2738f4` (2026-08-14) — so the suite stayed green while the real
function warn-and-returned False on every box, and AC5 read discharged on a
path that never ran. The registry write is in-process now (see
`first_run._register_engine_key`); a test that fakes a CLI here is testing a
mechanism that does not exist.

Negative spec: does not exercise `scripts/setup.py`'s call site directly —
that is a thin, one-branch call into this same function (see
`register_makima_root`'s C1 addition), and `scripts/test_setup.py` already
covers the identity/registration branching this hooks into.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.install import first_run


def _fake_run_factory(recorder, publish_rc=0, git_ok=True, write_stamp_on_publish=True, stamp_path=None):
    """Build a stand-in for `first_run._run` that records every argv and
    fakes each subprocess boundary this function crosses."""

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
    """Capture every in-process registry write as `(key, value)` on
    `request.node.registry_writes` — the boundary that replaced the
    `machine-local set` subprocess. A test wanting a failing write
    monkeypatches `first_run.registry_set` itself to raise."""
    writes = []
    request.node.registry_writes = writes
    monkeypatch.setattr(first_run, "registry_set", lambda key, value: writes.append((key, value)))


def test_provisions_without_any_machine_local_cli(monkeypatch, tmp_path, request):
    """AC5 regression: a box with no `machine-local` binary anywhere — the
    state of EVERY box since `3bd2738f4` deleted it — still reaches a
    registered, stamped engine. The pre-fix code warn-and-returned False
    here; nothing in `provision_stamped_engine` may resolve that binary
    again."""
    build_dest = tmp_path / "settings-home" / "engine-build" / "claude-klabauter"
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    makima_root = tmp_path / "makima"
    (makima_root / "coordinator" / "bin").mkdir(parents=True)
    (makima_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")
    assert not (makima_root / "coordinator" / "bin" / "machine-local").exists()

    stamp_path = build_dest / "coordinator_core" / "_engine_stamp"
    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder, stamp_path=stamp_path))

    assert first_run.provision_stamped_engine(makima_root) is True
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
    # Only the idempotent re-registration write happens — no git init, no publish.
    assert recorder == []
    assert request.node.registry_writes == [(first_run._KLABAUTER_MIRROR_REGISTRY_KEY, str(dest))]


def test_fresh_box_git_inits_registers_and_runs_publish(monkeypatch, tmp_path, request):
    build_dest = tmp_path / "settings-home" / "engine-build" / "claude-klabauter"

    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    makima_root = tmp_path / "makima"
    (makima_root / "coordinator" / "bin").mkdir(parents=True)
    (makima_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")

    stamp_path = build_dest / "coordinator_core" / "_engine_stamp"
    recorder = []
    monkeypatch.setattr(
        first_run,
        "_run",
        _fake_run_factory(recorder, publish_rc=0, stamp_path=stamp_path),
    )

    result = first_run.provision_stamped_engine(makima_root)

    assert result is True
    assert build_dest.is_dir()
    assert stamp_path.is_file()

    argvs = recorder
    assert any(a[:2] == ["git", "init"] for a in argvs)
    assert any(a[:1] == ["git"] and "commit" in a for a in argvs)
    assert any(str(makima_root / "coordinator" / "bin" / "publish.py") in a for a in argvs)
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

    makima_root = tmp_path / "makima"
    (makima_root / "coordinator" / "bin").mkdir(parents=True)
    (makima_root / "coordinator" / "bin" / "publish.py").write_text("# fake\n", encoding="utf-8")

    recorder = []
    monkeypatch.setattr(
        first_run,
        "_run",
        _fake_run_factory(recorder, publish_rc=1, stamp_path=build_dest / "coordinator_core" / "_engine_stamp"),
    )

    result = first_run.provision_stamped_engine(makima_root)

    assert result is False
    err = capsys.readouterr().err
    assert "publish round into" in err
    assert "exited 1" in err
    # Never registered as the engine when publish failed.
    assert first_run._KLABAUTER_MIRROR_REGISTRY_KEY not in [
        key for key, _ in request.node.registry_writes
    ]


def test_git_init_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )
    makima_root = tmp_path / "makima"

    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder, git_ok=False))

    result = first_run.provision_stamped_engine(makima_root)

    assert result is False
    assert "git init" in capsys.readouterr().err


def test_registry_write_failure_warns_and_returns_false(monkeypatch, tmp_path, capsys):
    """`registry_set` raises `ValueError`/`OSError` per its own contract; the
    provisioning path must warn-and-return, never propagate — the same
    warn-and-continue shape the removed `set_proc.returncode != 0` branch
    had."""
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: tmp_path / "settings-home"
    )

    def _raising_set(key, value):
        raise OSError("registry.local.toml is read-only")

    monkeypatch.setattr(first_run, "registry_set", _raising_set)

    makima_root = tmp_path / "makima"
    recorder = []
    monkeypatch.setattr(first_run, "_run", _fake_run_factory(recorder))

    assert first_run.provision_stamped_engine(makima_root) is False
    err = capsys.readouterr().err
    assert first_run._KLABAUTER_MIRROR_PATH_REGISTRY_KEY in err
    assert "read-only" in err
