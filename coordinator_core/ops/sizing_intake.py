"""
coordinator_core.ops.sizing_intake -- JSON-RPC "sizing.intake_mint".

Answers "what is the EM's intake surface for the newest PM ask": a draft sizing-object
seeded with the session's newest recorded PM turn only, plus an optional EM brief under
`em_analysis.em_brief` (see `sizing_intake_contract`). The returned `submit` line fires the
run through engine-ask's `{sizing}` arm.

Negative spec:
  - Does NOT scan `state/sizings/`; the mint-twice guard is the session's
    `sized_through_turn` mark, so cost does not grow with the corpus.
  - Does NOT spawn a process (no git).
  - Does NOT block on a missing or blank brief.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import baton_pm_turns
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.ops.sizing_intake_contract import (
    EM_BRIEF_KEY,
    intake_record,
    relayed_intake_record,
)
from coordinator_core.session.record_homes import home_dir
from coordinator_core.session_baton import store

_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_SLUG_MAX = 60


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:_SLUG_MAX].strip("-")


@register_op("sizing.intake_mint")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.intake_mint" handler.

    Params: name (str, required slug source), em_brief (str, optional), session_id (str,
    optional), source ("relayed" forces relayed provenance), sender and relayed_text (str,
    optional, carried on a relayed sizing). With source "relayed" or no recorded PM turn the
    sizing is relayed: no pm_verbatims row, turn None, no sized mark. Otherwise the newest PM
    turn seeds it. Returns exit_code 0 with applied, sizing_path (repo-relative), turn,
    provenance, submit; exit_code 1 with error on: no name, a turn already at or behind the
    session's sized_through_turn mark, an existing target path, or a schema failure.
    """
    if repo_root is None:
        return _err("sizing.intake_mint: repo_root is required")
    slug = _slug(str(params.get("name") or ""))
    if not slug:
        return _err("missing required param: name")
    em_brief = params.get("em_brief")
    if em_brief is not None and not isinstance(em_brief, str):
        return _err("em_brief must be a string")

    cwd = str(repo_root)
    sid = params.get("session_id") or resolve_current_session_id(Path(cwd))
    if not sid:
        return _err("no resolvable session id")

    source = params.get("source")
    sender = params.get("sender")
    relayed_text = params.get("relayed_text")
    for key, val in (("source", source), ("sender", sender), ("relayed_text", relayed_text)):
        if val is not None and not isinstance(val, str):
            return _err(f"{key} must be a string")

    turn = None
    if source != "relayed":
        newest = baton_pm_turns.recent_window(session_id=sid, cwd=cwd, after_turn=-1)[:1]
        if newest:
            turn = newest[0]
            mark = store.read_baton(sid, cwd).get("sized_through_turn")
            if isinstance(mark, int) and not isinstance(mark, bool) and turn["turn"] <= mark:
                return _err(f"turn {turn['turn']} is already sized")

    today = date.today().isoformat()
    if turn is None:
        text = (relayed_text or "").strip() or (em_brief or "").strip() or slug
        record = relayed_intake_record(name=slug, text=text, sender=sender or "", today=today)
    else:
        record = intake_record(name=slug, turn=turn, today=today)
    if em_brief and em_brief.strip():
        record["em_analysis"][EM_BRIEF_KEY] = em_brief.strip()
    errors = validate_frontmatter(record, _SIZING_SCHEMA_PATH)
    if errors:
        return _err(f"intake record failed schema validation: {format_validation_errors(errors)}")

    worktree = main_worktree_root(repo_root)
    rel = f"{Path(home_dir('', 'sizings')).as_posix()}/{today}-{slug}.yaml"
    target = worktree / rel
    if target.exists():
        return _err(f"sizing already exists: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)

    def mutate(old_text: str) -> str:
        if old_text:
            raise MutateAbort(f"sizing already exists: {rel}")
        return text

    try:
        locked_rmw(target, mutate, repo_root=repo_root, missing_ok=True)
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {rel}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "intake mint aborted")

    if turn is not None:
        baton_pm_turns.mark_sized_through(turn["turn"], session_id=sid, cwd=cwd)
    return {
        "exit_code": 0,
        "applied": True,
        "sizing_path": rel,
        "turn": turn["turn"] if turn is not None else None,
        "provenance": "pm-verbatim" if turn is not None else "relayed",
        "submit": (
            'Workflow({name: "coordinator:engine-ask", '
            f'args: {{sizing: "{rel}"}}}})'
        ),
    }
