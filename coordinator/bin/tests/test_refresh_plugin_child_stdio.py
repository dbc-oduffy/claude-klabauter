"""test_refresh_plugin_child_stdio — pytest coverage for the stdio contract
`refresh-plugin-live-install.py :: _run_refresh_cmd` holds over a
registry-supplied `refresh_cmd`.

Spec backlink: cross-repo/inbox/2026-09-02-example-game-repo-em-refresh-live-install-
warm-worker.md (Defect 1). A warm-served op runs in-process inside a
`coordinator_core/warm/server.py` pool worker spawned as `pythonw.exe`, whose
`sys.stdout`/`sys.stderr` are `None` and are rebound to `os.devnull` by
`_bind_null_std_streams`. The wrapper's own `print`s survive because they go
through the Python-level stream the warm entry seam captures; a CHILD process
writes to OS-level handles, which are the worker's — so an inherited-stdio
child's output lands in a sink with no reader, on every copy_install refresh
through the door, pass or fail. The reporter confirmed the pass case
independently: a run that exited 0 still showed nothing between "running
(cwd=...)" and "done".

Three properties are guarded here, all three regressions this file has
already suffered once:

  1. The child's streams are PIPED, never inherited — the property that makes
     the output reachable at all from a warm-served run.
  2. Output is ECHOED as it arrives rather than returned only at the end. The
     first repair of this defect traded live streaming for `capture_output`,
     which is a real regression for an operator watching a ~20s installer.
  3. The child's stdin is `DEVNULL`, not the caller's. NOT because inherited
     stdin was the cause of the reported rc=1 — measured, in a faithful
     detached-parent/`pythonw`-spawn/`ProcessPoolExecutor` reproduction, an
     all-inherited child exits 0 and a child that READS inherited stdin exits
     0 having read zero bytes — but because an installer's stdin must not
     depend on what a pooled worker happens to hold, which varies with how
     that worker was spawned and is not a property this file controls.
"""
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
    # Merged onto `stdout` on purpose: this is a transcript an operator reads,
    # and separated pipes would reorder an installer's own interleaving.
    assert "OUT-LINE" in result.stdout
    assert "ERR-LINE" in result.stdout
    assert result.stderr == ""


def test_child_output_is_echoed_to_this_process_stream(tmp_path, capsys):
    """The echo is what a warm-served caller actually receives: this process's
    `print` goes through the Python-level stream the entry seam captures,
    which the child's own OS handles never reach."""
    argv = _child(
        tmp_path,
        "echo.py",
        "import sys\nsys.stdout.write('VISIBLE-TO-CALLER' + chr(10))\n",
    )
    _mod._run_refresh_cmd(argv, tmp_path)

    assert "VISIBLE-TO-CALLER" in capsys.readouterr().out


def test_child_stdin_is_devnull_not_the_callers(tmp_path):
    """A child that reads stdin gets a deterministic empty stream rather than
    whatever handle the enclosing process (a pooled warm worker, a console, a
    pipe) happens to hold."""
    argv = _child(
        tmp_path,
        "reads_stdin.py",
        "import sys\nsys.stdout.write('READ=' + repr(sys.stdin.read()) + chr(10))\n",
    )
    result = _mod._run_refresh_cmd(argv, tmp_path)

    assert result.returncode == 0
    assert "READ=''" in result.stdout


def test_nonzero_child_returncode_is_propagated_with_its_output(tmp_path):
    """The failure path is the one that used to leave zero evidence — rc AND
    the child's own text must both survive it."""
    argv = _child(
        tmp_path,
        "fails.py",
        "import sys\nsys.stderr.write('WHY-IT-FAILED' + chr(10))\nsys.exit(3)\n",
    )
    result = _mod._run_refresh_cmd(argv, tmp_path)

    assert result.returncode == 3
    assert "WHY-IT-FAILED" in result.stdout


def test_unlaunchable_command_raises_oserror_for_the_call_site(tmp_path):
    """`_handle_copy_install` catches `OSError` around this call and routes it
    to snapshot-restore; swallowing it here would leave the live install
    neither refreshed nor restored."""
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
