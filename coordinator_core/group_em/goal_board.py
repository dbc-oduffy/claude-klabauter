"""The G-EM fleet goal board: schema validation, drift flags, and its sole writer.

`refresh` is the only code in `coordinator_core` that writes `<group-em home>/fleet-goal-board.json`;
the watch and every other reader only read it. The board is validated against the vendored
`contract/fleet-goal-board.schema.json` before any byte is written, so an invalid board is refused
and the file on disk is left untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.group_em import gem_home
from coordinator_core.group_em.atomic_record import write_json_atomic

BOARD_FILENAME = "fleet-goal-board.json"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contract" / "fleet-goal-board.schema.json"

DEFAULT_STALE_AFTER_HOURS = 24
FIFA_GOAL = "FIFA"

STALE = "stale"
NO_LIVE_OWNER = "no-live-owner"
UNMAPPED_TASK = "unmapped-task"

_schema_cache: Optional[dict] = None


def board_path() -> Path:
    """`<group-em home>/fleet-goal-board.json`. Raises `gem_home.HomeRefused` for a home inside a checkout."""
    return gem_home.resolve_home() / BOARD_FILENAME


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


def validate(board) -> list[dict]:
    """Schema errors for `board` as `{field, error, hint}` dicts; an empty list means valid."""
    from coordinator_core.frontmatter.schema_validate import validate_frontmatter_obj

    result = validate_frontmatter_obj(board, _schema())
    return [] if result.get("ok") else list(result.get("errors") or [])


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _registry_is_live(session_id: str) -> bool:
    from coordinator_core.group_em.session_registry import is_live

    return is_live({"session_id": session_id})[0]


def compute_flags(
    board: dict, now: datetime, is_live: Optional[Callable[[str], bool]] = None
) -> list[dict]:
    """Drift flags for `board` as `[{flag, id}]`, in goal order then task_map order.

    `stale`: a goal's `last_updated` is older than `stale_after_hours` (default 24) or unparsable.
    `no-live-owner`: no owner of a goal is a live session. `unmapped-task`: a `task_map` entry whose
    goal is null or names no goal row; `FIFA` always counts as mapped; its id is `<em>/<task_id>`.
    `is_live` maps a session id to a bool and defaults to the session registry.
    """
    live = is_live or _registry_is_live
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = timedelta(hours=int(board.get("stale_after_hours") or DEFAULT_STALE_AFTER_HOURS))
    goals = [g for g in board.get("goals") or [] if isinstance(g, dict)]
    flags: list[dict] = []
    liveness: dict[str, bool] = {}

    def owner_live(owner: str) -> bool:
        if owner not in liveness:
            liveness[owner] = bool(live(owner))
        return liveness[owner]

    for goal in goals:
        stamp = _parse_ts(goal.get("last_updated"))
        if stamp is None or now - stamp > cutoff:
            flags.append({"flag": STALE, "id": goal.get("id")})
    for goal in goals:
        owners = [o for o in goal.get("owners") or [] if isinstance(o, str)]
        if not any(owner_live(o) for o in owners):
            flags.append({"flag": NO_LIVE_OWNER, "id": goal.get("id")})
    known = {g.get("id") for g in goals} | {FIFA_GOAL}
    for entry in board.get("task_map") or []:
        if not isinstance(entry, dict):
            continue
        goal_id = entry.get("goal")
        if goal_id is None or goal_id not in known:
            flags.append({"flag": UNMAPPED_TASK, "id": f"{entry.get('em')}/{entry.get('task_id')}"})
    return flags


def _refusal(path, field: str, error: str, hint: str) -> dict:
    return {
        "ok": False,
        "path": str(path) if path is not None else None,
        "flags": [],
        "errors": [{"field": field, "error": error, "hint": hint}],
    }


def refresh(now: Optional[datetime] = None, is_live: Optional[Callable[[str], bool]] = None) -> dict:
    """Validate the board in the group-em home, recompute its flags and rewrite it atomically.

    Returns `{ok, path, flags, errors}`. Refusals (home inside a checkout, board absent or not
    JSON, schema errors) return `ok: False` with `errors` and never touch the file. On success the
    board's `board_as_of` becomes `now` and its `flags` become `[{kind, subject}]` per the schema.
    """
    now = now or _utcnow()
    try:
        path = board_path()
    except gem_home.HomeRefused as exc:
        return _refusal(
            None, gem_home.KEY, str(exc),
            f"set {gem_home.KEY} to a directory outside any checkout",
        )
    try:
        board = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _refusal(path, "board", f"cannot read board: {exc.strerror or exc}",
                        "seed the board in the group-em home")
    except ValueError as exc:
        return _refusal(path, "board", f"board is not JSON: {exc}", "fix the board file")
    errors = validate(board)
    if errors:
        return {"ok": False, "path": str(path), "flags": [], "errors": errors}
    flags = compute_flags(board, now, is_live)
    out = dict(board)
    out["board_as_of"] = _format_ts(now)
    out["flags"] = [{"kind": f["flag"], "subject": f["id"]} for f in flags]
    errors = validate(out)
    if errors:
        return {"ok": False, "path": str(path), "flags": flags, "errors": errors}
    write_json_atomic(path, out)
    return {"ok": True, "path": str(path), "flags": flags, "errors": []}
