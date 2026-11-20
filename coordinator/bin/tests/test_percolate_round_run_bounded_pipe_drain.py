"""percolate-round legs return when the child exits, not when every holder of its pipe lets go.

A grandchild that inherits the child's stdout/stderr and outlives it must not hold the round.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_run_bounded_pipe_drain.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _BIN_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_GRANDCHILD_LIFETIME_SECS = 40
_PROMPT_SECS = 15.0

# The grandchild lives until the stop file appears (the test's cleanup), or its own lifetime cap.
_GRANDCHILD_SRC = (
    "import os, sys, time\n"
    f"end = time.time() + {_GRANDCHILD_LIFETIME_SECS}\n"
    "while time.time() < end and not os.path.exists(sys.argv[1]):\n"
    "    time.sleep(0.1)\n"
)
_CHILD_SRC = (
    "import subprocess, sys\n"
    f"subprocess.Popen([sys.executable, '-c', {_GRANDCHILD_SRC!r}, sys.argv[1]])\n"
    "print('child-out', flush=True)\n"
    "print('child-err', file=sys.stderr, flush=True)\n"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("percolate_round_pipe_drain", _BIN_DIR / "percolate-round.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


@pytest.fixture
def stop_file(tmp_path):
    path = tmp_path / "stop-grandchild"
    yield path
    path.write_text("stop", encoding="utf-8")


@pytest.mark.deliberate_wall_clock(reason="returning at child exit rather than at pipe close is a wall-clock bound; that bound is the behaviour under test")
def test_run_returns_when_child_exits_though_grandchild_holds_stdout(stop_file):
    started = time.monotonic()
    result = _mod._run([sys.executable, "-c", _CHILD_SRC, str(stop_file)], timeout=300.0)
    elapsed = time.monotonic() - started
    assert elapsed < _PROMPT_SECS, f"_run waited {elapsed:.1f}s on a pipe the exited child no longer owned"
    assert result.returncode == 0
    assert "child-out" in result.stdout
    assert "child-err" in result.stderr


def test_run_timeout_still_returns_124_with_partial_output():
    src = "import time\nprint('before', flush=True)\ntime.sleep(60)\n"
    result = _mod._run([sys.executable, "-c", src], timeout=3.0)
    assert result.returncode == 124
    assert "before" in result.stdout
    assert "timed out after 3.0s" in result.stderr


@pytest.mark.deliberate_wall_clock(reason="returning at git exit rather than at pipe close is a wall-clock bound; that bound is the behaviour under test")
def test_git_bytes_returns_when_git_exits_though_a_holder_keeps_the_pipe(tmp_path, monkeypatch, stop_file):
    real_run = subprocess.run
    seen = {}

    def _redirect(cmd, **kwargs):
        seen["cmd"] = cmd
        return real_run([sys.executable, "-c", _CHILD_SRC, str(stop_file)], **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _redirect)
    started = time.monotonic()
    result = _mod._git_bytes(tmp_path, "rev-parse", "HEAD")
    elapsed = time.monotonic() - started
    assert seen["cmd"][0] == "git"
    assert elapsed < _PROMPT_SECS
    assert b"child-out" in result.stdout


def test_ordinary_leg_output_is_captured_as_text_and_bytes(tmp_path):
    ok = _mod._run(
        [sys.executable, "-c", "import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)"], timeout=60.0
    )
    assert (ok.returncode, ok.stdout.strip(), ok.stderr.strip()) == (3, "a", "b")
    git = _mod._git_bytes(tmp_path, "--version")
    assert git.returncode == 0 and git.stdout.startswith(b"git version")


def test_run_feeds_text_input_to_the_child():
    result = _mod._run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], timeout=60.0, input="abc\n"
    )
    assert result.stdout.strip() == "ABC"
