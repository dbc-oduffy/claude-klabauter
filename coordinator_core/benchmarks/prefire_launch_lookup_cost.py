"""Prefire launch-lookup cost: does the Bash guard's per-session launch lookup list the temp dir?

Builds a fresh managed temp dir holding N unrelated files plus three launch records for one
session, then times `_em_post_fire.fired` plus `_em_post_fire._engine_run_id_map` (the lookup the
Bash PreToolUse guard runs) and counts `os.listdir`/`os.scandir`/`glob.glob` calls aimed at that
dir. Prints one line: `listdir_of_tmpdir=<n> process_ms=<f>` (n per pair, f the mean process time
per pair over `--repeats` pairs; Windows `process_time` ticks at ~15.6 ms, so one sample cannot
resolve a 10 ms difference).

Usage: python coordinator_core/benchmarks/prefire_launch_lookup_cost.py --unrelated 100000
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SESSION = "sess-prefire-bench"
LAUNCH_RECORDS = 3


def _record_path(tmpdir: str, session_id: str, task_id: str) -> str:
    """The producer's own path builder, so the records land wherever the tree under test writes them
    (flat at the baseline). Never `launch_records` directly: it can exist before the producer uses it."""
    from coordinator_core.hooks import postuse_advisory_dispatch

    return postuse_advisory_dispatch._workflow_run_record_path(tmpdir, session_id, task_id)


def _write_fixture(tmpdir: str, root: str, unrelated: int) -> None:
    import json

    for i in range(unrelated):
        with open(os.path.join(tmpdir, f"unrelated-{i}.tmp"), "w", encoding="utf-8"):
            pass
    os.makedirs(root, exist_ok=True)
    for i in range(LAUNCH_RECORDS):
        path = _record_path(tmpdir, SESSION, f"toolu_{i}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"run_id": f"wf_{i}", "scriptPath": f"{root}/run-{i}.workflow.mjs"}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))) if path is not None else ""


@contextlib.contextmanager
def _count_listings(tmpdir: str, counter: list):
    """Count `os.listdir`/`os.scandir`/`glob.glob` calls whose target is `tmpdir` itself."""
    target = _norm(tmpdir)
    real_listdir, real_scandir, real_glob = os.listdir, os.scandir, glob.glob

    def listdir(path="."):
        if _norm(path) == target:
            counter[0] += 1
        return real_listdir(path)

    def scandir(path="."):
        if _norm(path) == target:
            counter[0] += 1
        return real_scandir(path)

    def glob_glob(pattern, *args, **kwargs):
        head = os.path.dirname(os.fspath(pattern))
        if target in (_norm(head), _norm(glob.escape(tmpdir))):
            counter[0] += 1
        return real_glob(pattern, *args, **kwargs)

    os.listdir, os.scandir, glob.glob = listdir, scandir, glob_glob
    try:
        yield
    finally:
        os.listdir, os.scandir, glob.glob = real_listdir, real_scandir, real_glob


@contextlib.contextmanager
def _managed_tempdir_at(tmpdir: str):
    """Point every `managed_tempdir` binding the lookup reads at `tmpdir`; restore on exit."""
    from pathlib import Path

    from coordinator_core import temp_layout
    from coordinator_core.hooks import flag_em_poll_in_flight

    fake = lambda repo_root=None: Path(tmpdir)  # noqa: E731
    saved = [(temp_layout, "managed_tempdir"), (flag_em_poll_in_flight, "managed_tempdir")]
    originals = [getattr(mod, name) for mod, name in saved]
    for mod, name in saved:
        setattr(mod, name, fake)
    try:
        yield
    finally:
        for (mod, name), original in zip(saved, originals):
            setattr(mod, name, original)


def _reset_caches() -> None:
    """Each real hook fire is a fresh process: drop every in-process cache the lookup keeps."""
    from coordinator_core.hooks import _em_post_fire, flag_em_poll_in_flight

    flag_em_poll_in_flight._LAUNCH_RECORDS.clear()
    _em_post_fire._VERDICTS.clear()
    _em_post_fire._LANDED.clear()


def _pair(root: str):
    from coordinator_core.hooks import _em_post_fire

    _reset_caches()
    return _em_post_fire.fired(SESSION, root), _em_post_fire._engine_run_id_map(SESSION)


def measure(unrelated: int, workdir: str, repeats: int = 50) -> dict:
    """`{"listdir_of_tmpdir": calls per pair, "process_ms": mean per pair, "engine_ids": n}`.

    Fixture creation and the counted pair are outside the timed loop.
    """
    tmpdir = os.path.join(workdir, "managed-temp")
    root = os.path.join(workdir, "repo-root")
    os.makedirs(tmpdir, exist_ok=True)
    _write_fixture(tmpdir, root, unrelated)
    with _managed_tempdir_at(tmpdir):
        counter = [0]
        with _count_listings(tmpdir, counter):
            _, engine_ids = _pair(root)
        start = time.process_time()
        for _ in range(repeats):
            _pair(root)
        elapsed = time.process_time() - start
    return {
        "listdir_of_tmpdir": counter[0],
        "process_ms": elapsed / repeats * 1000.0,
        "engine_ids": len(engine_ids),
    }


def main(argv=None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unrelated", type=int, default=0, help="unrelated files in the temp dir")
    ap.add_argument("--repeats", type=int, default=50)
    ap.add_argument("--workdir", default=None, help="fresh dir for the fixture (default: under <repo>/scratch)")
    args = ap.parse_args(argv)
    owned = args.workdir is None
    if owned:
        from coordinator_core.temp_layout import make_managed_temp_dir

        workdir = str(make_managed_temp_dir(prefix="prefire-lookup-"))
    else:
        workdir = args.workdir
    try:
        result = measure(args.unrelated, workdir, args.repeats)
    finally:
        if owned:
            from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

            rmtree_or_raise(Path(workdir), label="prefire-lookup")
    print(f"listdir_of_tmpdir={result['listdir_of_tmpdir']} process_ms={result['process_ms']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
