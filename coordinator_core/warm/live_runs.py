"""Detect live mid-stage Workflow runs across registered repos, so a warm-pair handover can hold, then refuse, for them.

Pure file reads: globs each registered repo's state/warp/*/run-stage.json; spawns nothing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from coordinator_core.session import record_homes

# How long a handover waits for mid-stage runs to end before it refuses, and how often it re-reads.
HOLD_BOUND_S = 120.0
HOLD_POLL_S = 2.0

# A run-stage.json whose last_activity_at is older than this is abandoned, not live.
FRESHNESS_WINDOW = timedelta(minutes=30)

# `landed` is terminal too (orphan_adopt agrees): a hand-landed run left at `landed` with fresh
# activity otherwise blocks every warm stop for 30 minutes.
_TERMINAL_STAGES = frozenset({"halted", "complete", "landed"})


@dataclass(frozen=True)
class LiveRun:
    repo: str
    run_id: str
    stage: str
    last_activity_at: str


def _parse_ts(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _registered_repos() -> Mapping[str, str]:
    from coordinator_core.ops.fleet._memo_resolver import read_registry_repos

    try:
        return read_registry_repos()
    except Exception:
        # Fails open: an unreadable registry means no live run is reported, so the stop proceeds.
        return {}


def _read_stage(rec: Path) -> Optional[dict]:
    try:
        data = json.loads(rec.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stage_records(warp_dir: Path) -> list[Path]:
    """warp_dir/*/run-stage.json via one scandir; a missing warp dir yields none."""
    try:
        entries = list(os.scandir(warp_dir))
    except FileNotFoundError:
        return []
    return [Path(e.path) / "run-stage.json" for e in entries]


def _owner_live(session_id: object, memo: Optional[dict] = None) -> bool:
    """True when `session_id` names a session that is live now; absent, unsafe or unreadable is False.

    `memo` caches the verdict per session_id for one find_live_runs call.
    """
    if not isinstance(session_id, str) or not session_id or session_id != Path(session_id).name:
        return False
    if memo is not None and session_id in memo:
        return memo[session_id]
    from coordinator_core.session.liveness import session_live

    try:
        live = bool(session_live(session_id))
    except Exception:
        live = False
    if memo is not None:
        memo[session_id] = live
    return live


def _token_held(repo_root: Path, run_id: str) -> bool:
    """True when the run holds a live run token or a claimed one (mid terminal-commit); pathlib reads only.

    An unreadable token store (OSError) reads as held, so a live run is not dropped from the handover
    log on a transient read failure; a malformed record reads as not held."""
    from coordinator_core.ops.dispatch_emit import run_token

    try:
        return any(r.get("run_id") == run_id for r in run_token.held_records(repo_root))
    except OSError:
        return True
    except (ValueError, KeyError, TypeError):
        return False


def _live_stage(
    data: dict,
    cutoff: datetime,
    owner_memo: Optional[dict] = None,
    *,
    repo_root: Path,
    run_id: str,
    require_token: bool = True,
) -> Optional[str]:
    """The run's stage when it is live under the shared predicate, else None.

    Liveness rests on the run's own `last_activity_at` stamp and a live owner; a workflow journal
    that merely names the run id is not evidence, since a journal embeds the ids it launched.
    A run holding no run token (live or claimed) is hand-landed or abandoned, not live.
    """
    stage = data.get("stage")
    if not isinstance(stage, str) or not stage or stage in _TERMINAL_STAGES:
        return None
    ts = _parse_ts(data.get("last_activity_at"))
    if ts is None or ts < cutoff or not _owner_live(data.get("session_id"), owner_memo):
        return None
    if require_token and not _token_held(repo_root, run_id):
        return None
    if stage == "minted":
        from coordinator_core import warp_live

        if warp_live.runner_unproven(repo_root, run_id):
            return None
    return stage


def run_is_live(
    repo_root: Path,
    run_id: str,
    *,
    now: Optional[datetime] = None,
    window: timedelta = FRESHNESS_WINDOW,
) -> bool:
    """True when the run's stage is not halted/complete, its owning session is live, its own `last_activity_at` is inside `window`, and it holds a run token.

    Unreadable or missing records are not live.
    """
    repo_root = Path(repo_root)
    data = _read_stage(Path(record_homes.home_dir(str(repo_root), "warp")) / run_id / "run-stage.json")
    if data is None:
        return False
    cutoff = (now or datetime.now(timezone.utc)) - window
    return _live_stage(data, cutoff, repo_root=repo_root, run_id=run_id) is not None


def run_stage_live(
    repo_root: Path,
    run_id: str,
    *,
    now: Optional[datetime] = None,
    window: timedelta = FRESHNESS_WINDOW,
) -> bool:
    """run_is_live without the run-token test: stage, live owner and fresh activity only.

    The reaper's bar: a mid-stage run between ops holds no active token and must not be reaped.
    """
    repo_root = Path(repo_root)
    data = _read_stage(Path(record_homes.home_dir(str(repo_root), "warp")) / run_id / "run-stage.json")
    if data is None:
        return False
    cutoff = (now or datetime.now(timezone.utc)) - window
    return _live_stage(data, cutoff, repo_root=repo_root, run_id=run_id, require_token=False) is not None


def run_owner_holds(repo_root: Path, run_id: str) -> bool:
    """True when the run's stage is non-terminal and its owning session is live, whatever the age
    of `last_activity_at`. A long stage (an executor, a review) stamps nothing for its whole length,
    so freshness alone reads a live run as abandoned. This is the reaper's bar, which is stricter
    than `run_stage_live` (no freshness window), because reaping a live run is not undone by a refire."""
    data = _read_stage(Path(record_homes.home_dir(str(Path(repo_root)), "warp")) / run_id / "run-stage.json")
    if data is None:
        return False
    stage = data.get("stage")
    if not isinstance(stage, str) or not stage or stage in _TERMINAL_STAGES:
        return False
    return _owner_live(data.get("session_id"))


def find_live_runs(
    repos: Optional[Mapping[str, str]] = None,
    *,
    now: Optional[datetime] = None,
    window: timedelta = FRESHNESS_WINDOW,
    require_token: bool = True,
) -> list[LiveRun]:
    """Return runs `run_is_live` accepts: not halted/complete, owner session live, own stage activity inside the window, run token held.

    `require_token=False` is the `run_stage_live` bar: a staged run between ops holds no token and
    still counts. `repos` maps registry key to repo root (default: the registry). Unreadable repos
    or records are skipped. Result is sorted by repo then run_id.
    """
    if repos is None:
        repos = _registered_repos()
    cutoff = (now or datetime.now(timezone.utc)) - window
    found: list[LiveRun] = []
    owner_memo: dict = {}
    seen_roots: set[str] = set()
    for key, root in repos.items():
        try:
            root_key = os.path.normcase(str(Path(root).resolve()))
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            records = _stage_records(Path(record_homes.home_dir(str(root), "warp")))
        except OSError:
            continue
        name = key[len("repos."):] if key.startswith("repos.") else key
        for rec in records:
            data = _read_stage(rec)
            if data is None:
                continue
            stage = _live_stage(
                data, cutoff, owner_memo, repo_root=Path(root), run_id=rec.parent.name,
                require_token=require_token,
            )
            if stage is None:
                continue
            found.append(LiveRun(name, rec.parent.name, stage, str(data.get("last_activity_at") or "")))
    found.sort(key=lambda r: (r.repo, r.run_id))
    return found


def format_refusal(runs: list[LiveRun], held_s: float = 0.0) -> str:
    """The refusal text for a warm-pair handover held up by mid-stage runs: one line per run, id and repo."""
    lines = [
        f"  {r.repo} {r.run_id} stage={r.stage} last_activity={r.last_activity_at}"
        for r in runs
    ]
    return (
        f"warm handover refused: {len(runs)} run(s) mid-stage after holding {held_s:.0f}s; "
        "a staged run's next leg is an engine op, so the server is not stopped under it:\n"
        + "\n".join(lines)
    )


def hold_for_mid_stage_runs(
    *,
    bound_s: Optional[float] = None,
    poll_s: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[list[LiveRun], float]:
    """Wait up to `bound_s` for every registered repo's mid-stage runs (own included, token or not) to end.

    Returns (runs still mid-stage, seconds held): the runs empty means the handover may stop the
    server; non-empty means the bound lapsed and the caller refuses with `format_refusal`. Reads
    only; spawns nothing.
    """
    bound = HOLD_BOUND_S if bound_s is None else bound_s
    poll = HOLD_POLL_S if poll_s is None else poll_s
    start = monotonic()
    while True:
        runs = find_live_runs(require_token=False)
        held = monotonic() - start
        if not runs or held >= bound:
            return runs, held
        sleep(min(poll, max(bound - held, 0.0)))
