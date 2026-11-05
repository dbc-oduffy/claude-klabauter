"""coordinator_core.install.engine_interpreter -- the engine-site provider (DR-478).

Purpose: on a PEP-668 externally-managed base interpreter the installer puts the
declared third-party deps in ``<settings-home>/engine-site`` with one
``pip install --target``; the engine bootstrap puts that directory on
``sys.path`` while its stamp matches the running interpreter's major.minor.
No second interpreter exists; hooks and shims keep the system one.

Public surface:

    def engine_site_dir(home: Optional[Path] = None) -> Path: ...
    def stamp(home: Optional[Path] = None) -> Optional[tuple[int, int]]: ...
    def ensure(base_python: str, dep_specs: Sequence[str], home: Optional[Path] = None) -> Path: ...
    def health(interpreter_path: str, home: Optional[Path] = None) -> EngineSiteHealth: ...

``home`` is the settings-home root; ``None`` resolves ``settings_home()``.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from coordinator_core import _settings_home
from coordinator_core.win_portability import no_console_creationflags

_DIR_NAME = "engine-site"
_STAMP_NAME = ".engine-site-stamp"
_PIP_TIMEOUT_S = 600
_PROBE_TIMEOUT_S = 30


def engine_site_dir(home: Optional[Path] = None) -> Path:
    """The engine-site directory under the settings home."""
    return (Path(home) if home is not None else _settings_home.settings_home()) / _DIR_NAME


def _read_stamp(home: Optional[Path]) -> Optional[tuple[tuple[int, int], str]]:
    try:
        lines = (engine_site_dir(home) / _STAMP_NAME).read_text(encoding="utf-8").splitlines()
        major, minor = lines[0].strip().split(".")
        return (int(major), int(minor)), (lines[1].strip() if len(lines) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def stamp(home: Optional[Path] = None) -> Optional[tuple[int, int]]:
    """The major.minor engine-site was provisioned for, or None when unstamped. Zero spawn."""
    read = _read_stamp(home)
    return read[0] if read else None


REMEDY = "python3 scripts/setup.py"
_VERSIONED_BASENAME = re.compile(r"^python(\d+)\.(\d+)(?:\.exe)?$", re.IGNORECASE)


class EngineSiteHealth(NamedTuple):
    """``status``: ok | missing | stamp-mismatch | yaml-absent | unknown. ``remedy`` is the fix command."""

    status: str
    stamp_minor: Optional[tuple[int, int]]
    interp_minor: Optional[tuple[int, int]]
    remedy: str


def health(interpreter_path: str, home: Optional[Path] = None) -> EngineSiteHealth:
    """Will ``interpreter_path`` load yaml from engine-site? File stats only; never spawns.

    The interpreter's minor comes from its realpath basename (``python3.15``); a name
    carrying no minor (``python.exe``) is ``unknown``, never a guess.
    """
    try:
        match = _VERSIONED_BASENAME.match(os.path.basename(os.path.realpath(interpreter_path)))
    except (OSError, ValueError):
        match = None
    interp = (int(match.group(1)), int(match.group(2))) if match else None
    stamped = stamp(home)
    if interp is None:
        status = "unknown"
    elif stamped is None:
        status = "missing"
    elif stamped != interp:
        status = "stamp-mismatch"
    elif not (engine_site_dir(home) / "yaml").is_dir():
        status = "yaml-absent"
    else:
        status = "ok"
    return EngineSiteHealth(status, stamped, interp, REMEDY)


def _specs_digest(dep_specs: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(sorted(dep_specs)).encode("utf-8")).hexdigest()[:16]


def _minor_of(base_python: str) -> tuple[int, int]:
    """major.minor of ``base_python``; zero spawn when it is the running interpreter."""
    try:
        if os.path.realpath(base_python) == os.path.realpath(sys.executable):
            return (sys.version_info[0], sys.version_info[1])
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [base_python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            **no_console_creationflags(),
        )
        major, minor = proc.stdout.strip().split(".")
        return int(major), int(minor)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError(f"cannot read the version of {base_python}: {exc}") from exc


def ensure(base_python: str, dep_specs: Sequence[str], home: Optional[Path] = None) -> Path:
    """Return the engine-site directory, provisioning it when the stamp does not match.

    Idempotent: a stamp matching ``base_python``'s major.minor and ``dep_specs``
    spawns nothing. A drifted or missing stamp rebuilds the directory with one
    ``base_python -m pip install --target``; the stamp is written last. Never
    passes ``--break-system-packages``. Raises RuntimeError on failure.
    """
    want = _minor_of(base_python)
    digest = _specs_digest(dep_specs)
    if _read_stamp(home) == (want, digest):
        return engine_site_dir(home)
    site = engine_site_dir(home)
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [base_python, "-m", "pip", "install", "--target", str(site), *dep_specs],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(site, ignore_errors=True)
        raise RuntimeError(f"engine-site install failed: {exc}") from exc
    if proc.returncode != 0:
        shutil.rmtree(site, ignore_errors=True)
        raise RuntimeError(
            f"engine-site install failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )
    (site / _STAMP_NAME).write_text(f"{want[0]}.{want[1]}\n{digest}\n", encoding="utf-8")
    return site
