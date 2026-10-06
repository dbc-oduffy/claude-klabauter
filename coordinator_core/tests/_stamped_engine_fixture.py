"""Stamped/unstamped engine-copy fixture for real-interpreter cold-CLI measurement.

`build_engine_copy` materialises a runnable copy of this checkout's engine; `cold_cli_env`
builds a child environment that carries no test allowance. Copy and warm-up cost are
one-off per fixture and never part of a measurement.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_ROOTS = ("coordinator_core", "coordinator/bin", "coordinator/lib")
_ENGINE_SUFFIXES = frozenset({".py", ".json", ".yaml", ".toml", ".txt", ".scm"})
_SKIP_DIRS = frozenset({"tests", "ceremony_tests", "__pycache__", "scratch", ".git"})
_STAMP_REL = ("coordinator_core", "_engine_stamp")
_STRIPPED_ENV = (
    "PYTHONPATH",
    "COORDINATOR_PYTEST_ALLOW_UNSTAMPED_DISPATCH",
    "PYTEST_CURRENT_TEST",
)
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _engine_files(source_root: Path):
    for rel in _ENGINE_ROOTS:
        base = source_root / rel
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix in _ENGINE_SUFFIXES and not name.startswith("test_"):
                    yield path


def cold_cli_env(engine_root: Path) -> dict:
    """Child env for a cold CLI run against `engine_root`: warm server off, engine pinned,
    and no pytest or unstamped-dispatch allowance inherited."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _STRIPPED_ENV and not k.startswith("CLAUDE")
    }
    env["COORDINATOR_SESSION_ID"] = "stamped-engine-fixture-session"
    env["COORDINATOR_WARM"] = "0"
    env["COORDINATOR_ENGINE_ROOT"] = str(engine_root)
    return env


def spawn_path_env(env: dict) -> dict:
    """`env` with the in-process rung declined, so the stamped tree takes the child-spawn path."""
    return {**env, "COORDINATOR_TEST_NO_IN_PROCESS_RUNG": "1"}


def build_engine_copy(dest: Path, *, stamped: bool) -> Path:
    """Copy the engine file set into `dest`; write a non-empty `coordinator_core/_engine_stamp`
    when `stamped`. Runs a discarded `.pyc` warm-up so compile cost is never measured."""
    dest = Path(dest)
    for src in _engine_files(_REPO_ROOT):
        target = dest / src.relative_to(_REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    if stamped:
        stamp = dest.joinpath(*_STAMP_REL)
        stamp.write_bytes(b"c5-fixture-stamped-engine-build\n")
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "-j", "1", str(dest)],
        env=cold_cli_env(dest),
        cwd=str(dest),
        check=False,
        capture_output=True,
        creationflags=_NO_CONSOLE,
    )
    return dest
