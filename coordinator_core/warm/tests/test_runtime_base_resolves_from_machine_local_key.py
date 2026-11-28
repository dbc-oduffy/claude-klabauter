"""The warm runtime base resolves from the settings-home machine-local key.

Order, shared with door.c and door_posix.c: `COORDINATOR_WARM_RUNTIME_BASE`
> settings-home `machine-local/.coordinator-warm-runtime-base` > LOCALAPPDATA
> ~/.cache.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb
from coordinator_core.warm.tests.door_test_support import (
    WINDOWS_ONLY,
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

_DOOR_DIR = Path(breadcrumb.__file__).resolve().parent / "door"


@pytest.fixture
def settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.delenv(breadcrumb.RUNTIME_BASE_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    return home


def _write_key(home: Path, text: str) -> None:
    (home / "machine-local" / breadcrumb.RUNTIME_BASE_KEY_FILE).write_text(text, encoding="utf-8")


def test_key_file_moves_the_runtime_dir(settings_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "d-drive" / "warm-runtime"
    _write_key(settings_home, f"{target}\n")
    assert breadcrumb.runtime_base() == target
    svc = breadcrumb.svc_dir(tmp_path / "clone")
    assert target in svc.parents
    assert (tmp_path / "local-app-data") not in svc.parents


def test_unset_key_falls_back_to_localappdata(settings_home: Path, tmp_path: Path) -> None:
    assert breadcrumb.runtime_base() == tmp_path / "local-app-data"


@pytest.mark.parametrize("text", ["", "   \n", "relative/dir"])
def test_blank_or_relative_key_reads_as_unset(settings_home: Path, tmp_path: Path, text: str) -> None:
    _write_key(settings_home, text)
    assert breadcrumb.runtime_base() == tmp_path / "local-app-data"


def test_env_seam_outranks_the_key(settings_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_key(settings_home, str(tmp_path / "keyed"))
    seam = tmp_path / "seam"
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(seam))
    assert breadcrumb.runtime_base() == seam


def _door_boot_lock(tmp_path: Path, env_extra: dict, home: Path) -> Path:
    """The binder's boot-lock path, after the door's no-endpoint spawn debounce wrote its own."""
    root = make_stub_engine_root(tmp_path)
    server_dir = root / "coordinator_core" / "warm"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.py").write_text("pass\n", encoding="utf-8")
    exe = door_under_default_name(root)
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINATOR_")}
    env.pop("LOCALAPPDATA", None)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_SETTINGS_HOME=str(home),
        COORDINATOR_DOOR_STDIN_MODE="hook",
        **env_extra,
    )
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    ).encode()
    subprocess.run(
        [str(exe), "hooks.preuse_bash_dispatch"], input=payload, capture_output=True, env=env,
        cwd=str(root), timeout=60, **no_console_creationflags(),
    )
    return breadcrumb.svc_dir(root) / "warm.json.boot.lock"


@WINDOWS_ONLY
def test_built_door_and_binder_resolve_the_same_dir_under_the_key(
    settings_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "d-drive" / "warm-runtime"
    _write_key(settings_home, f"{target}\n")
    lock = _door_boot_lock(tmp_path, {}, settings_home)
    assert target in lock.parents
    assert lock.exists(), "the door resolved a different runtime dir than the binder"


@WINDOWS_ONLY
def test_built_door_env_seam_outranks_the_key(
    settings_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed = tmp_path / "keyed"
    seam = tmp_path / "seam"
    _write_key(settings_home, str(keyed))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(seam))
    lock = _door_boot_lock(tmp_path, {breadcrumb.RUNTIME_BASE_ENV: str(seam)}, settings_home)
    assert seam in lock.parents and lock.exists()
    assert not keyed.exists()


def test_both_door_sources_read_the_same_key_file() -> None:
    for name in ("door.c", "door_posix.c"):
        text = (_DOOR_DIR / name).read_text(encoding="utf-8")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith(("/*", "*", "//"))]
        assert any(breadcrumb.RUNTIME_BASE_KEY_FILE in ln for ln in code), name
