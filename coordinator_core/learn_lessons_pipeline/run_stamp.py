"""coordinator_core.learn_lessons_pipeline.run_stamp — the write half.

Purpose: today the `COMPLETE` sentinel that `learn_lessons_cutoff.derive_cutoff`
scans for is written by a human following SKILL.md Phase 8. Once the engine
drives the run itself, an unwritten sentinel means the cutoff never advances
and every subsequent sweep re-derives the same stale date — silently. This
module is the only new state this plan introduces.

`stamp_run_complete()` creates `<runs_dir>/learn-lessons-<run_date>/` if
absent and writes an empty `COMPLETE` file, idempotently — a second call on
the same date adds nothing and mutates nothing, matching the
content-addressed-spool idempotency posture in
`workstream_complete/directives_lessons_plan.py :: _spool_body_to_file`.

`run_date` defaults to today's local date in YYYY-MM-DD and is validated
against the same `^\\d{4}-\\d{2}-\\d{2}$` shape `central_run_due._CUTOFF_RE`
enforces — a malformed date refuses rather than creating a dir
`learn_lessons_cutoff.derive_cutoff` will silently never match.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C2
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

_RUN_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def stamp_run_complete(runs_dir: Path, run_date: Optional[str] = None) -> Path:
    """Create `<runs_dir>/learn-lessons-<run_date>/COMPLETE`, idempotently.

    Raises `ValueError` for a `run_date` that does not match
    `^\\d{4}-\\d{2}-\\d{2}$` — never creates a directory
    `learn_lessons_cutoff.derive_cutoff` would silently never match.

    Returns the path to the (created-or-already-present) `COMPLETE` file.
    """
    if run_date is None:
        run_date = date.today().isoformat()
    if not _RUN_DATE_RE.match(run_date):
        raise ValueError(f"run_date must match YYYY-MM-DD, got {run_date!r}")

    run_dir = runs_dir / f"learn-lessons-{run_date}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sentinel = run_dir / "COMPLETE"
    if not sentinel.exists():
        sentinel.write_bytes(b"")
    return sentinel
