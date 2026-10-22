"""Run driver for fleet.migrate_prose_queue; stub until the real driver lands."""

from __future__ import annotations

from pathlib import Path
from typing import List


def run_migration(repo_root: Path, families: List[str], dry_run: bool) -> dict:
    raise NotImplementedError("run_migration is implemented by the run-driver row")
