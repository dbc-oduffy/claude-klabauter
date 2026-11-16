"""Absorb a late `workflow.revise` into the running plan at the post-DAG settle point.

Two ops and one JS composer. `workflow.pending_revisions` (COMPUTE_ONLY) replies the register
rows the run's plan has not claimed, plus the ids whose pinned text drifted. `plan.claim_register_rows`
(MUTATING) appends named register rows to the plan's `register_claims.rows` and pins them, in one
`locked_rmw`. `absorb_tail_js` is the script text that runs after the DAG settles and before the
terminal commit: check, plan-author adds one spine row per new register row, each added row runs.

File I/O only: no git, no subprocess.

Negative-spec: absorbs NEW rows only. A row whose pinned text changed is reported as `drift` and
left to the `register-pin-drift` path (`plan.repin_register`); the tail never acts on it. Never
edits the sizing, `requirements_ledger.py` or any plan field other than `register_claims.rows`
and `register_claims.pins`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.executor_economy import (
    ABSORB_ROUNDS,
    CLAIM_ROWS_OP,
    PENDING_REVISIONS_OP,
)
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.workflow_scaffold import _js_string_literal

GENERATES = []

_PENDING_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("plan", "nonempty_str", required=True),
)
_CLAIM_PARAMS = (
    Field("plan", "nonempty_str", required=True),
    Field("rows", "str_list", required=True),
)

_PENDING_SCHEMA = {
    "type": "object",
    "required": ["rows", "drift"],
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text"],
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "kind": {"type": "string"}},
            },
        },
        "drift": {"type": "array", "items": {"type": "string"}},
    },
}
_ADDED_SCHEMA = {
    "type": "object",
    "required": ["added"],
    "properties": {
        "added": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "writes"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "writes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_MECHANICAL_OPTS = "agentType: 'coordinator:executor', model: 'sonnet', effort: 'low'"
_AUTHOR_OPTS = "agentType: 'coordinator:plan-author', model: 'opus'"
_ROW_OPTS = "agentType: 'coordinator:executor', model: 'sonnet'"
_PHASE_VAR = "_EXECUTE_PHASE"


def _plan_key(path: object) -> str:
    return str(path or "").replace("\\", "/").removeprefix("./")


def _plan_fm(plan_text: str) -> str:
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(plan_text)
    return split.fm_text if split is not None else ""


NOTE_ROW_MARKER = "revise-note: "
NOTE_KIND = "note"


def _note_key(entry: dict, run_id: str) -> str:
    """A note's id as a row names it: bare for this run's own, `<run>:<id>` for a predecessor's (note ids
    are per-run ordinals, so two runs' `N1` would collide)."""
    return entry["id"] if entry["run"] == run_id else f"{entry['run']}:{entry['id']}"


def _spine_rows(plan_text: str) -> list[dict]:
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    try:
        located = locate_fenced_block(plan_text)
        rows = yaml.safe_load(located.body) if located.status is LocateStatus.LOCATED else None
    except yaml.YAMLError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def unexecuted_notes(root: Path, run_id: str, plan_text: str) -> list[dict]:
    """`[{id, text, kind: "note"}]` for the run's live revise notes (its own and its `resumed_from`
    chain's) that no spine row of `plan_text` executes, oldest first, `id` the `_note_key`. A note is
    executed once a row's body carries `revise-note: <key>`, or the note names that row's id or one of
    its `writes` (the kept-work rule that sends such a row back to run). Keyed by note, never by sizing."""
    from coordinator_core.ops.dispatch_emit import run_notes

    rows = _spine_rows(plan_text)
    out: list[dict] = []
    for entry in sorted(run_notes.live(rl.carried_notes(root, run_id)), key=lambda e: e["at"]):
        key = _note_key(entry, run_id)
        marker = re.compile(re.escape(NOTE_ROW_MARKER + key) + r"(?![A-Za-z0-9_:])")
        text = entry["note"]
        if any(_executes(row, marker, text) for row in rows):
            continue
        out.append({"id": key, "text": text, "kind": NOTE_KIND})
    return out


def _executes(row: dict, marker: "re.Pattern[str]", note: str) -> bool:
    if marker.search(str(row.get("body") or "")):
        return True
    row_id = str(row.get("id") or "")
    if row_id and re.search(r"(?<![A-Za-z0-9_])" + re.escape(row_id) + r"(?![A-Za-z0-9_])", note):
        return True
    return any(
        isinstance(w, str) and w and re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(w) + r"(?![A-Za-z0-9_])", note)
        for w in row.get("writes") or ()
    )


