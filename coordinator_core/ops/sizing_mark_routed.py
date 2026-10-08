"""coordinator_core.ops.sizing_mark_routed — JSON-RPC "sizing.mark_routed".

Moves a sizing `sized -> routed` and records the artifact its route landed as
(today: the ratified goal, `goal_id`). A goal-setting/roadmap/shape sizing is
terminal only through the deliverable cascade; `sizing.ship` refuses those
routes, so this is the sanctioned "handed off" stamp the landing ceremony
writes instead. Without it the sizing stays `sized` and goal-blitz's sweep
re-goals it.

Idempotent on the same `goal_id`; refuses a different one (two landings of
one ask is a duplicate, not an update).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, replace_fm_field
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.sizing_ship import _err, _validate_sizing_fm

_ROUTABLE_FROM = frozenset({"sized", "routed"})


def _set_goal_id(text: str, goal_id: str) -> str:
    if read_fm_field_unquoted(text, "goal_id") is not None:
        return replace_fm_field(text, "goal_id", goal_id)
    head, sep, rest = text.partition("\nstatus:")
    line_end = rest.find("\n")
    status_line = rest if line_end < 0 else rest[:line_end]
    tail = "" if line_end < 0 else rest[line_end:]
    return f"{head}{sep}{status_line}\ngoal_id: {goal_id}{tail}"


@register_op("sizing.mark_routed")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: `sizing_path` (under state/sizings/), `goal_id`. Both required.

    exit_code 0 applied True  — status -> routed and goal_id written.
    exit_code 0 applied False — already routed with this goal_id.
    exit_code 1               — bad params, not found, terminal or draft status,
                                a different goal_id already recorded, schema
                                refusal, lock timeout.
    """
    sizing_path_raw = (params.get("sizing_path") or "").strip()
    goal_id = (params.get("goal_id") or "").strip()
    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if not goal_id:
        return _err("missing required param: goal_id")
    if repo_root is None:
        return _err("sizing.mark_routed: repo_root is required")

    worktree = main_worktree_root(repo_root)
    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [worktree / "state" / "sizings"])
    if p is None:
        return _err(f"sizing_path escapes state/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_path_raw}")

    state = {"applied": False}

    def mutate(old_text: str) -> str:
        status = read_fm_field_unquoted(old_text, "status")
        recorded = read_fm_field_unquoted(old_text, "goal_id")
        if status not in _ROUTABLE_FROM:
            raise MutateAbort(
                f"refusing to mark {p} routed: status is {status!r}, not one of "
                f"{sorted(_ROUTABLE_FROM)}"
            )
        if recorded and recorded != goal_id:
            raise MutateAbort(
                f"refusing to mark {p} routed to {goal_id!r}: it already landed as "
                f"{recorded!r}; a second landing of one ask is a duplicate"
            )
        if status == "routed" and recorded == goal_id:
            return old_text
        new_text = _set_goal_id(replace_fm_field(old_text, "status", "routed"), goal_id)
        errors = _validate_sizing_fm(new_text)
        if errors:
            raise MutateAbort(
                f"mark_routed: post-mutation schema validation failed: "
                f"{format_validation_errors(errors)}"
            )
        state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mark_routed: mutation aborted")

    if state["applied"]:
        return {"exit_code": 0, "applied": True, "message": f"{sizing_path_raw} routed to {goal_id}"}
    return {"exit_code": 0, "applied": False, "message": f"{sizing_path_raw} already routed to {goal_id}"}
