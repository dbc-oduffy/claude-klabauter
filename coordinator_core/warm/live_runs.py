"""Detect live mid-stage Workflow runs across registered repos, so a warm-pair stop can refuse first.

Pure file reads: globs each registered repo's state/warp/*/run-stage.json; spawns nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.session import record_homes

# A run-stage.json whose last_activity_at is older than this is abandoned, not live.
FRESHNESS_WINDOW = timedelta(minutes=30)

_TERMINAL_STAGES = frozenset({"halted", "complete"})


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


def _live_stage(
    data: dict,
    cutoff: datetime,
    owner_memo: Optional[dict] = None,
) -> Optional[str]:
    """The run's stage when it is live under the shared predicate, else None.

    Liveness rests on the run's own `last_activity_at` stamp and a live owner; a workflow journal
    that merely names the run id is not evidence, since a journal embeds the ids it launched.
    """
    stage = data.get("stage")
    if not isinstance(stage, str) or not stage or stage in _TERMINAL_STAGES:
        return None
    ts = _parse_ts(data.get("last_activity_at"))
    if ts is not None and ts >= cutoff and _owner_live(data.get("session_id"), owner_memo):
        return stage
    return None


def run_is_live(
    repo_root: Path,
    run_id: str,
    *,
    now: Optional[datetime] = None,
    window: timedelta = FRESHNESS_WINDOW,
) -> bool:
    """True when the run's stage is not halted/complete, its owning session is live, and its own `last_activity_at` is inside `window`.

    Unreadable or missing records are not live.
    """
    repo_root = Path(repo_root)
    data = _read_stage(Path(record_homes.home_dir(str(repo_root), "warp")) / run_id / "run-stage.json")
    if data is None:
        return False
    cutoff = (now or datetime.now(timezone.utc)) - window
    return _live_stage(data, cutoff) is not None


def find_live_runs(
    repos: Optional[Mapping[str, str]] = None,
    *,
    now: Optional[datetime] = None,
    window: timedelta = FRESHNESS_WINDOW,
) -> list[LiveRun]:
    """Return runs `run_is_live` accepts: not halted/complete, owner session live, own stage activity inside the window.

    `repos` maps registry key to repo root (default: the registry). Unreadable repos or
    records are skipped. Result is sorted by repo then run_id.
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
            stage = _live_stage(data, cutoff, owner_memo)
            if stage is None:
                continue
            found.append(LiveRun(name, rec.parent.name, stage, str(data.get("last_activity_at") or "")))
    found.sort(key=lambda r: (r.repo, r.run_id))
    return found


def format_refusal(runs: list[LiveRun]) -> str:
    """One line per live run plus a terse alternative."""
    lines = ["Live Workflow runs mid-stage; the warm pair stop would cut them:"]
    lines += [
        f"  {r.repo}  {r.run_id}  stage={r.stage}  last_activity={r.last_activity_at}"
        for r in runs
    ]
    lines.append("Alternative: publish again once those runs finish.")
    return "\n".join(lines)


def unnamed_runs(runs: list[LiveRun], names: Optional[list[str]]) -> list[LiveRun]:
    """The runs in `runs` whose run_id is not in `names` (None or empty names no run)."""
    named = set(names or ())
    return [r for r in runs if r.run_id not in named]


def require_reason(raw: object) -> str:
    """Return the stripped override reason; raise ValueError when it is not a non-blank string."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("--allow-mid-stage needs a non-empty reason")
    return raw.strip()


def format_override(reason: str, runs: list[LiveRun]) -> str:
    """The log record for an override: the reason, then one line per run the warm pair stop will cut."""
    lines = [f"--allow-mid-stage: reason={reason!r}; cutting {len(runs)} live Workflow run(s):"]
    lines += [
        f"  {r.repo}  {r.run_id}  stage={r.stage}  last_activity={r.last_activity_at}"
        for r in runs
    ]
    return "\n".join(lines)