@register_op(PENDING_REVISIONS_OP)
def _pending_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.pending_revisions": params `run_id`, `plan` (both required).

    Replies `{rows: [{id, text, kind?}], drift: [ids], plan, sizing}`: register rows the plan's
    `register_claims.rows` omit, minus rows a recorded out-of-scope ruling withholds, then the run's
    live revise notes no spine row executes (`kind: "note"`, see `unexecuted_notes`), and the ids
    whose pinned text drifted. Refuses `{"error": str}` for an unemitted run, a run with no bound
    sizing, a sizing with no readable plan edge, a `plan` that is not that edge, claims that name
    another sizing or carry no `rows` list, and a sizing with no register: never an empty reply
    for an unreadable input. When the plan is readable and notes are owed, they ride in `rows` even
    under a register refusal, which is then carried as `register_refusal`.
    """
    reply = _pending_register(params, repo_root)
    if repo_root is None:
        return reply
    root = main_worktree_root(Path(repo_root))
    plan = _run_plan(root, params)
    if plan is None:
        return reply
    owed = unexecuted_notes(root, params["run_id"], plan[1])
    if not owed:
        return reply
    if "error" in reply:
        return {"rows": owed, "drift": [], "plan": plan[0], "sizing": plan[2], "register_refusal": reply["error"]}
    reply["rows"] = [*reply["rows"], *owed]
    return reply


def _run_plan(root: Path, params: dict) -> Optional[tuple[str, str, str]]:
    """`(plan_rel, plan_text, sizing_rel)` of the run's sizing edge when `params["plan"]` names it, else None."""
    try:
        run = rl.read_run(rl.run_dir(root, params["run_id"]))
    except (rl.LedgerRefused, KeyError, TypeError):
        return None
    sizing_rel = (run or {}).get("sizing_rel")
    if not (isinstance(sizing_rel, str) and sizing_rel):
        return None
    edge, plan, _ = rl._sizing_plan(root, sizing_rel)
    if plan is None or _plan_key(edge) != _plan_key(params.get("plan")):
        return None
    try:
        return edge, plan.read_text(encoding="utf-8", errors="replace"), sizing_rel
    except OSError:
        return None


def _pending_register(params: dict, repo_root: Optional[Path]) -> dict:
    """The register half of `_pending_handler`: unclaimed rows and drift, or the refusal."""
    if repo_root is None:
        return {"error": f"{PENDING_REVISIONS_OP} requires repo_root"}
    refusal = validate_params(PENDING_REVISIONS_OP, params, _PENDING_PARAMS)
    if refusal is not None:
        return refusal
    root = main_worktree_root(Path(repo_root))
    try:
        dir_ = rl.run_dir(root, params["run_id"])
    except rl.LedgerRefused as exc:
        return {"error": str(exc)}
    run = rl.read_run(dir_)
    if run is None:
        return {"error": f"run {params['run_id']} has no ledger (not emitted)"}
    sizing_rel = run.get("sizing_rel")
    if not (isinstance(sizing_rel, str) and sizing_rel):
        return {"error": f"run {params['run_id']} is not bound to a sizing"}
    edge, plan, sizing_text = rl._sizing_plan(root, sizing_rel)
    if plan is None:
        return {"error": f"{sizing_rel} names no readable plan under docs/plans"}
    if _plan_key(edge) != _plan_key(params["plan"]):
        return {"error": f"plan {params['plan']!r} is not the plan {sizing_rel} names ({edge})"}
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    claims = plan_frontmatter(plan.read_text(encoding="utf-8", errors="replace")).get("register_claims")
    if not isinstance(claims, dict) or claims.get("sizing") != sizing_rel:
        return {"error": f"{edge} register_claims does not name {sizing_rel}"}
    if not isinstance(claims.get("rows"), list):
        return {"error": f"{edge} register_claims.rows is not a list"}
    register = rr.read_register(sizing_text)
    if register is None:
        return {"error": f"{sizing_rel} carries no requirement_register"}
    _, missing = rl._unclaimed_rows(root, sizing_rel)
    _, withheld = rl.ledger_ids(register.rows)
    wanted = [i for i in missing if i not in set(withheld)]
    text_of = {str(r[rr.ROW_ID]): str(r.get(rr.ROW_TEXT) or "") for r in register.rows}
    _, drift = rl.pinned_plan_drift(root, sizing_rel)
    return {
        "rows": [{"id": i, "text": text_of[i]} for i in wanted],
        "drift": drift,
        "plan": edge,
        "sizing": sizing_rel,
    }


