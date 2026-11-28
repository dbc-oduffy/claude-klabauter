"""Per-writer census of every ``<temp>/coordinator`` root: entry count, oldest age, dead-owner count.

Reads two levels per root (repo dirs, then each registered writer dir), never recurses.

Run: `python coordinator_core/benchmarks/temp_coordinator_root_census.py [--json]`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core import temp_layout  # noqa: E402

_PID_PREFIX_RE = re.compile(r"^(\d+)-")


def _entry_pid(name: str) -> int | None:
    m = _PID_PREFIX_RE.match(name)
    return int(m.group(1)) if m else None


def _scan(directory: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(directory) as it:
            return list(it)
    except OSError:
        return []


def _entry_age(entry: os.DirEntry, now: float) -> float | None:
    try:
        return now - entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return None


def _writer_row(
    writer: temp_layout.RootWriter, owner_dir: Path, now: float, alive: Callable[[int], bool]
) -> dict:
    count = dead = over_age = 0
    oldest: float | None = None
    for entry in _scan(writer.directory(owner_dir)):
        if not writer.matches(entry.name):
            continue
        count += 1
        age = _entry_age(entry, now)
        if age is not None:
            oldest = age if oldest is None else max(oldest, age)
            if age >= writer.max_age_s:
                over_age += 1
        pid = _entry_pid(entry.name) if writer.owner == temp_layout.OWNER_SIGNAL_PID else None
        if pid is not None and pid != os.getpid() and not alive(pid):
            if not os.path.exists(os.path.join(entry.path, temp_layout.PYTEST_KEPT_MARKER)):
                dead += 1
    return {
        "writer": writer.name,
        "owner_dir": str(owner_dir),
        "entries": count,
        "oldest_age_s": None if oldest is None else round(oldest),
        "dead_owner": dead,
        "past_max_age": over_age,
        "keep_latest": writer.keep_latest,
        "over_keep_latest": max(0, count - writer.keep_latest) if writer.keep_latest else 0,
    }


def _unregistered(owner_dir: Path, scope: str) -> list[str]:
    writers = [w for w in temp_layout.COORDINATOR_ROOT_WRITERS if w.scope == scope]
    heads = {w.container[0] for w in writers if w.container}
    direct = [w for w in writers if not w.container]
    return sorted(
        e.name
        for e in _scan(owner_dir)
        if e.name not in heads and not any(w.matches(e.name) for w in direct)
    )


def census(
    roots: Sequence[Path] | None = None,
    *,
    now: float | None = None,
    alive: Callable[[int], bool] | None = None,
) -> dict:
    """Per-root, per-owner-dir, per-writer rows; ``roots`` defaults to ``temp_layout.pytest_sweep_roots()``."""
    if alive is None:
        from coordinator_core.group_em import session_registry

        alive = session_registry.pid_alive
    now = time.time() if now is None else now
    report: dict = {"roots": []}
    for root in temp_layout.pytest_sweep_roots() if roots is None else roots:
        owners = []
        for repo in _scan(root):
            if not repo.is_dir(follow_symlinks=False):
                continue
            scope = temp_layout.SCOPE_FLEET if repo.name == temp_layout.FLEET_DIRNAME else temp_layout.SCOPE_REPO
            owner_dir = Path(repo.path)
            rows = [
                _writer_row(w, owner_dir, now, alive)
                for w in temp_layout.COORDINATOR_ROOT_WRITERS
                if w.scope == scope
            ]
            owners.append(
                {
                    "owner_dir": repo.name,
                    "writers": [r for r in rows if r["entries"]],
                    "unregistered": _unregistered(owner_dir, scope),
                }
            )
        report["roots"].append({"root": str(root), "owners": owners})
    report["totals"] = _totals(report)
    return report


def _totals(report: dict) -> dict:
    totals: dict = {}
    for root in report["roots"]:
        for owner in root["owners"]:
            for row in owner["writers"]:
                t = totals.setdefault(row["writer"], {"entries": 0, "dead_owner": 0, "past_max_age": 0, "oldest_age_s": 0})
                t["entries"] += row["entries"]
                t["dead_owner"] += row["dead_owner"]
                t["past_max_age"] += row["past_max_age"]
                t["oldest_age_s"] = max(t["oldest_age_s"], row["oldest_age_s"] or 0)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    report = census()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for name, t in sorted(report["totals"].items()):
        print(
            f"{name}: entries={t['entries']} dead_owner={t['dead_owner']} "
            f"past_max_age={t['past_max_age']} oldest_age_s={t['oldest_age_s']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
