"""Cross-repo sweep of dead-pid pytest run dirs under ``<gettempdir()>/coordinator/*/pytest/``.

Negative spec: never deletes a run dir owned by a live pid or this process, one carrying
``PYTEST_KEPT_MARKER``, one whose name does not parse, anything under ``_fleet``, or a link.
No subprocess is spawned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.group_em import session_registry
from coordinator_core.ops.fleet.scratch_hygiene_apply import remove_tree_no_follow
from coordinator_core.ops.fleet.scratch_hygiene_plan import _Context, _is_link, classify_entry
from coordinator_core.temp_layout import FLEET_DIRNAME, PYTEST_KEPT_MARKER, parse_pytest_run_dir

_PYTEST_DIRNAME = "pytest"


def _children(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _candidates() -> list[tuple[Path, Path, int]]:
    """``(repo temp dir, run dir, owning pid)`` for every unmarked, parseable, non-link run dir."""
    root = Path(tempfile.gettempdir()) / "coordinator"
    found: list[tuple[Path, Path, int]] = []
    for repo_name in _children(root):
        repo_dir = root / repo_name
        if repo_name == FLEET_DIRNAME or _is_link(repo_dir):
            continue
        pytest_dir = repo_dir / _PYTEST_DIRNAME
        if _is_link(pytest_dir) or not pytest_dir.is_dir():
            continue
        for run_name in _children(pytest_dir):
            pid = parse_pytest_run_dir(run_name)
            run_dir = pytest_dir / run_name
            if pid is None or _is_link(run_dir) or not run_dir.is_dir():
                continue
            if (run_dir / PYTEST_KEPT_MARKER).exists():
                continue
            found.append((repo_dir, run_dir, pid))
    return found


def sweep_dead_runs(
    *,
    apply: bool = True,
    quiescence_hours: float = 1.0,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Purge records for dead-pid run dirs; each ``would-delete`` is removed when ``apply`` is set."""
    now = time.time() if now is None else now
    ctx = _Context(registry_dir=registry_dir)
    own_pid = os.getpid()
    records: list[dict[str, Any]] = []
    for repo_dir, run_dir, pid in _candidates():
        if pid == own_pid or session_registry.pid_alive(pid):
            continue
        rec = classify_entry(
            run_dir,
            repo_root=Path(repo_dir.name),
            quiescence_hours=quiescence_hours,
            now=now,
            _ctx=ctx,
        )
        if apply and rec["action"] == "would-delete":
            try:
                remove_tree_no_follow(run_dir)
                rec["action"] = "deleted"
            except OSError as exc:
                rec["action"] = "delete-failed"
                rec["reason"] = type(exc).__name__
        records.append(rec)
    return records


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pytest_basetemp_sweep")
    parser.add_argument("--dry-run", action="store_true", help="classify only; delete nothing")
    args = parser.parse_args(argv)
    for rec in sweep_dead_runs(apply=not args.dry_run):
        sys.stdout.write(json.dumps(rec) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