def _claim_rows_text(plan_text: str, ids: list[str], register: rr.Register) -> str:
    """`plan_text` with `ids` appended to `register_claims.rows` (one flow-style line) and every
    claimed row pinned; every other byte is kept."""
    import yaml

    nl = "\r\n" if "\r\n" in plan_text else "\n"
    lines = plan_text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("plan has no frontmatter")
    fm_end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if fm_end is None:
        raise ValueError("plan frontmatter is not closed")
    claims = (yaml.safe_load("".join(lines[1:fm_end])) or {}).get("register_claims")
    if not isinstance(claims, dict):
        raise ValueError("plan has no register_claims")
    current = [str(i) for i in (claims.get(rr.ROWS_KEY) or [])]
    head = next(
        (i for i in range(1, fm_end) if re.match(r"register_claims:\s*(#.*)?$", lines[i].rstrip("\r\n"))),
        None,
    )
    if head is None:
        raise ValueError("register_claims is not a block mapping")
    claims_end = rr._sub_block_end(lines, head, fm_end)
    child = next((rr._indent_of(lines[i]) for i in range(head + 1, claims_end) if lines[i].strip()), 2)
    key_re = re.compile(r"^ {%d}rows:" % child)
    at = next((i for i in range(head + 1, claims_end) if key_re.match(lines[i])), None)
    if at is None:
        raise ValueError("register_claims carries no rows key")
    lines[at:rr._sub_block_end(lines, at, claims_end)] = [
        f"{' ' * child}{rr.ROWS_KEY}: {json.dumps(current + ids)}{nl}"
    ]
    new_text, _ = rr.repin_plan_text("".join(lines), register)
    return new_text


