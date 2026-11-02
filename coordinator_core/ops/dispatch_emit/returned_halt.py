"""Engine-side `halted.json` for a finished ask run whose script returned `halted_by` unrecorded."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def land_returned_halt(record_dir: Path, result: dict, *, launched_at: float) -> Optional[Path]:
    """Write `halted.json` via `halted_run.write_halt_marker` when `result` carries a `halted_by` dict,
    `halt_commit` is not a committed reply, and the record dir's `halted.json` is absent or older than
    `launched_at`; return the marker path, else None. Never raises."""
    raise NotImplementedError
