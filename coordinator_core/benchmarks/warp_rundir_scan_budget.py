"""Falsifier for the per-run-dir scans the warm engine's land, gate and review legs run over `state/warp`.

Fixture: N run dirs shaped `state/warp/<run_id>/{manifest.json, kept.json}` plus
`scratch/warp/<run_id>/commit-request.txt`, a halted-withheld index entry per run, and one small
tracked-looking file per run that its `kept.json` fingerprints. Each axis is driven in-process, twice
(cold: process-wide caches cleared; warm: the same call again, nothing changed):

  record_dir              `run_dir_stat_cache.record_dir` (the memoized `ask_contract.record_dir`) per run: the path resolution every scan below pays per
                          run; the kept_fingerprint and manifest_declared axes take it from a precomputed
                          map so each reports only the leg its cache bounds
  halted_withheld_index   `halted_withheld_index.entries`
  kept_fingerprint        the `kept_work.kept_fingerprints` + `kept_work.fingerprint` loop of
                          `sibling_claims._own_dirt` / `land_run_halt._peer_only_after_checkpoint`
  manifest_declared       `warp_run_deliverable._manifest_declared` per run (the leg under
                          `review_prep._without_foreign_claimed -> _sibling_declared -> _peer_view`),
                          outside a release scope
  ack_status              `dispatch_ack.AckStore.status` per admitted key

Each pass reports `process_ms` (`time.process_time`, never wall clock) and `read_bytes_calls` (every
`Path.read_bytes` and `call_io_scope.read_bytes` under the run roots; `read_text_calls` counts
`Path.read_text` likewise). Warm passes that scale with the run-dir count are the defect.

Run: `python coordinator_core/benchmarks/warp_rundir_scan_budget.py --run-dirs 5000`; prints one JSON line.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator, Optional
from unittest import mock

DEFAULT_RUN_DIRS = 5000
AXES = ("record_dir", "halted_withheld_index", "kept_fingerprint", "manifest_declared", "ack_status")
PASSES = ("cold", "warm")
BAR_MS = 500.0


def _run_id(i: int) -> str:
    return f"ask-20261118T000000-{i:06x}"


def build_fixture(root: Path, run_dirs: int) -> list:
    """Synthesize `run_dirs` runs under `root`; the run ids, sorted."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.commit_request import MARKER_PREFIX

    idx._STAT_CACHE.clear()
    ids = []
    for i in range(run_dirs):
        run_id = _run_id(i)
        ids.append(run_id)
        rel = f"src/bench/file_{i}.txt"
        data = f"payload {i}\n".encode() * 64
        (root / "src" / "bench").mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        record = root / "state" / "warp" / run_id
        record.mkdir(parents=True, exist_ok=True)
        (record / "manifest.json").write_text(
            json.dumps({"rows": [{"id": "C1", "writes": [rel, f"src/bench/test_{i}.py"]}], "doc_writes": [f"docs/{i}.md"]}),
            encoding="utf-8",
        )
        (record / kept_work.KEPT_FILE).write_text(
            json.dumps({"chunks": {"C1": {"paths": {rel: kept_work.blob_sha(data)}}}, "plan_gate": None}),
            encoding="utf-8",
        )
        scratch = root / "scratch" / "warp" / run_id
        scratch.mkdir(parents=True, exist_ok=True)
        marker = {
            "version": 1,
            "chunks": [{"id": "C1", "title": "bench", "paths": [rel], "prefixes": [f"src/bench{i % 7}"], "report": ""}],
            "deliverable_id": None, "session_id": "bench", "repo_root": None, "plan_path": None,
        }
        (scratch / "commit-request.txt").write_text(MARKER_PREFIX + json.dumps(marker) + "\n", encoding="utf-8")
        idx.add_run(
            root, run_id, [f"halted/other_{i}.txt"], sizing_rel=f"state/sizings/halted-{i}.yaml",
            prints={f"halted/other_{i}.txt": f"blob{i}"}, declared=[rel],
        )
    idx.atomic_write_bytes(idx._dir(root) / idx.SENTINEL, b"")
    return ids


class _ReadCounter:
    """Counts reads of files under the run roots; `calls` and `text_calls` are the running totals."""

    def __init__(self, prefixes: tuple) -> None:
        self._prefixes = prefixes
        self.calls = 0
        self.text_calls = 0

    def _under(self, path: "str | os.PathLike") -> bool:
        return os.fspath(path).replace("\\", "/").startswith(self._prefixes)


