"""Post-land re-run of the importer tripwires a test verdict recorded NOT RUN: one detached `test_leg_run` at the landed sha."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import TRIPWIRES_NOT_RUN_KEY

RERUN_RECORD = "tripwire-rerun.json"
_SCRIPT = "coordinator_core/ops/test_leg_run.py"


def launch(root: Path, run_dir: Optional[Path], landed_sha: str, verdict: Optional[Mapping]) -> Optional[list]:
    """Start one detached `test_leg_run` over exactly `verdict[tripwires_not_run]` at `landed_sha`, writing
    `<run_dir>/tripwire-rerun.json`; returns its argv, or None when nothing was launched (no tripwires, no
    run dir or sha, spawn refused). `--touched` routes the leg through the shared-clone mirror at the sha.
    Never awaited and never raises: the land has already happened."""
    raw = verdict.get(TRIPWIRES_NOT_RUN_KEY) if isinstance(verdict, Mapping) else None
    files = list(dict.fromkeys(f for f in raw if isinstance(f, str) and f)) if isinstance(raw, (list, tuple)) else []
    if not files or run_dir is None or not landed_sha:
        return None
    args = ["--head-sha", landed_sha]
    for f in files:
        args += ["--touched", f]
    args += ["--out", str(Path(run_dir) / RERUN_RECORD), "--", *files]
    try:
        started = spawn_detached(str(root), _SCRIPT, args)
    except Exception:  # noqa: BLE001 -- the landed commit stands; a lost re-run is not a land failure
        return None
    return args if started else None
