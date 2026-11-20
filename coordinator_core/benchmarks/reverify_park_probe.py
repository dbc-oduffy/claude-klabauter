"""Does a halted run park at `clean_overlay_verify.reverify_on_mismatch`, at what process time and spawn
count, and which side of the verdict key moved.

Per `--run-id`, two replays, each over a scratch copy of the run's records so the run dir is never written
(`overlay-inflight` included):

  stored    the on-disk `measured-rev.json` and `measured-touched.json` as the land leg reads them, against the
            run's `overlay-verdict.json`
  rebuilt   `measured_rev.measured_rev` re-run against the repo's current tree (a new base sha, the worktree's
            blobs), its record and touched file redirected into the scratch copy; `moved` is its field-by-field
            diff against the stored record

Each replay is `check_verdict`, then `reverify_on_mismatch` on a `mismatch`, with an injected `spawn` that
counts its calls and launches nothing; `land_deadline.LandDeadlineExceeded` is `parked=true`. One JSON line
per run: `run_id`, `verdict`, `parked`, `process_time_ms` (`time.process_time` over the verdict read and
reverify, never wall clock), `spawns`, `moved`, `detail`, and the `stored_*` counterparts. `verdict`, `parked`,
`process_time_ms`, `spawns` and `detail` describe the rebuilt replay (the landing against today's tree), or
the stored one when the rebuild refused (`rebuild_error` set). `rebuild_process_time_ms` is the cost of the
`measured_rev` call itself. The rebuild writes content-addressed git objects only; `records_unchanged`
asserts the run dir's three record files kept their bytes.

    python coordinator_core/benchmarks/reverify_park_probe.py --repo-root . --run-id <id> [--run-id <id> ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov  # noqa: E402
from coordinator_core.ops.dispatch_emit import land_deadline, measured_rev  # noqa: E402
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path  # noqa: E402

_RECORD_FILES = ("measured-rev.json", "measured-touched.json", cov.VERDICT_FILE, "kept.json", "evicted.json")
_FIELDS_COMPARED = ("base_sha", "sha", "overlay_sha", "tree")


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _seed_scratch(source_dir: Path, scratch_root: Path, run_id: str, files: tuple) -> Path:
    """`scratch_root`'s record dir for `run_id` holding copies of `files` from `source_dir`; returns it."""
    dest = measured_rev_record_path(scratch_root, run_id).parent
    dest.mkdir(parents=True, exist_ok=True)
    for name in files:
        if (source_dir / name).is_file():
            shutil.copyfile(source_dir / name, dest / name)
    return dest


@contextmanager
def _redirected_records(scratch_root: Path) -> Generator[None, None, None]:
    """`measured_rev` writes its record and touched file under `scratch_root`, whatever repo root it is given."""
    real = measured_rev.measured_rev_record_path

    def record(repo_root: Path, run_id: str) -> Path:
        real(repo_root, run_id)  # called for its run-id validation (raises ValueError); the path it returns is discarded
        return measured_rev_record_path(scratch_root, run_id)

    def touched(repo_root: Path, run_id: str) -> Path:
        return record(repo_root, run_id).with_name("measured-touched.json")

    saved = (measured_rev.measured_rev_record_path, measured_rev.touched_record_path)
    setattr(measured_rev, "measured_rev_record_path", record)
    setattr(measured_rev, "touched_record_path", touched)
    try:
        yield
    finally:
        setattr(measured_rev, "measured_rev_record_path", saved[0])
        setattr(measured_rev, "touched_record_path", saved[1])


def replay(scratch_root: Path, run_id: str, measured: dict, git_root: Optional[Path] = None) -> dict:
    """The landing's overlay read over `measured` in `scratch_root`: `check_verdict`, then `reverify_on_mismatch`
    on a mismatch, with a counting no-launch spawn. Returns `verdict`, `parked`, `process_time_ms`, `spawns`,
    `detail`."""
    spawns: list = []

    def spawn(*args, **kwargs) -> bool:
        spawns.append(args)
        return True

    parked = False
    started = time.process_time()
    outcome = cov.check_verdict(scratch_root, run_id, measured, git_root)
    if outcome.verdict == "mismatch":
        try:
            outcome = cov.reverify_on_mismatch(scratch_root, run_id, measured, outcome, spawn=spawn, git_root=git_root)
        except land_deadline.LandDeadlineExceeded:
            parked = True
    elapsed_ms = round((time.process_time() - started) * 1000, 1)
    return {
        "verdict": "mismatch" if parked else outcome.verdict, "parked": parked,
        "process_time_ms": elapsed_ms, "spawns": len(spawns), "detail": outcome.detail,
    }


