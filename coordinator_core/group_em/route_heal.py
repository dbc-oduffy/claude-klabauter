"""Maps a comms-route break reason to an in-engine heal or a runnable fix."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
HEAL_TIMEOUT_S = 10.0


def heal_agy_leg(conversation: str, cwd: Optional[str] = None) -> bool:
    """Re-register an Agy conversation's leg through its hook-launch script; True on exit 0."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            [sys.executable, str(_BIN / "unimatrix-agy-hook-launch.py"), "--conversation", conversation],
            cwd=cwd, capture_output=True, timeout=HEAL_TIMEOUT_S, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


_AGY_FIX = "python coordinator/bin/unimatrix-agy-hook-launch.py --conversation <conversation-id>"

_TABLE: dict = {
    "no_binding": (None, "python coordinator/bin/unimatrix-roster.py"),
    "no_leg_listening": (heal_agy_leg, _AGY_FIX),
    "far_unreachable": (None, "python coordinator/bin/unimatrix-doctor.py"),
    "no_receipt": (None, "python coordinator/bin/unimatrix-inbox.py"),
}

_ALIASES = {
    "box_unreachable": "far_unreachable",
    "no leg endpoint": "no_leg_listening",
    "not listening": "no_leg_listening",
}


def table_reasons() -> frozenset:
    """The reasons `classify` resolves directly, before aliases."""
    return frozenset(_TABLE)


def classify(reason: str) -> dict:
    """`{heal: callable|None, fix: str}` for a break reason; an unknown reason gets the doctor fix."""
    text = (reason or "").strip()
    key = text.split(":", 1)[0]
    key = _ALIASES.get(key, key)
    if key not in _TABLE:
        lowered = text.lower()
        key = next((v for k, v in _ALIASES.items() if k in lowered), key)
    heal, fix = _TABLE.get(key, (None, "python coordinator/bin/unimatrix-doctor.py"))
    return {"heal": heal, "fix": fix}


def fix_paths() -> list:
    """Repo-relative script paths every fix command names."""
    out = {tok for _h, fix in _TABLE.values() for tok in fix.split() if tok.endswith(".py")}
    return sorted(out)
