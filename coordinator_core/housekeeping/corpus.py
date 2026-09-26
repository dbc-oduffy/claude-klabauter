
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple, Union

from coordinator_core.housekeeping.head_scan import scan_keys

LIVE_CORPUS_KEYS: Tuple[str, ...] = (
    "handoff_id",
    "stub_id",
    "deployment_state",
    "blocked_by",
    "claimed_by",
    "consumed_by",
)

#: table. RESTATED from the table's 20 ms with the measurement that refutes
#: CONSEQUENCE, recorded rather than absorbed: the plan's budget table sums
LEG_BUDGET_MS = 50.0

PathLike = Union[str, Path]


def _scan_error_message(context: PathLike, exc: OSError) -> str:
    return f"{context}: {exc}"


def list_live_handoffs(live_dir: PathLike) -> Tuple[List[Path], List[str]]:
    paths: List[Path] = []
    gaps: List[str] = []
    try:
        it = os.scandir(live_dir)
    except OSError as exc:
        gaps.append(_scan_error_message(live_dir, exc))
        return paths, gaps

    try:
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:
                    gaps.append(_scan_error_message(live_dir, exc))
                    break
                try:
                    if entry.is_file() and entry.name.endswith(".md"):
                        paths.append(Path(entry.path))
                except OSError as exc:
                    gaps.append(_scan_error_message(entry.path, exc))
    except OSError as exc:
        gaps.append(_scan_error_message(live_dir, exc))

    return paths, gaps


def list_archived_handoffs(archive_dir: PathLike) -> Tuple[List[Path], List[str]]:
    paths: List[Path] = []
    gaps: List[str] = []

    def _onerror(exc: OSError) -> None:
        gaps.append(_scan_error_message(getattr(exc, "filename", archive_dir), exc))

    for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=_onerror):
        for name in filenames:
            if name.endswith(".md"):
                paths.append(Path(dirpath) / name)

    return paths, gaps


@dataclass
class LiveCorpusResult:

    records: Dict[Path, Dict[str, Any]] = field(default_factory=dict)
    scan_gaps: List[str] = field(default_factory=list)
    read_count: int = 0
    process_time_ms: float = 0.0


def read_live_corpus(
    live_dir: PathLike,
    keys: Iterable[str] = LIVE_CORPUS_KEYS,
    *,
    reader: Callable[[PathLike, Iterable[str]], Dict[str, Any]] = scan_keys,
) -> LiveCorpusResult:
    start = time.process_time()
    paths, gaps = list_live_handoffs(live_dir)

    records: Dict[Path, Dict[str, Any]] = {}
    read_count = 0
    key_tuple = tuple(keys)
    for path in paths:
        records[path] = reader(path, key_tuple)
        read_count += 1

    elapsed_ms = (time.process_time() - start) * 1000.0

    return LiveCorpusResult(
        records=records,
        scan_gaps=gaps,
        read_count=read_count,
        process_time_ms=elapsed_ms,
    )
