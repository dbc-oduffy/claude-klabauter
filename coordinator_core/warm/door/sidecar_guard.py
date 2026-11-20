"""Box-wide door sidecar guard: `<settings-home>/bin/door.engine-root.txt` may only name the
registered installed engine (`repos.claude_klabauter`).

Pure stdlib and file reads only -- no spawn -- so the session-start path can import it.
Scope is the box-wide sidecar alone: a scratch build's own door directory is unguarded, and
a scratch build steers its own processes with COORDINATOR_DOOR_ENGINE_ROOT, never this file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

OVERRIDE_ENV = "COORDINATOR_ALLOW_BOX_SIDECAR_RETARGET"
SIDECAR_NAME = "door.engine-root.txt"
GUARD_LOG_NAME = "door-sidecar-guard.jsonl"
_REGISTRY_FILES = ("registry.local.toml", "registry.toml")  # first hit wins: local overrides base


class DoorSidecarRefused(RuntimeError):
    """A write of the box-wide sidecar was refused; the sidecar is untouched."""


def _norm(p: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(str(p))))


def same_root(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def is_scratch_path(p: str | os.PathLike) -> bool:
    return "scratch" in {part.lower() for part in Path(os.path.abspath(str(p))).parts}


def registered_engine_root(home: Path) -> Optional[str]:
    """`repos.claude_klabauter` from the machine-local registry files, or None when unset."""
    import tomllib

    for name in _REGISTRY_FILES:
        try:
            data = tomllib.loads((home / "machine-local" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get("repos.claude_klabauter")
        repos = data.get("repos")
        if not value and isinstance(repos, dict):
            value = repos.get("claude_klabauter")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_box_bin(sidecar_dir: Path, home: Path) -> bool:
    return _norm(sidecar_dir) == _norm(home / "bin")


def _log(home: Path, row: dict) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pid": os.getpid(), **row}
    try:
        with open(home / GUARD_LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def check_box_sidecar_write(sidecar_dir: Path, engine_root: Path) -> None:
    """Raise DoorSidecarRefused when `sidecar_dir` is the box bin and `engine_root` is under a
    `scratch/` directory or is not the registered installed engine. An unset registry refuses
    scratch roots only, so a first install can still register itself."""
    try:
        from coordinator_core._settings_home import settings_home

        home = settings_home()
    except Exception:
        return
    if not _is_box_bin(Path(sidecar_dir), home):
        return
    registered = registered_engine_root(home)
    target = str(Path(engine_root))
    scratch = is_scratch_path(target)
    mismatch = registered is not None and not same_root(target, registered)
    if not (scratch or mismatch):
        return
    if os.environ.get(OVERRIDE_ENV) == "1":
        _log(home, {"event": "override", "target": _norm_display(target), "registered": registered})
        return
    _log(home, {"event": "refused", "target": _norm_display(target), "registered": registered})
    raise DoorSidecarRefused(
        f"box door sidecar write refused: {target} is "
        f"{'under scratch/' if scratch else 'not the registered engine'} "
        f"(registered: {registered}). Steer a scratch build with COORDINATOR_DOOR_ENGINE_ROOT; "
        f"deliberate retarget: {OVERRIDE_ENV}=1 (logged to {home / GUARD_LOG_NAME})."
    )


def _norm_display(p: str) -> str:
    return str(Path(p).resolve())


def _iso(mtime: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))


def sidecar_drift(home: Path) -> Optional[dict]:
    """The box sidecar's root and the registered engine, when they disagree; else None.

    Reads three small files and never spawns, so it is safe on the session-start path. An
    absent sidecar or unset registry is not drift: nothing is mis-routed."""
    path = home / "bin" / SIDECAR_NAME
    try:
        raw = path.read_text(encoding="utf-8").strip()
        mtime = path.stat().st_mtime
    except OSError:
        return None
    registered = registered_engine_root(home)
    if not raw or registered is None or same_root(raw, registered):
        return None
    return {"sidecar": raw, "registered": registered, "sidecar_mtime": _iso(mtime)}


def warn_if_resolved_via_drifted_sidecar(engine_root: str | os.PathLike, stream) -> bool:
    """Loud, durable warning when `engine_root` is what a drifted box sidecar named. Never raises.

    Quiet when COORDINATOR_DOOR_ENGINE_ROOT is set: the door's first rung steered this engine,
    not the sidecar."""
    try:
        if os.environ.get("COORDINATOR_DOOR_ENGINE_ROOT"):
            return False
        from coordinator_core._settings_home import settings_home

        home = settings_home()
        drift = sidecar_drift(home)
        if drift is None or not same_root(str(engine_root), drift["sidecar"]):
            return False
        print(
            f"[door-sidecar] WARNING: this engine ({engine_root}) was reached through the box "
            f"sidecar {home / 'bin' / SIDECAR_NAME} (mtime {drift['sidecar_mtime']}), which names "
            f"{drift['sidecar']}, not the registered installed engine {drift['registered']}. "
            f"Repoint the sidecar at the registered engine.",
            file=stream,
        )
        _log(home, {"event": "read-drift", "engine_root": str(engine_root), **drift})
        return True
    except Exception:
        return False
