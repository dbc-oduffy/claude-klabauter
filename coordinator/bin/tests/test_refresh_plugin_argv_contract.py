"""test_refresh_plugin_argv_contract — pytest coverage for the argv-only
command contract in refresh-plugin-live-install.py.

Spec backlink: tasks/shell-spawn-regrowth-gate/brief-C7a.md — PM ruling
2026-08-06 ("no shell spawns are tolerated"). Both `shell=True` sites
(COORDINATOR_REFRESH_VENV_INSTALL_CMD override, and the copy_install leg's
registry-supplied `refresh_cmd`) were converted to shlex.split + argv-mode
subprocess.run — no host shell (`/bin/sh` / `cmd.exe`) is dispatched, and a
value that will not `shlex.split` or whose first token is not an executable
now fails with a diagnostic naming the argv-only contract and the specific
config key/env var at fault, instead of a bare traceback or silent skip.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install_argv_contract",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
_parse_argv_command = _mod._parse_argv_command
ArgvCommandError = _mod.ArgvCommandError


def test_windows_backslash_path_survives_argv_parsing(tmp_path):
    windows_shaped_name = "tools\\refresh.exe"
    fake_exe = tmp_path / windows_shaped_name
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("")
    windows_shaped = str(fake_exe)
    argv = _parse_argv_command(f"{windows_shaped} --flag", "TEST_CMD", resolve_cwd=tmp_path)
    assert argv[0] == windows_shaped
    assert "\\" in argv[0]
    assert argv[1:] == ["--flag"]


def test_argv_shaped_value_parses_and_resolves():
    argv = _parse_argv_command(f"{sys.executable} -c 'print(1)'", "TEST_CMD")
    assert argv[0] == sys.executable
    assert argv[1:] == ["-c", "print(1)"]


def test_unbalanced_quotes_raise_with_diagnostic():
    with pytest.raises(ArgvCommandError) as excinfo:
        _parse_argv_command("echo 'unterminated", "MY_ENV_VAR")
    msg = str(excinfo.value)
    assert "MY_ENV_VAR" in msg
    assert "argv-only" in msg.lower()


def test_shell_word_form_with_unresolvable_executable_raises():
    with pytest.raises(ArgvCommandError) as excinfo:
        _parse_argv_command(
            "FOO=bar ./definitely-not-a-real-executable-xyz", "plugin.mirrors.demo.refresh_cmd"
        )
    msg = str(excinfo.value)
    assert "plugin.mirrors.demo.refresh_cmd" in msg
    assert "FOO=bar" in msg


def test_empty_string_raises_with_diagnostic():
    with pytest.raises(ArgvCommandError) as excinfo:
        _parse_argv_command("   ", "TEST_CMD")
    assert "TEST_CMD" in str(excinfo.value)


def test_copy_install_snapshot_restore_fires_on_parse_failure(tmp_path):
    plugins_dir = tmp_path / "plugins"
    live_path = plugins_dir / "my-plugin"
    live_path.mkdir(parents=True)
    (live_path / "marker.txt").write_text("live-before-refresh")
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()
    snapshots_dir = tmp_path / "snapshots"
    refresh_log = tmp_path / "refresh-log"
    registry_local = tmp_path / "registry.local.toml"

    rc = _mod._handle_copy_install(
        "my-plugin",
        str(source_path),
        str(live_path),
        "'unterminated quote refresh_cmd",
        False,
        plugins_dir,
        snapshots_dir,
        refresh_log,
        registry_local,
    )

    assert rc == 1
    snapshots = list(snapshots_dir.glob("my-plugin-*"))
    assert snapshots, "snapshot must have been taken before the parse failure"


def test_copy_install_snapshot_restore_fires_on_nonzero_rc(tmp_path):
    plugins_dir = tmp_path / "plugins"
    live_path = plugins_dir / "my-plugin"
    live_path.mkdir(parents=True)
    (live_path / "marker.txt").write_text("live-before-refresh")
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()
    snapshots_dir = tmp_path / "snapshots"
    refresh_log = tmp_path / "refresh-log"
    registry_local = tmp_path / "registry.local.toml"

    false_bin = shutil.which("false") or "/usr/bin/false"

    rc = _mod._handle_copy_install(
        "my-plugin",
        str(source_path),
        str(live_path),
        false_bin,
        False,
        plugins_dir,
        snapshots_dir,
        refresh_log,
        registry_local,
    )

    assert rc == 1
    snapshots = list(snapshots_dir.glob("my-plugin-*"))
    assert snapshots, "snapshot must have been taken and preserved for restore"

