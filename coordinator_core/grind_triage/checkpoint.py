"""Durable 25/50/75% progress reports for a grinder run.

A report is a line appended to `<run_dir>/progress.jsonl`; it never wakes a session mid-run.
The file is the sole dedupe state, so a resumed run re-emits nothing it already reported.

Trap: no engine code calls this module yet. The composed script's `_checkpoint`
(`grind_stages.compose_checkpoint_js`) logs in-process and never writes `progress.jsonl`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_append import append_line
from coordinator_core.grind_triage.contract import CHECKPOINT_PERCENTS, CheckpointEvent

PROGRESS_FILE = "progress.jsonl"


def reported_percents(run_dir: Path) -> set[int]:
    """Percents already written to the run's progress file; unreadable lines are skipped."""
    path = Path(run_dir) / PROGRESS_FILE
    if not path.is_file():
        return set()
    seen: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            percent = json.loads(line).get("percent")
        except (ValueError, AttributeError):
            continue
        if isinstance(percent, int):
            seen.add(percent)
    return seen


def due_events(
    settled: int, total: int, reported: set[int], run_id: Optional[str] = None
) -> list[CheckpointEvent]:
    """Events for every threshold `settled` of `total` has crossed that is not in `reported`.

    A jump past several thresholds yields one event per threshold, ascending. An empty run
    (`total` <= 0) yields none.
    """
    if total <= 0:
        return []
    return [
        CheckpointEvent(percent=p, settled=settled, total=total, run_id=run_id)
        for p in CHECKPOINT_PERCENTS
        if p not in reported and settled * 100 >= p * total
    ]


def emit_checkpoints(
    run_dir: Path, settled: int, total: int, run_id: Optional[str] = None
) -> list[CheckpointEvent]:
    """Append each newly crossed threshold to `<run_dir>/progress.jsonl`; return the events written.

    Idempotent: a threshold already in the file is never written twice. `run_dir` must exist.
    """
    run_dir = Path(run_dir)
    events = due_events(settled, total, reported_percents(run_dir), run_id)
    sink = run_dir / PROGRESS_FILE
    for event in events:
        line = json.dumps({"kind": "grind_checkpoint", **asdict(event)}, sort_keys=True) + "\n"
        append_line(sink, line.encode("utf-8"))
    return events
