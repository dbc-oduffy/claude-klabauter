from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install_stdio_test",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _child(tmp_path: Path, name: str, body: str) -> list[str]:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_child_stdout_and_stderr_are_captured_and_merged(tmp_path):
    argv = _child(
        tmp_path,
        "emit.py",
        "import sys\n"
        "sys.stdout.write('OUT-LINE' + chr(10))\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('ERR-LINE' + chr(10))\n",
    )
    result = _mod._run_refresh_cmd(argv, tmp_path)

    assert result.returncode == 0
    assert "OUT-LINE" in result.stdout
    assert "ERR-LINE" in result.stdout
    assert result.stderr == ""


def test_child_output_is_echoed_to_this_process_stream(tmp_path, capsys):
    argv = _child(
        tmp_path,
        "echo.py",
        "import sys\nsys.stdout.write('VISIBLE-TO-CALLER' + chr(10))\n",
    )
    _mod._run_refresh_cmd(argv, tmp_path)

    assert "VISIBLE-TO-CALLER" in capsys.readouterr().out


def test_child_stdin_is_devnull_not_the_callers(tmp_path):
    argv = _child(
        tmp_path,
        "reads_stdin.py",
        "import sys\nsys.stdout.write('READ=' + repr(sys.stdin.read()) + chr(10))\n",
    )
    result = _mod._run_refresh_cmd(argv, tmp_path)

    assert result.returncode == 0
    assert "READ=''" in result.stdout


def test_nonzero_child_returncode_is_propagated_with_its_output(tmp_path):
    argv = _child(
        tmp_path,
        "fails.py",
        "import sys\nsys.stderr.write('WHY-IT-FAILED' + chr(10))\nsys.exit(3)\n",
    )
    result = _mod._run_refresh_cmd(argv, tmp_path)

    assert result.returncode == 3
    assert "WHY-IT-FAILED" in result.stdout


def test_unlaunchable_command_raises_oserror_for_the_call_site(tmp_path):
    with pytest.raises(OSError):
        _mod._run_refresh_cmd(["definitely-not-a-real-binary-xyzzy"], tmp_path)


def test_child_runs_in_the_given_cwd(tmp_path):
    argv = _child(
        tmp_path,
        "pwd.py",
        "import os, sys\nsys.stdout.write('CWD=' + os.getcwd() + chr(10))\n",
    )
    workdir = tmp_path / "sub"
    workdir.mkdir()
    result = _mod._run_refresh_cmd(argv, workdir)

    assert str(workdir.resolve()) in result.stdout


def test_failure_log_carries_the_env_names_the_warm_door_mutates():
    """`FORWARDING_SET`'s `CLAUDE_*` entries carry the CALLER's value into a
    warm-served child where they used to carry the SERVER's. A failure log
    that omits them cannot show the difference it exists to show."""
    assert "CLAUDE_" in _mod._FAILURE_ENV_KEY_PREFIXES
    assert "COORDINATOR_" in _mod._FAILURE_ENV_KEY_PREFIXES
    assert "PATH" in _mod._FAILURE_ENV_KEYS
