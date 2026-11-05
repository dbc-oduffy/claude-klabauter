"""coordinator_core.install.engine_interpreter -- the engine-owned interpreter.

Purpose: when the installer's base interpreter is PEP-668 externally managed
(fresh Homebrew python3), the engine runs from a stdlib ``venv`` under the
settings home that the installer builds once and stamps. Not a fallback beside
a machine interpreter: see docs/decisions/DR-engine-interpreter-on-a-guarded-base.md.

Public surface:

    def engine_interpreter_dir(home: Optional[Path] = None) -> Path: ...
    def interpreter_path(venv_dir: Path) -> Path: ...
    def ensure(base_python: str, home: Optional[Path] = None) -> str: ...
    def stamped(home: Optional[Path] = None) -> Optional[str]: ...

``home`` is the settings-home root; ``None`` resolves ``settings_home()``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.win_portability import no_console_creationflags

_DIR_NAME = "engine-interpreter"
_CREATE_TIMEOUT_S = 120


def _is_windows() -> bool:
    return os.name == "nt"


def engine_interpreter_dir(home: Optional[Path] = None) -> Path:
    """The engine interpreter's venv directory under the settings home."""
    return (Path(home) if home is not None else settings_home()) / _DIR_NAME


def interpreter_path(venv_dir: Path) -> Path:
    """The interpreter inside ``venv_dir``: ``Scripts/python.exe`` on Windows, ``bin/python`` elsewhere."""
    if _is_windows():
        return Path(venv_dir) / "Scripts" / "python.exe"
    return Path(venv_dir) / "bin" / "python"


def stamped(home: Optional[Path] = None) -> Optional[str]:
    """The engine interpreter path when it exists as an executable file, else None. Zero spawn."""
    py = interpreter_path(engine_interpreter_dir(home))
    if py.is_file() and os.access(py, os.X_OK):
        return str(py)
    return None


def ensure(base_python: str, home: Optional[Path] = None) -> str:
    """Return the engine interpreter path, creating it with ``base_python -m venv`` if absent.

    Idempotent: an existing working interpreter is reused with no spawn.
    Raises RuntimeError when creation fails or leaves no interpreter.
    """
    existing = stamped(home)
    if existing is not None:
        return existing
    venv_dir = engine_interpreter_dir(home)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [base_python, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=_CREATE_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"engine interpreter creation failed: {exc}") from exc
    created = stamped(home)
    if proc.returncode != 0 or created is None:
        raise RuntimeError(
            f"engine interpreter creation failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )
    return created