def moved_fields(stored: dict, fresh: dict) -> list:
    """What differs between two measured-rev records: scalar fields by name, `blobs` / `own_blobs` by path,
    and the declared / removed / peer-hunk sets by the paths that differ."""
    moved: list = []
    for key in _FIELDS_COMPARED:
        if stored.get(key) != fresh.get(key):
            moved.append(f"{key}: {str(stored.get(key))[:12]} -> {str(fresh.get(key))[:12]}")
    for key in ("blobs", "own_blobs"):
        old, new = stored.get(key) or {}, fresh.get(key) or {}
        moved.extend(f"{key}:{p}" for p in sorted(set(old) | set(new)) if old.get(p) != new.get(p))
    for key in ("paths", "removed"):
        old, new = set(stored.get(key) or ()), set(fresh.get(key) or ())
        moved.extend(f"{key}:{p}" for p in sorted(old ^ new))
    old_peers, new_peers = stored.get("peer_hunks") or {}, fresh.get("peer_hunks") or {}
    moved.extend(f"peer_hunks:{p}" for p in sorted(set(old_peers) | set(new_peers)) if old_peers.get(p) != new_peers.get(p))
    return moved


def probe_run(root: Path, run_id: str, rebuild: bool = True) -> dict:
    """The probe row of one run; `ValueError` for an unsafe run id, `OSError` / `ValueError` when its
    `measured-rev.json` is unreadable."""
    run_dir = measured_rev_record_path(root, run_id).parent
    before = {name: _digest(run_dir / name) for name in _RECORD_FILES}
    stored = json.loads((run_dir / "measured-rev.json").read_text(encoding="utf-8"))
    row: dict = {"run_id": run_id}
    with tempfile.TemporaryDirectory(prefix="reverify-park-probe-") as tmp:
        tmp_root = Path(tmp)
        _seed_scratch(run_dir, tmp_root / "stored", run_id, _RECORD_FILES)
        stored_row = replay(tmp_root / "stored", run_id, stored, root)
        verdict_data = _verdict_data(run_dir)
        row["stored_key_matches"] = cov.landing_key(stored) == verdict_data.get("key")
        row["legacy_key_matches"] = cov.legacy_landing_key(stored) == verdict_data.get("key")
        row["verdict_file"] = "recorded components" if "keyed" in verdict_data else "legacy (no components)"
        result = stored_row
        row["rebuild_error"] = None
        row["rebuild_process_time_ms"] = None
        row["rebuilt_key_matches"] = None
        row["moved"] = []
        if rebuild:
            rebuilt_root = tmp_root / "rebuilt"
            _seed_scratch(run_dir, rebuilt_root, run_id, ("measured-touched.json", cov.VERDICT_FILE, "kept.json", "evicted.json"))
            started = time.process_time()
            try:
                with _redirected_records(rebuilt_root):
                    fresh = measured_rev.measured_rev(run_id, list(stored.get("paths") or ()), repo_root=root)
                    row["rebuild_process_time_ms"] = round((time.process_time() - started) * 1000, 1)
                    _seed_scratch(run_dir, rebuilt_root, run_id, ("measured-rev.json",))  # the stored record a legacy verdict is checked against
                    result = replay(rebuilt_root, run_id, fresh, root)
                row["moved"] = moved_fields(stored, fresh)
                row["rebuilt_key_matches"] = cov.landing_key(fresh) == _stored_key(run_dir)
            except (OSError, ValueError) as exc:
                row["rebuild_error"] = f"{type(exc).__name__}: {exc}"
    row.update({k: result[k] for k in ("verdict", "parked", "process_time_ms", "spawns", "detail")})
    row.update({f"stored_{k}": stored_row[k] for k in ("verdict", "parked", "process_time_ms", "spawns")})
    row["records_unchanged"] = before == {name: _digest(run_dir / name) for name in _RECORD_FILES}
    return row


def _verdict_data(run_dir: Path) -> dict:
    try:
        data = json.loads((run_dir / cov.VERDICT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stored_key(run_dir: Path) -> Optional[str]:
    return _verdict_data(run_dir).get("key")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="reverify_park_probe")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--run-id", action="append", required=True, dest="run_ids")
    parser.add_argument("--no-rebuild", action="store_true", help="replay the stored record only; skip the measured_rev rebuild")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    status = 0
    for run_id in args.run_ids:
        try:
            row = probe_run(root, run_id, rebuild=not args.no_rebuild)
        except (OSError, ValueError) as exc:
            row = {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
            status = 1
        print(json.dumps(row, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
