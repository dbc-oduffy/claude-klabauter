"""Run-bound engine identity receipts: which engine process launched each stage worker.

`<run record dir>/engine-identity.jsonl` takes one line per stage launch: `run_id`, `stage_id`,
`worker_pid` (the spawned child), `engine_pid` (the process that launched it), `engine_root`,
`engine_head` (the engine checkout's own HEAD), `source_stamp` (the claude-klabauter source sha that head
publishes, the `running_sha` `engine.drift` reports) and a UTC `ts`. `run.engine_identity` (COMPUTE_ONLY)
replies the latest line plus `consistent`: every line agrees on `engine_head` and `source_stamp`.

Negative-spec: zero spawns (`engine_version.resolve_engine_source_head` reads `.git` objects, once per
process); one append of one small line; never raises into a launch, a missing receipt is an absent
line and the reader reports it as `count: 0`, never as a consistent run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

RECEIPT_FILE = "engine-identity.jsonl"
OP = "run.engine_identity"
FIELDS = ("run_id", "stage_id", "worker_pid", "engine_pid", "engine_root", "engine_head", "source_stamp", "ts")
_AGREE = ("engine_head", "source_stamp")

PROMPT_LINE = (
    "Engine identity: `coordinator-invoke run.engine_identity '{{\"run_id\":\"{run_id}\"}}'` returns the engine "
    "head and source stamp of the process that launched this stage."
)

_IDENTITY: list = []


def process_identity() -> dict:
    """`{engine_root, engine_head, source_stamp}` of this process's engine checkout, read once: an engine
    swap starts a new process, so a process never reports two answers. Unresolvable parts are None."""
    if not _IDENTITY:
        found: Optional[dict] = None
        try:
            from coordinator_core.engine_version import resolve_engine_source_head
            from coordinator_core.git.repo_root import _walk_for_repo

            walked = _walk_for_repo(Path(__file__).resolve().parent)
            head = resolve_engine_source_head(walked[1] if walked else None)
            found = {
                "engine_root": str(Path(walked[1]).resolve()) if walked else None,
                "engine_head": head.engine_head,
                "source_stamp": head.sha,
            }
        except Exception:  # noqa: BLE001 -- identity is evidence, never a launch gate
            found = {"engine_root": None, "engine_head": None, "source_stamp": None}
        _IDENTITY.append(found)
    return dict(_IDENTITY[0])


def _base(root: Path) -> Path:
    """The main worktree for a linked worktree's root; a plain root is its own base."""
    try:
        return main_worktree_root(Path(root))
    except ValueError:
        return Path(root)


def _receipt_path(root: Path, run_id: str) -> Path:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    return record_dir(_base(root), run_id) / RECEIPT_FILE


def append_receipt(
    root: Path, run_id: str, stage_id: str, worker_pid: Optional[int] = None, launcher: Optional[str] = None,
) -> Optional[dict]:
    """Append this launch's receipt and return it; None when the run's record dir cannot take it.
    `launcher` (e.g. "workflow") is recorded only when given."""
    try:
        line = {
            "run_id": run_id, "stage_id": stage_id, "worker_pid": worker_pid, "engine_pid": os.getpid(),
            **process_identity(),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        if launcher:
            line["launcher"] = launcher
        path = _receipt_path(root, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write((json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8"))
        return line
    except (OSError, ValueError):
        return None


def append_stage_start_receipt(root: Path, run_id: str, stage_id: str, launcher: str = "workflow") -> Optional[dict]:
    """The engine-side receipt for a launcher with no worker spawn (an in-session Workflow): one line with
    `worker_pid` null, skipped when the run already has a receipt so a spawn recorder's lines are not doubled.
    Never raises."""
    try:
        path = _receipt_path(root, run_id)
        if path.is_file() and path.stat().st_size > 0:
            return None
        return append_receipt(root, run_id, stage_id, None, launcher)
    except Exception:  # noqa: BLE001 -- evidence, never a stage gate
        return None


def spawn_recorder(
    root: Path, run_id: Optional[str], stage_id: str, chained: Optional[Callable[[int], None]] = None,
) -> Optional[Callable[[int], None]]:
    """An `on_spawn` callback that appends the receipt with the worker pid, then calls `chained`."""
    if not run_id:
        return chained

    def on_spawn(pid: int) -> None:
        append_receipt(root, run_id, stage_id, pid)
        if chained is not None:
            chained(pid)

    return on_spawn


def read_latest(root: Path, run_id: str) -> dict:
    """`{run_id, receipt, count, consistent}`: the last parseable line, how many parsed, and whether all agree
    on `engine_head` and `source_stamp`. No line: `receipt` None, `count` 0, `consistent` False."""
    rows: list = []
    try:
        text = _receipt_path(root, run_id).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    consistent = bool(rows) and all(
        all(row.get(key) == rows[0].get(key) for key in _AGREE) for row in rows
    )
    return {"run_id": run_id, "receipt": rows[-1] if rows else None, "count": len(rows), "consistent": consistent}


@register_op(OP)
def _engine_identity(params: dict, repo_root: Optional[Any] = None) -> dict:
    """JSON-RPC "run.engine_identity": params `run_id` (required), `repo` (optional, else the caller's repo).

    Replies `read_latest`; refuses `{"error": str}` for a missing or unsafe run_id or no repo root.
    """
    run_id = (params or {}).get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "run_id is required"}
    base = (params or {}).get("repo") or repo_root
    if not base:
        return {"error": "repo is required"}
    try:
        return read_latest(Path(base), run_id)
    except ValueError as exc:
        return {"error": str(exc)}
