"""Pytest plugin that refuses a nested pytest session, plus the env every engine-spawned pytest carries.

Load with ``-p coordinator_core.testing.nested_session_guard``; ``guarded_child_env`` injects it
through PYTEST_ADDOPTS so every descendant (engine-spawned or a raw ``subprocess`` inside a test)
loads it. Not registered in the root conftest: the conftest does its own check in this repo.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from coordinator_core.testing import spawn_depth

PLUGIN_MODULE = "coordinator_core.testing.nested_session_guard"
REFUSAL_CODE = 70
XDIST_WORKER_KEY = "PYTEST_XDIST_WORKER"
BLAS_THREAD_KEYS = spawn_depth.BLAS_THREAD_KEYS

# Directory holding the ``coordinator_core`` package this module was imported from.
ENGINE_ROOT = str(Path(__file__).resolve().parents[2])


def guarded_child_env(env: Mapping[str, str]) -> dict:
    """Env for an engine-spawned pytest: depth + 1, the plugin in PYTEST_ADDOPTS, engine root on PYTHONPATH, BLAS pools at one thread unless set.

    The PYTHONPATH entry keeps ``-p`` importable when the child runs from a repo that has no
    ``coordinator_core`` of its own. Idempotent: applying it twice adds nothing more.
    """
    out = spawn_depth.child_env(env)
    addopts = out.get("PYTEST_ADDOPTS", "")
    if PLUGIN_MODULE not in addopts:
        out["PYTEST_ADDOPTS"] = f"{addopts} -p {PLUGIN_MODULE}".strip()
    entries = [e for e in out.get("PYTHONPATH", "").split(os.pathsep) if e]
    if ENGINE_ROOT not in entries:
        out["PYTHONPATH"] = os.pathsep.join([*entries, ENGINE_ROOT])
    return out


def pytest_configure(config) -> None:
    """Count this session's hop and exit past MAX_DEPTH; an xdist worker is never refused."""
    if os.environ.get(XDIST_WORKER_KEY):
        return
    depth = spawn_depth.enter_session(os.environ)
    if depth > spawn_depth.MAX_DEPTH:
        import pytest  # noqa: PLC0415

        pytest.exit(
            f"refused: pytest nesting depth {depth} exceeds {spawn_depth.MAX_DEPTH} "
            f"({spawn_depth.ENV_KEY}); run the selection from the top-level session instead",
            returncode=REFUSAL_CODE,
        )
