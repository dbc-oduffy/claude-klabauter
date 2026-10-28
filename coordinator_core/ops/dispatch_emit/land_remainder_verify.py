"""``verify_remainder`` of ``dispatch.land_verified_remainder``: import closure of the on-disk bytes
inline, and their test retest in the isolated landing export from a detached child.

Seam: the child is this module run as a script through ``ceremony.detached_spawn.spawn_detached``
(the spawn ``retest_targeted._start_detached`` uses) and runs tests through ``retest_targeted._run_pass``.
The verdict is ``<root>/scratch/warp/<run_id>/land-remainder-verdict.json``, keyed by the sha256
fingerprint of the requested paths' bytes: a verdict for other bytes is no verdict.

Traps: ``build_landing_export`` consumes the run's base export (``scratch/warp/<run_id>/export.base``), so
a second retest of changed bytes needs the base rebuilt; the inline path never builds one and refuses
``no-base-export``. The inline path spawns nothing but the child and reads only the verdict file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import land_remainder_contract as contract
from coordinator_core.ops.dispatch_emit import landing_retest

VERDICT_NAME = "land-remainder-verdict.json"
BASE_EXPORT_NAME = "export.base"
_RUNNING = "running"
_GRACE_SECS = 60.0


def verdict_path(root: Path, run_id: str) -> Path:
    if not safe_id(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    return Path(root) / "scratch" / "warp" / run_id / VERDICT_NAME


def _norm(paths: Sequence[str]) -> list[str]:
    return sorted({str(p).replace("\\", "/") for p in paths})


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _read_verdict(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_verdict(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _past_deadline(record: dict) -> bool:
    try:
        return datetime.fromisoformat(record["deadline"]) < datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True


def _closure_findings(root: Path, paths: Sequence[str]) -> list[str]:
    from coordinator_core.ops.dispatch_emit import landing_integrity

    present = [p for p in paths if (root / p).is_file()]
    gone = [p for p in paths if p not in set(present)]
    found = landing_integrity.import_closure_findings(root, [p for p in present if p.endswith(".py")], gone)
    return [f"{f.path}: {f.detail}" if f.path else f.detail for f in found]


def _spawn_child(root: Path, run_id: str, paths: Sequence[str], spawn: Callable[..., bool]) -> bool:
    engine = Path(__file__).resolve().parents[3]
    argv = ["--child", "--root", str(root), "--run-id", run_id, *[x for p in paths for x in ("--path", p)]]
    return bool(spawn(str(engine), str(Path(__file__).resolve()), argv, site_free=True))


def verify_remainder(
    root: Path, run_id: str, paths: list[str], *, spawn: Optional[Callable[..., bool]] = None,
) -> dict:
    """The ``VerifyRemainder`` contract. ``refused`` is set to ``import-closure-open`` (with
    ``closure_open`` findings), ``no-base-export``, ``retest-pending`` (child spawned or still running)
    or ``retest-failed``; a pass carries ``retest`` with ``status`` ``pass`` or ``no-tests`` and no
    ``refused`` key."""
    root = Path(root).resolve()
    rels = _norm(paths)
    # A remainder with no test file among its paths gets the closure check only: source-only bytes are not retested.
    closure = _closure_findings(root, rels)
    if closure:
        return {"refused": contract.REFUSED_IMPORT_CLOSURE_OPEN, "closure_open": closure}
    tests = [p for p in rels if _is_test_file(p) and (root / p).is_file()]
    if not tests:
        return {"retest": {"status": "no-tests", "tests": []}}

    vpath = verdict_path(root, run_id)
    fingerprint = landing_retest.landing_fingerprint(root, rels)
    record = _read_verdict(vpath)
    if record and record.get("fingerprint") == fingerprint:
        status = record.get("status")
        if status == "pass":
            return {"retest": record}
        if status == "fail":
            return {"refused": contract.REFUSED_RETEST_FAILED, "retest": record}
        if status == _RUNNING and not _past_deadline(record):
            return {"refused": contract.REFUSED_RETEST_PENDING, "retest": record}

    if not (vpath.parent / BASE_EXPORT_NAME).is_dir():
        return {"refused": contract.REFUSED_NO_BASE_EXPORT,
                "retest": {"status": "error", "reason": f"no base export at {vpath.parent.as_posix()}/{BASE_EXPORT_NAME}"}}
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    started = datetime.now(timezone.utc)
    pending = {
        "run_id": run_id, "status": _RUNNING, "fingerprint": fingerprint, "tests": tests,
        "at": started.isoformat(), "deadline": (started + timedelta(seconds=_child_budget() + _GRACE_SECS)).isoformat(),
    }
    _write_verdict(vpath, pending)
    if not _spawn_child(root, run_id, rels, spawn or spawn_detached):
        pending = {**pending, "status": "error", "reason": "retest child failed to spawn"}
        _write_verdict(vpath, pending)
        return {"refused": contract.REFUSED_RETEST_FAILED, "retest": pending}
    return {"refused": contract.REFUSED_RETEST_PENDING, "retest": pending}


def _child_budget() -> float:
    from coordinator_core.ops.dispatch_emit import retest_targeted

    return retest_targeted.RETEST_CHILD_TIMEOUT_SECS


def run_child(
    root: Path, run_id: str, paths: Sequence[str], *, run_pass: Optional[Callable[..., tuple]] = None,
) -> int:
    """Detached child: overlay ``paths`` on the run's base export, run their test files there, and write
    the verdict keyed to the on-disk bytes read before the export was built. The export is removed on
    every exit path. Returns 0 on pass or no-tests, 1 on fail, 2 on error."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.ops.dispatch_emit import retest_targeted as rt
    from coordinator_core.testing import baseline_export

    root = Path(root).resolve()
    rels = _norm(paths)
    vpath = verdict_path(root, run_id)
    fingerprint = landing_retest.landing_fingerprint(root, rels)
    tests = [p for p in rels if _is_test_file(p) and (root / p).is_file()]
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

    def finish(status: str, reason: str = "", **extra) -> int:
        _write_verdict(vpath, {
            "run_id": run_id, "status": status, "fingerprint": fingerprint, "tests": tests,
            "at": now(), **({"reason": reason} if reason else {}), **extra,
        })
        return {"pass": 0, "fail": 1}.get(status, 2)

    export_root = le.run_export_root(root, run_id)
    base = export_root.with_name(BASE_EXPORT_NAME)
    base_sha = landing_retest.run_base_for(root, run_id, {})
    if not base_sha:
        return finish("error", "run base sha unreadable")
    log = vpath.with_suffix(".log")
    try:
        le.build_landing_export(root, export_root, rels, base_export=base, base_sha=base_sha)
        status, reason, _codes, _used = (run_pass or rt._run_pass)(
            rt._run_argv, export_root, tests, log, time.monotonic() + rt.RETEST_CHILD_TIMEOUT_SECS,
        )
    except le.LandingExportError as exc:
        return finish("error", f"isolated export failed: {exc}")
    finally:
        baseline_export.remove(export_root)
    if status == rt.NO_TESTS_STATUS:
        status = "error"
    return finish(status, reason, log=str(log))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="land_remainder_verify")
    ap.add_argument("--child", action="store_true", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--path", action="append", default=[])
    args = ap.parse_args(argv)
    return run_child(Path(args.root), args.run_id, args.path)


if __name__ == "__main__":
    sys.exit(main())
