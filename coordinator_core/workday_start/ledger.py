"""Run ledger: one JSONL file per run under the git common dir, one terminal row per step.

Ceremony-neutral: the ceremony name arrives as an argument.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coordinator_core.atomic_append import append_line
from coordinator_core.session.core import sessions_dir
from .contract import StepResult

LEDGER_FILE = "ledger.jsonl"


def run_dir(repo_root: "Path | str", ceremony: str, run_id: str) -> Path:
    """`<git-common-dir>/coordinator-sessions/<ceremony>-runs/<run_id>`; raises outside a git repo."""
    base = sessions_dir(str(repo_root))
    if not base:
        raise RuntimeError(f"ledger: {repo_root} is not inside a git repository")
    return Path(base) / f"{ceremony}-runs" / run_id


def append(run_dir_path: Path, result: StepResult) -> None:
    """Atomically append one row; creates the run directory on first use."""
    run_dir_path.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result.to_row(), ensure_ascii=False, separators=(",", ":")) + "\n"
    append_line(run_dir_path / LEDGER_FILE, line.encode("utf-8"))


def read(run_dir_path: Path) -> list[dict[str, Any]]:
    """Every row in append order; an absent ledger is empty."""
    path = run_dir_path / LEDGER_FILE
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
