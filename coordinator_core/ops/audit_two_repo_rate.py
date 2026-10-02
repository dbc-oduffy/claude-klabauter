"""
coordinator_core.ops.audit_two_repo_rate — JSON-RPC "goal.kr2_two_repo_rate"
operation (docs/plans/2026-07-20-kr-baselining-package.md, C1).

Purpose: measure the claude-klabauter side of KR2 (`kr-2` of goal
`goal-claude-klabauter-engine-of-record`): how many engine-tool commits landed in a
window. An engine-tool commit is a non-merge commit touching at least one path
under `ENGINE_PATH_PREFIXES`. Read-only: the op never writes a file; the caller
appends the returned `record` to `state/audits/kr2-two-repo-rate.jsonl`.

NEGATIVE-SPEC (read before touching this module):
  - The pairing leg is NOT measured. No source in this repo links a claude-klabauter
    commit to the coordinator-content-repo commit it needed, so `pairing.status` is
    "unmeasured", `evidence_source` is null, and `paired_commits` and
    `single_repo_rate` are null — never 0, never 1.0. No heuristic (commit
    keywords, path overlap, cross-repo-commitments linkage) may fill them.
  - A shallow clone is refused (`exit_code: 2`, `error: "shallow-history"`):
    its count is a truncated undercount that reads as a baseline.
  - Exactly two git spawns per call, never one per commit.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.git.run import GitResult, run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops._git_root_util import git_root

SCHEMA = "kr2-two-repo-rate/v1"
GOAL_ID = "goal-claude-klabauter-engine-of-record"
KR = "kr-2"
ENGINE_PATH_PREFIXES = ("coordinator_core/", "bin/")
DEFAULT_WINDOW_DAYS = 28
_SENTINEL = "\x01"
_PAIRING_REASON = (
    "no evidence source links a claude-klabauter commit to a paired coordinator-content-repo commit "
    "(docs/plans/2026-07-20-kr-baselining-package.md § Pairing evidence)"
)


def _git(args: List[str], cwd: str) -> Optional[GitResult]:
    result = run_git(args, cwd=cwd)
    if result.timed_out or result.returncode == 127:
        return None
    return result


def _resolve_window(params: dict) -> Tuple[date, date]:
    until_raw = params.get("until")
    until = (
        date.fromisoformat(str(until_raw))
        if until_raw
        else datetime.now(timezone.utc).date()
    )
    since_raw = params.get("since")
    since = (
        date.fromisoformat(str(since_raw))
        if since_raw
        else until - timedelta(days=DEFAULT_WINDOW_DAYS)
    )
    return since, until


def _count_engine_commits(log_output: str) -> int:
    count = 0
    for chunk in log_output.split(_SENTINEL)[1:]:
        paths = chunk.split("\n")[1:]
        if any(p.startswith(ENGINE_PATH_PREFIXES) for p in paths):
            count += 1
    return count


@register_op("goal.kr2_two_repo_rate")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    # Body spawns git and reads the filesystem; off-loop so dispatch's wait_for can fire.
    return await asyncio.to_thread(_run, params, repo_root)


def _run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "goal.kr2_two_repo_rate" handler — KR2 of
    `goal-claude-klabauter-engine-of-record`; read-only, two git spawns.

    Optional params: `repo_root` (default: git root of cwd), `until` (ISO date,
    default today UTC), `since` (ISO date, default `until` minus 28 days); both
    bounds inclusive.

    Returns `{"exit_code": 0, "record": {...}}` where the record is the
    `kr2-two-repo-rate/v1` shape: schema, goal_id, kr, captured_at, head,
    window{since, until}, engine_path_prefixes, engine_tool_commits,
    pairing{status: "unmeasured", evidence_source: null, reason},
    paired_commits: null, single_repo_rate: null.

    Refusals: `{"exit_code": 2, "error": "not-a-git-repo" | "shallow-history"}`
    (a shallow clone truncates the window). `exit_code: 1` on a malformed date.
    The pairing leg is unmeasured: no source links a claude-klabauter commit to its
    paired coordinator-content-repo commit.
    """
    try:
        since, until = _resolve_window(params or {})
    except ValueError as exc:
        return {"exit_code": 1, "error": f"goal.kr2_two_repo_rate: bad date ({exc})"}

    root = (params or {}).get("repo_root") or repo_root or git_root(Path.cwd())
    if not root or not Path(str(root)).is_dir():
        return {"exit_code": 2, "error": "not-a-git-repo"}
    root = str(root)

    probe = _git(["rev-parse", "--is-shallow-repository", "HEAD"], root)
    lines = probe.stdout.split() if probe is not None and probe.returncode == 0 else []
    if len(lines) != 2:
        return {"exit_code": 2, "error": "not-a-git-repo"}
    if lines[0] == "true":
        return {"exit_code": 2, "error": "shallow-history"}
    head = lines[1]

    log = _git(
        [
            "log",
            "--no-merges",
            f"--since={since.isoformat()}T00:00:00Z",
            f"--until={until.isoformat()}T23:59:59Z",
            "--name-only",
            f"--format={_SENTINEL}%H",
        ],
        root,
    )
    if log is None or log.returncode != 0:
        return {"exit_code": 2, "error": "not-a-git-repo"}

    record: Dict[str, Any] = {
        "schema": SCHEMA,
        "goal_id": GOAL_ID,
        "kr": KR,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "head": head,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "engine_path_prefixes": list(ENGINE_PATH_PREFIXES),
        "engine_tool_commits": _count_engine_commits(log.stdout),
        "pairing": {
            "status": "unmeasured",
            "evidence_source": None,
            "reason": _PAIRING_REASON,
        },
        "paired_commits": None,
        "single_repo_rate": None,
    }
    return {"exit_code": 0, "record": record}