@register_op(CLAIM_ROWS_OP)
def _claim_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.claim_register_rows": params `plan` (repo-relative path under docs/plans/),
    `rows` (register row ids).

    Appends the ids to the plan's `register_claims.rows` and pins every claimed row with the
    `plan.repin_register` pin form, in one `locked_rmw`. Replies `{ok, plan, claimed}`. Refuses
    `{"error": str}` for an id absent from the register, an id already claimed, a duplicate id, a
    plan outside docs/plans, a plan with no `register_claims.sizing`, an unreadable sizing, and a
    lock timeout; nothing is written on a refusal.
    """
    if repo_root is None:
        return {"error": f"{CLAIM_ROWS_OP} requires repo_root"}
    refusal = validate_params(CLAIM_ROWS_OP, params, _CLAIM_PARAMS)
    if refusal is not None:
        return refusal
    ids = [i.strip() for i in params["rows"]]
    if not ids or not all(ids) or len(set(ids)) != len(ids):
        return {"error": "rows must be a non-empty list of distinct non-empty ids"}
    root = main_worktree_root(Path(repo_root))
    p = Path(params["plan"])
    p = contained_path(p if p.is_absolute() else root / p, [root / "docs" / "plans"])
    if p is None:
        return {"error": f"plan escapes docs/plans: {params['plan']!r}"}
    if not p.is_file():
        return {"error": f"plan not found on disk: {params['plan']}"}
    plan_rel = p.relative_to(root.resolve()).as_posix()
    try:
        sizing_rel, claimed = rr.plan_claims(_plan_fm(p.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"plan unreadable: {exc}"}
    if not sizing_rel:
        return {"error": f"{plan_rel} carries no register_claims.sizing"}
    try:
        register = rr.read_register(rl._sizing_path(root, sizing_rel).read_text(encoding="utf-8"))
    except (rl.LedgerRefused, OSError, UnicodeDecodeError):
        return {"error": f"sizing-object unreadable: {sizing_rel}"}
    if register is None:
        return {"error": f"{sizing_rel} carries no requirement_register"}
    absent = rr.unknown_row_ids(register, ids)
    if absent:
        return {"error": f"rows absent from the register: {', '.join(absent)}"}

    crlf = b"\r\n" in p.read_bytes()

    def mutate(old_text: str) -> str:
        _, now_claimed = rr.plan_claims(_plan_fm(old_text.replace("\r\n", "\n")))
        dup = [i for i in ids if i in now_claimed]
        if dup:
            raise MutateAbort(f"rows already claimed: {', '.join(dup)}")
        try:
            new_text = _claim_rows_text(old_text, ids, register)
        except ValueError as exc:
            raise MutateAbort(str(exc)) from exc
        return new_text.replace("\n", "\r\n") if crlf else new_text

    try:
        locked_rmw(p, mutate, repo_root=root)
    except FileNotFoundError:
        return {"error": f"plan disappeared before the lock could be acquired: {p}"}
    except LockTimeout as exc:
        return {"error": f"timed out waiting for file lock on {p}: {exc}"}
    except MutateAbort as exc:
        return {"error": str(exc.args[0]) if exc.args else "claim_register_rows: mutation aborted"}
    return {"ok": True, "plan": plan_rel, "claimed": ids}


def absorb_tail_js(
    *,
    plan_path: str,
    run_id: str,
    row_opts: str = _ROW_OPTS,
    phase_expr: str = _PHASE_VAR,
) -> str:
    """The JS statements run after the DAG settles and before the terminal commit.

    Up to `ABSORB_ROUNDS` rounds of: a mechanical agent runs `workflow.pending_revisions` (schema
    `{rows, drift}`); empty `rows` stops; else a plan-author agent adds one spine row per register
    row through `plan.tasks.mutate add-task` and runs `plan.claim_register_rows`, replying
    `{added: [{id, title, writes}]}`; each added row then runs through the script's `_runRow` with
    `_absorbPrompt(id, title)`, which the emitting script defines. Drift ids are logged and left
    to the pin-drift path. `row_opts` is a JS object-literal fragment; `phase_expr` is a JS expression.
    """
    invoke = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
    plan_js = _js_string_literal(plan_path)
    run_js = _js_string_literal(run_id)
    pending_params = json.dumps({"run_id": run_id, "plan": plan_path}, separators=(",", ":"))
    check = (
        f"Run `{invoke} {PENDING_REVISIONS_OP} '{pending_params}'` once and return its reply's "
        "`rows` and `drift` verbatim. Judge nothing, edit nothing, commit nothing. If the op "
        "refuses or errors, return empty `rows` and empty `drift`. Run the command with its command "
        "word exactly as written (a guard denies the prefix moved into a shell variable)."
    )
    author = (
        f"Plan {plan_path} gained register rows the running plan does not claim. For each row in "
        "the rows below, add exactly one spine row with "
        f"`{invoke} plan.tasks.mutate` verb add-task (params plan_path={plan_path}, task): a "
        "unique id, a title, declared `writes`, and `depends_on` only on rows already landed. "
        f"A row whose kind is `{NOTE_KIND}` is a revise note, not a register row: its spine row's `body` "
        f"begins with the line `{NOTE_ROW_MARKER}<its id>` followed by the note text verbatim, and its "
        "`writes` are the repo paths the note names. Then "
        f"run `{invoke} {CLAIM_ROWS_OP}` once, when any row is not a note, with params "
        f'{{"plan":"{plan_path}","rows":[<the register row ids, never a note id>]}}. '
        'Reply `{added: [{id, title, writes}]}` for the rows you added. Edit nothing else; never '
        "hand-edit the plan. Run each command with its command word exactly as written (a guard "
        "denies the prefix moved into a shell variable)."
    )
    return "\n".join([
        "  for (let _absorbRound = 0; _absorbRound < " + str(ABSORB_ROUNDS) + "; _absorbRound++) {",
        "    let _absorbPending = null;",
        "    try {",
        "      _absorbPending = await agent(" + _js_string_literal(check) + ", { label: 'absorb:check-' + _absorbRound, "
        f"phase: {phase_expr}, {_MECHANICAL_OPTS}, schema: {json.dumps(_PENDING_SCHEMA)} }});",
        "    } catch (e) { _absorbPending = null; }",
        "    if (!_absorbPending) break;",
        "    if (Array.isArray(_absorbPending.drift) && _absorbPending.drift.length) {",
        "      log('absorb: pinned register rows drifted (left to the pin-drift path): ' + _absorbPending.drift.join(', '));",
        "    }",
        "    if (!Array.isArray(_absorbPending.rows) || !_absorbPending.rows.length) break;",
        "    let _absorbAdded = null;",
        "    try {",
        "      _absorbAdded = await agent(" + _js_string_literal(author) + " + ' Rows: ' + JSON.stringify(_absorbPending.rows), "
        "{ label: 'absorb:plan-' + _absorbRound, "
        f"phase: {phase_expr}, {_AUTHOR_OPTS}, schema: {json.dumps(_ADDED_SCHEMA)} }});",
        "    } catch (e) { _absorbAdded = null; }",
        "    if (!_absorbAdded || !Array.isArray(_absorbAdded.added) || !_absorbAdded.added.length) break;",
        "    log('absorb: round ' + _absorbRound + ' added ' + _absorbAdded.added.map((a) => a.id).join(', ') + ' to ' + "
        + plan_js + " + ' for run ' + " + run_js + ");",
        "    await Promise.all(_absorbAdded.added.map((a) => _runRow(",
        "      a.id, [], null,",
        "      async () => agent(_absorbPrompt(a.id, a.title), { label: 'work:' + a.id, "
        f"phase: {phase_expr}, {row_opts} }}),",
        "      { title: a.title, paths: [], declared: a.writes || [] })));",
        "  }",
    ])