@contextlib.contextmanager
def counting_reads(root: Path) -> Generator[_ReadCounter]:
    """Wrap `Path.read_bytes`, `Path.read_text` and `call_io_scope.read_bytes` for reads anywhere under `root`
    (the fixture is the run root: run records, scratch dirs, index and the files `kept.json` fingerprints)."""
    from coordinator_core.git import call_io_scope

    base = os.fspath(root).replace("\\", "/").rstrip("/")
    counter = _ReadCounter((base + "/",))
    real_bytes, real_text, real_scoped = Path.read_bytes, Path.read_text, call_io_scope.read_bytes

    def read_bytes(self) -> bytes:
        counter.calls += counter._under(self)
        return real_bytes(self)

    def read_text(self, *args: Any, **kwargs: Any) -> str:
        counter.text_calls += counter._under(self)
        return real_text(self, *args, **kwargs)

    def scoped(path: "str | os.PathLike") -> Optional[bytes]:
        counter.calls += counter._under(path)
        return real_scoped(path)

    with mock.patch.object(Path, "read_bytes", read_bytes), mock.patch.object(Path, "read_text", read_text), \
            mock.patch.object(call_io_scope, "read_bytes", scoped):
        yield counter


def _axis_legs(root: Path, ids: list) -> dict:
    """`{axis: (reset, run)}`; `reset` drops the axis's process-wide caches before the cold pass."""
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.run_dir_stat_cache import clear as clear_stat_cache, record_dir
    from coordinator_core.warm import dispatch_ack

    store = dispatch_ack.AckStore(
        capacity=len(ids) + 16, executing_bound=lambda method: 600.0, journal_dir=root / "ack-journal"
    )
    keys = [dispatch_ack.mint_key() for _ in ids]
    for key in keys:
        store.admit(key, "land")

    dirs = {run_id: record_dir(root, run_id) for run_id in ids}

    def record_dirs() -> int:
        return sum(record_dir(root, run_id) is not None for run_id in ids)

    def kept_fingerprint() -> int:
        matched = 0
        for run_id in ids:
            kept = kept_work.kept_fingerprints(dirs[run_id])
            for rel, blobs in kept.items():
                matched += kept_work.fingerprint(root, rel) in blobs
        return matched

    def manifest_declared() -> int:
        with mock.patch.object(wrd, "record_dir", lambda _root, run_id: dirs[run_id]):
            return sum(len(wrd._manifest_declared(run_id, root)[0]) for run_id in ids)

    def ack_status() -> int:
        return sum(store.status(key).get("state") == dispatch_ack.STATE_EXECUTING for key in keys)

    return {
        "record_dir": (clear_stat_cache, record_dirs),
        "halted_withheld_index": (idx._STAT_CACHE.clear, lambda: len(idx.entries(root))),
        "kept_fingerprint": (lambda: None, kept_fingerprint),
        "manifest_declared": (lambda: None, manifest_declared),
        "ack_status": (lambda: None, ack_status),
    }


def _timed(root: Path, fn: Callable[[], int]) -> dict:
    with counting_reads(root) as counter:
        start = time.process_time()
        result = fn()
        ms = round((time.process_time() - start) * 1000.0, 1)
    return {"process_ms": ms, "read_bytes_calls": counter.calls, "read_text_calls": counter.text_calls, "result": result}


def run_benchmark(base: Optional[Path] = None, run_dirs: int = DEFAULT_RUN_DIRS) -> dict:
    """`{"run_dirs": N, "axes": {axis: {"cold": row, "warm": row}}}` under `base` (a managed temp dir when None);
    a row is `{process_ms, read_bytes_calls, read_text_calls, result}`."""
    from coordinator_core.temp_layout import make_managed_temp_dir

    owned = base is None
    base = make_managed_temp_dir(prefix="warp-rundir-scan-") if owned else Path(base)
    try:
        ids = build_fixture(base, run_dirs)
        axes: dict = {}
        for name, (reset, run) in _axis_legs(base, ids).items():
            reset()
            axes[name] = {"cold": _timed(base, run)}
            axes[name]["warm"] = _timed(base, run)
        return {"run_dirs": run_dirs, "axes": axes}
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)


def over_bar(report: dict) -> list:
    """Axes whose warm pass is over `BAR_MS` process time or reads (bytes or text) at least one file per run dir."""
    n = report["run_dirs"]
    return [
        name for name, passes in report["axes"].items()
        if passes["warm"]["process_ms"] >= BAR_MS or passes["warm"]["read_bytes_calls"] + passes["warm"]["read_text_calls"] >= n
    ]


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dirs", type=int, default=DEFAULT_RUN_DIRS)
    args = parser.parse_args(argv)
    report = run_benchmark(run_dirs=args.run_dirs)
    report["warm_over_bar"] = over_bar(report)
    print(json.dumps(report, separators=(",", ":")))
    return 1 if report["warm_over_bar"] else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.exit(main())
