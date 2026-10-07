"""Fleet Temp layout resolver: every path under system Temp that coordinator owns.

Stdlib-only. Paths are built from ``tempfile.gettempdir()`` at call time, never a
literal ``%TEMP%`` or ``/tmp``, so a monkeypatched ``gettempdir`` is honored.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

FLEET_DIRNAME = "_fleet"
_ROOT_DIRNAME = "coordinator"


def _coordinator_root() -> Path:
    return Path(tempfile.gettempdir()) / _ROOT_DIRNAME


def coordinator_temp_root(repo_root: Path | str) -> Path:
    """``<gettempdir()>/coordinator/<repo dir name>/``; ValueError for a repo named ``_fleet``."""
    name = Path(repo_root).name
    if not name:
        raise ValueError(f"repo_root has no directory name: {repo_root!r}")
    if name == FLEET_DIRNAME:
        raise ValueError(f"repo directory name {FLEET_DIRNAME!r} is reserved for host-scoped Temp")
    return _coordinator_root() / name


def fleet_temp_root() -> Path:
    """``<gettempdir()>/coordinator/_fleet/``: host-scoped, never a purge root."""
    return _coordinator_root() / FLEET_DIRNAME


def pytest_basetemp_root(repo_root: Path | str) -> Path:
    """``coordinator_temp_root(repo_root)/pytest/``: parent of per-run pytest basetemps."""
    return coordinator_temp_root(repo_root) / "pytest"
