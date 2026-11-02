"""codex_runner worker: `python -m coordinator_core.codex_runner <run_dir>`.

Spawned detached by `codex_runner.start`. Reads `<run_dir>/request.json`, runs review or
execute, and writes `<run_dir>/result.json` atomically. The codex pid exists only once the
mode function has spawned codex, so the roster record is written from its `on_spawn`
callback and removed in `finally`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.codex_runner.contract import (
    STATUS_FAILED,
    ModeRun,
    RunRequest,
    RunResult,
)

REQUEST_FILENAME = "request.json"
RESULT_FILENAME = "result.json"


def _mode_run(mode: str) -> ModeRun:
    """Resolve review.run / execute.run / agy.review_run lazily so importing this module needs none."""
    if mode == "agy-review":
        return importlib.import_module("coordinator_core.codex_runner.agy").review_run
    return importlib.import_module(f"coordinator_core.codex_runner.{mode}").run


def _write_result(run_dir: Path, result: RunResult) -> None:
    final = run_dir / RESULT_FILENAME
    tmp = run_dir / f"{RESULT_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(result.to_dict()), encoding="utf-8")
    os.replace(tmp, final)


def run_worker(run_dir: Path) -> RunResult:
    """Run the request in `run_dir` to completion and persist the result; never raises."""
    from coordinator_core.codex_runner import roster

    run_id = run_dir.name
    codex_pids: List[int] = []
    req: Optional[RunRequest] = None
    try:
        req = RunRequest.from_dict(json.loads((run_dir / REQUEST_FILENAME).read_text(encoding="utf-8")))
        request = req

        def on_spawn(pid: int) -> None:
            codex_pids.append(pid)
            roster.write_record(pid, run_id, request.mode, request.repo_root, request.parent_session_id)

        result = _mode_run(req.mode)(req, run_dir, on_spawn)
    except BaseException as exc:  # noqa: BLE001 -- the worker must always leave a result
        result = RunResult(
            run_id=run_id,
            status=STATUS_FAILED,
            codex_pid=codex_pids[0] if codex_pids else None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        for pid in codex_pids:
            try:
                roster.remove_record(pid)
            except OSError:
                pass
    _write_result(run_dir, result)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m coordinator_core.codex_runner <run_dir>\n")
        return 2
    run_worker(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
