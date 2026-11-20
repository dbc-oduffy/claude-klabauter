"""When the warm engine's own root is what the box sidecar names AND that root is not the
registered installed engine, the engine boot warns loudly and durably. Never a hard fail:
a deliberate retarget keeps working."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core._settings_home import settings_home
from coordinator_core.warm import server
from coordinator_core.warm.door import sidecar_guard as g


def _setup(tmp_path: Path, sidecar_root: Path | None) -> tuple[Path, Path]:
    home = settings_home()
    (home / "machine-local").mkdir(parents=True, exist_ok=True)
    installed = tmp_path / "installed"
    installed.mkdir()
    (home / "machine-local" / "registry.local.toml").write_text(
        f"[repos]\nclaude_klabauter = '{installed.as_posix()}'\n", encoding="utf-8"
    )
    (home / "bin").mkdir(parents=True, exist_ok=True)
    if sidecar_root is not None:
        (home / "bin" / g.SIDECAR_NAME).write_text(str(sidecar_root) + "\n", encoding="utf-8")
    return home, installed


def test_drift_names_both_paths_and_mtime(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch" / "klabauter-x"
    scratch.mkdir(parents=True)
    home, installed = _setup(tmp_path, scratch)
    drift = g.sidecar_drift(home)
    assert drift is not None
    assert drift["sidecar"] == str(scratch) and drift["registered"] == installed.as_posix()
    assert drift["sidecar_mtime"].endswith("Z")


def test_no_drift_when_sidecar_is_the_registered_engine(tmp_path: Path) -> None:
    home, installed = _setup(tmp_path, None)
    (home / "bin" / g.SIDECAR_NAME).write_text(str(installed) + "\n", encoding="utf-8")
    assert g.sidecar_drift(home) is None


def test_no_drift_without_a_sidecar_or_registry(tmp_path: Path) -> None:
    home = settings_home()
    assert g.sidecar_drift(home) is None


def test_engine_boot_warns_loudly_and_durably_without_failing(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("COORDINATOR_DOOR_ENGINE_ROOT", raising=False)
    scratch = tmp_path / "scratch" / "klabauter-x"
    scratch.mkdir(parents=True)
    home, installed = _setup(tmp_path, scratch)
    assert server._warn_box_sidecar_drift(scratch) is True
    err = capsys.readouterr().err
    assert str(scratch) in err and installed.as_posix() in err and "mtime" in err
    rows = [json.loads(x) for x in (home / g.GUARD_LOG_NAME).read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event"] == "read-drift"


def test_engine_boot_is_quiet_for_an_env_steered_scratch_engine(tmp_path: Path, capsys, monkeypatch) -> None:
    scratch = tmp_path / "scratch" / "klabauter-x"
    scratch.mkdir(parents=True)
    _setup(tmp_path, None)
    monkeypatch.setenv("COORDINATOR_DOOR_ENGINE_ROOT", str(scratch))
    assert server._warn_box_sidecar_drift(scratch) is False
    assert capsys.readouterr().err == ""
