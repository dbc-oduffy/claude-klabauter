"""
coordinator_core.ops.goal_record_go — "goal.record_go" op.

Purpose: record a goal-level go on ``state/goals/<goal>.yaml`` as an authorization
record. ``/roadmap-planning`` reads it to treat the goal's roadmap-seeds as
PM-acked. The op dispatches nothing.

Params: ``goal`` (goal id or file stem), ``who`` (the authorizer), exactly one or
both of ``quote`` (the PM's words) and ``ruling_ref`` (an APM ruling path/id),
optional ``seeds`` (handoff ids to authorize; default every live
``kind: roadmap-seed`` handoff whose ``origin_goal_id`` names the goal and that is
not already shipped/continued/closed), optional ``at`` (ISO timestamp; default now).

Record shape, appended to the goal's ``goal_go`` list (field undeclared in
goal.schema.json, which is DoE-vendored and permissive)::

    goal_go:
      - scope: goal
        who: ...
        at: ...
        quote: ... | ruling_ref: ...
        seeds: [hnd-..., ...]

Refuses: unknown goal, goal not ``active``, missing ``who``, neither ``quote`` nor
``ruling_ref``, a named seed that is not a live seed of this goal, no seeds to
authorize. Idempotent: an identical (who, quote, ruling_ref, seeds) record is an
``applied: False`` success.

Negative-spec:
  - Does NOT git-commit and does NOT dispatch anything.
  - Does NOT touch any other goal field; the record is appended under ``locked_rmw``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root

MUTATES = ["state/goals/*.yaml"]

_DONE_STATES = frozenset({"shipped", "continued", "closed"})
_KEY = "goal_go"


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": f"goal.record_go: {message}"}


def _load_goal(text: str) -> dict:
    split = split_frontmatter(text)
    body = split.fm_text if split is not None else text
    loaded = yaml.safe_load(body)
    return loaded if isinstance(loaded, dict) else {}


def _find_goal(worktree: Path, ref: str) -> Optional[tuple[Path, dict]]:
    for path in sorted((worktree / "state" / "goals").glob("*.yaml")):
        try:
            fm = _load_goal(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if ref in (path.stem, fm.get("id")):
            return path, fm
    return None


def _goal_seeds(worktree: Path, goal_id: str) -> dict[str, str]:
    """handoff_id -> repo-relative path of every live, undone roadmap-seed of the goal."""
    found: dict[str, str] = {}
    for path in sorted((worktree / "state" / "handoffs").glob("*.md")):
        try:
            fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        origin = fm.get("origin_goal_id")
        origins = [origin] if isinstance(origin, str) else (origin or [])
        if fm.get("kind") != "roadmap-seed" or goal_id not in origins:
            continue
        if str(fm.get("deployment_state") or "") in _DONE_STATES:
            continue
        hid = str(fm.get("handoff_id") or path.stem)
        found[hid] = path.relative_to(worktree).as_posix()
    return found


def _render(record: dict[str, Any]) -> str:
    q = json.dumps
    lines = [
        f"  - scope: {record['scope']}",
        f"    who: {q(record['who'])}",
        f"    at: {q(record['at'])}",
    ]
    if record.get("quote"):
        lines.append(f"    quote: {q(record['quote'])}")
    if record.get("ruling_ref"):
        lines.append(f"    ruling_ref: {q(record['ruling_ref'])}")
    lines.append("    seeds: [" + ", ".join(q(s) for s in record["seeds"]) + "]")
    return "\n".join(lines) + "\n"


def _append_record(text: str, record: dict[str, Any]) -> str:
    """Append ``record`` to the ``goal_go`` list, creating the key at the end when absent."""
    block = _render(record)
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^{_KEY}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if key_re.match(line.rstrip("\r\n")):
            end = i + 1
            while end < len(lines) and (
                not lines[end].strip() or lines[end][0] in " \t-"
            ):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            if end > 0 and not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines.insert(end, block)
            return "".join(lines)
    prefix = text if not text or text.endswith("\n") else text + "\n"
    return f"{prefix}{_KEY}:\n{block}"


@register_op("goal.record_go")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Append a goal-level go record to the goal file.

    Returns ``{exit_code, applied, goal, seeds, message}`` or ``{exit_code: 1,
    applied: False, error}``.
    """
    ref = str(params.get("goal") or "").strip()
    who = str(params.get("who") or "").strip()
    quote = str(params.get("quote") or "").strip()
    ruling_ref = str(params.get("ruling_ref") or "").strip()
    if not ref:
        return _err("missing required param: goal")
    if not who:
        return _err("missing required param: who")
    if not quote and not ruling_ref:
        return _err("one of quote or ruling_ref is required")
    if repo_root is None:
        return _err("repo_root is required")

    worktree = main_worktree_root(repo_root)
    hit = _find_goal(worktree, ref)
    if hit is None:
        return _err(f"goal not found under state/goals/: {ref!r}")
    path, goal = hit
    if goal.get("status") != "active":
        return _err(f"goal {ref!r} is {goal.get('status')!r}, not active")

    available = _goal_seeds(worktree, str(goal.get("id") or path.stem))
    named = params.get("seeds")
    if named:
        wanted = [str(s).strip() for s in named]
        stray = [s for s in wanted if s not in available]
        if stray:
            return _err(f"not a live roadmap-seed of this goal: {stray}")
        seeds = sorted(set(wanted))
    else:
        seeds = sorted(available)
    if not seeds:
        return _err("goal has no live roadmap-seeds to authorize")

    record = {
        "scope": "goal",
        "who": who,
        "at": str(params.get("at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "quote": quote,
        "ruling_ref": ruling_ref,
        "seeds": seeds,
    }
    state = {"applied": False}

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        for prior in _load_goal(old_text).get(_KEY) or []:
            if (
                isinstance(prior, dict)
                and prior.get("who") == who
                and (prior.get("quote") or "") == quote
                and (prior.get("ruling_ref") or "") == ruling_ref
                and sorted(prior.get("seeds") or []) == seeds
            ):
                return old_text
        state["applied"] = True
        if split is None:
            return _append_record(old_text, record)
        return rebuild(split, _append_record(split.fm_text, record))

    try:
        locked_rmw(path, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except (OSError, yaml.YAMLError) as exc:
        return _err(f"cannot read/write goal: {exc}")

    rel = path.relative_to(worktree).as_posix()
    return {
        "exit_code": 0,
        "applied": state["applied"],
        "goal": rel,
        "seeds": seeds,
        "message": (
            f"{rel} goal_go recorded for {len(seeds)} seed(s)"
            if state["applied"]
            else f"{rel} already carries this goal_go - no-op"
        ),
    }
