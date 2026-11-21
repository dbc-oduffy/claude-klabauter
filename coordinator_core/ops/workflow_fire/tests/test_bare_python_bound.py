"""A host with only `python3` on PATH must not receive a prompt or script whose command word is bare `python`."""
from __future__ import annotations

import os
import re
import sys

import pytest

from coordinator_core.ops.dispatch_emit.ask_staged_prompts import bind_script_literals
from coordinator_core.ops.workflow_fire import worker_preamble
from coordinator_core.ops.workflow_fire.worker_preamble import bind_engine_commands

_BARE = re.compile(r"(?<![\w./\\-])python(?=[ \t]+(?:-m|-c|[^\s`'\"<>-]*[/\\]|\S+\.py))")

_COMMANDS = (
    "python -m coordinator_core.ops.test_leg_run --out scratch/r.json -- tests/test_x.py",
    "python -m pytest -rf tests/test_x.py",
    "python coordinator/bin/box-memory-snapshot.py --command-key k",
    "python coordinator_core/ops/ceremony/close_background.py run-1 --root .",
    "python scripts/launcher.py tests/test_x.py",
)


def _stub(directory, name):
    """An executable `name` that `shutil.which` finds on this OS."""
    path = directory / (name + (".exe" if os.name == "nt" else ""))
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


@pytest.fixture
def python3_only_host(tmp_path, monkeypatch):
    """PATH holds a `python3` and no `python`, the Linux box the halt was seen on."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "python3")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    worker_preamble._on_path.cache_clear()
    yield
    worker_preamble._on_path.cache_clear()


@pytest.mark.parametrize("command", _COMMANDS)
def test_prompt_command_has_no_bare_python_argv0(python3_only_host, command):
    bound = bind_engine_commands(f"Run `{command}` once.")
    assert not _BARE.search(bound), bound
    assert sys.executable in bound or worker_preamble._engine_command_runtime()[0] in bound


@pytest.mark.parametrize("command", _COMMANDS)
def test_emitted_script_literal_has_no_bare_python_argv0(python3_only_host, command):
    script = "const p = 'Step 2: run `" + command + "` then report.';\nagent(p);\n"
    assert _BARE.search(script)
    assert not _BARE.search(bind_script_literals(script))


def test_prose_naming_python_is_untouched(python3_only_host):
    prose = "Run Python as `python3`, falling back to `python`; never bare `python -m pytest`."
    assert bind_engine_commands(prose) == prose


def test_host_with_python_on_path_keeps_the_portable_word(tmp_path, monkeypatch):
    _stub(tmp_path, "python")
    monkeypatch.setenv("PATH", str(tmp_path))
    worker_preamble._on_path.cache_clear()
    command = "Run `python -m pytest tests/test_x.py`."
    assert bind_engine_commands(command) == command
    worker_preamble._on_path.cache_clear()
