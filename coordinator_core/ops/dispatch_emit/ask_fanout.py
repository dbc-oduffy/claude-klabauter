"""
coordinator_core.ops.dispatch_emit.ask_fanout -- JSON-RPC "dispatch.ask_fanout".

Answers "what runs does this multi-ask submission become": it partitions the parent register over
the slices, mints a parent id and one reserved run id per slice, writes one draft slice sizing per
slice and the write-once parent record, and returns the frozen reply plus the ready runs' fire
calls. Contract: docs/reference/multi-ask-submission-contract.md.

Three arms:
  - asks:   {asks: [{ask, source?, sender?, relayed_text?, route_preference?, em_rationale?, em_context?, context?, label?, writes?, stop_after?}],
            sender?, confirm_split?} -- the parent register is one row
            (R1..Rn) per ask, each slice claims its own. An entry key outside ASKS_ITEM_KEYS is
            refused by name. An entry's `em_context` and prose `context` land on its slice sizing as
            `em_analysis.em_brief`, its path-shaped `context` as `em_analysis.em_context_paths`
            (one string, one path per line); the ask, intent and pm_verbatims are never touched.
            An entry's `stop_after` ("plan" only; else refused naming asks[i].stop_after) lands as
            `em_analysis.stop_after` on its slice sizing alone; the top-level default is the hook's.
  - slices: {slices: [{label, ask, rows, writes?}], parent_sizing, confirm_split?} -- the parent
            register is the parent sizing's own rows; each slice carries its rows byte for byte.
  - sizings: {sizings: [{sizing, label?, writes?, wave?, ids?, blocked_by?}], interaction_mode?,
            edges_from?: "blocked_by", confirm_split?} -- with edges_from, after edges follow each item's
            blocked_by (a wave adds none), a blocker outside the batch holds the run, and the reply names
            held runs under `held`. Existing
            planned sizings (each with a `plan:` edge); mints no sizing, runs no partition, only
            stamps each sizing's `em_analysis.ask_parent` and, where the sizing records no
            `interaction_mode`, the inherited top-level one (never overwriting a recorded mode;
            a sizing with none takes the fleet mode; none resolvable is refused by name).

Negative spec:
  - Refuses before writing anything: shape and partition checks run first.
  - An asks item naming no `source` is AI-derived: stamped relayed (text and sender in `em_analysis`),
    no `pm_verbatims`, and the turn log is never read for it, whatever its text.
  - Only an explicit `source: pm` item carries the session's recorded PM turn; when the session records
    none the submission is refused before any write. The log is read once, and only for a pm item.
  - Does NOT call the `sizing.intake_mint` handler (it refuses a second mint for one PM turn).
  - Does NOT spawn a process (no git); every write goes through `session.claimed_write`.
  - Does NOT fire anything: it returns the fire calls, the caller fires them.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops import baton_pm_turns
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_SOURCE_PM,
    ASK_SOURCE_RELAYED,
    ASK_SOURCES,
    STOP_AFTER_KEY,
    STOP_AFTER_PLAN,
)
from coordinator_core.ops.dispatch_emit.route_preference import RoutePreference, parse, refusal, to_em_analysis
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.ops.sizing_intake_contract import (
    EM_BRIEF_KEY,
    EM_CONTEXT_PATHS_KEY,
    em_context_paths_value,
    intake_record,
    is_path_shaped,
    with_em_context,
)
from coordinator_core.session import claimed_write
from coordinator_core.session.record_homes import home_dir
from coordinator_core.warp_fire import fire_command

_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_SLUG_MAX = 40
ASK_PARENT_KEY = "ask_parent"
RELAYED_TEXT_KEY = "relayed_text"
RELAYED_FROM_KEY = "relayed_sender"


class _Refused(ValueError):
    """The submission cannot be admitted; the message names the cause."""


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:_SLUG_MAX].strip("-")


def _writes(value: Any, where: str) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(w, str) and w.strip() for w in value):
        raise _Refused(f"{where}: writes must be a list of non-empty path strings")
    return tuple(w.strip() for w in value)


def _read_slices(params: Mapping) -> tuple[list[contract.AskSlice], Optional[str]]:
    """The submitted slices and, on the slices arm, the parent sizing path."""
    asks, slices = params.get("asks"), params.get("slices")
    if (asks is None) == (slices is None):
        raise _Refused("pass exactly one of asks or slices")
    raw = asks if asks is not None else slices
    if not isinstance(raw, list) or not raw:
        raise _Refused("asks/slices must be a non-empty list")
    out: list[contract.AskSlice] = []
    for i, item in enumerate(raw):
        where = f"{'asks' if asks is not None else 'slices'}[{i}]"
        if not isinstance(item, Mapping):
            raise _Refused(f"{where}: must be an object")
        if asks is not None:
            unknown = contract.unknown_keys_refusal(where, item, contract.ASKS_ITEM_KEYS)
            if unknown:
                raise _Refused(unknown)
        ask = item.get("ask")
        if not isinstance(ask, str) or not ask.strip():
            raise _Refused(f"{where}: ask must be a non-empty string")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise _Refused(f"{where}: label must be a string")
        label = (label or "").strip() or f"slice-{i + 1}"
        source = None
        if asks is not None:
            source = item.get("source")
            if source is not None and source not in ASK_SOURCES:
                raise _Refused(f"{where}: source must be one of {', '.join(ASK_SOURCES)}")
            rows: tuple[str, ...] = (f"R{i + 1}",)
        else:
            row_ids = item.get("rows")
            if not isinstance(row_ids, list) or not all(isinstance(r, str) for r in row_ids):
                raise _Refused(f"{where}: rows must be a list of register row ids")
            rows = tuple(row_ids)
        out.append(contract.AskSlice(label, ask.strip(), rows, _writes(item.get("writes"), where), source=source))
    parent_sizing = None
    if slices is not None:
        parent_sizing = params.get("parent_sizing")
        if not isinstance(parent_sizing, str) or not parent_sizing.strip():
            raise _Refused("the slices arm needs parent_sizing")
    return out, parent_sizing


def _relay_fields(params: Mapping, count: int) -> list[tuple[Optional[str], Optional[str]]]:
    """Per ask item (sender, relayed_text); an item's own `sender` wins over the submission's."""
    raw = params.get("asks")
    if raw is None:
        return [(None, None)] * count
    top = params.get("sender")
    out = []
    for i, item in enumerate(raw):
        sender, text = item.get("sender", top), item.get("relayed_text")
        for key, val in (("sender", sender), ("relayed_text", text)):
            if val is not None and (not isinstance(val, str) or not val.strip()):
                raise _Refused(f"asks[{i}]: {key} must be a non-empty string")
        out.append((sender.strip() if sender else None, text.strip() if text else None))
    return out


def _context_fields(params: Mapping, count: int) -> list[tuple[Optional[str], tuple[str, ...]]]:
    """Per ask item (prose block, context paths) from its `em_context` and `context`; asks arm only.

    The prose block is `em_context` then the prose `context` entries under one EM-context marker;
    path-shaped `context` entries are returned stripped. Both None/() when the item carries neither."""
    raw = params.get("asks")
    if raw is None:
        return [(None, ())] * count
    out = []
    for i, item in enumerate(raw):
        prose: list[str] = []
        paths: list[str] = []
        em_context = item.get("em_context")
        if em_context is not None:
            if not isinstance(em_context, str):
                raise _Refused(f"asks[{i}]: em_context must be a string")
            if em_context.strip():
                prose.append(em_context.strip())
        context = item.get("context")
        if context is not None:
            entries = [context] if isinstance(context, str) else context
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise _Refused(f"asks[{i}]: context must be a string or a list of strings")
            for entry in entries:
                if not entry.strip():
                    continue
                (paths if is_path_shaped(entry) else prose).append(entry.strip())
        block = with_em_context(None, "\n".join(prose)) if prose else None
        out.append((block, tuple(paths)))
    return out


def _route_fields(params: Mapping, count: int) -> list[RoutePreference]:
    """Per ask item its route_preference/em_rationale (asks arm only; other arms get empty)."""
    raw = params.get("asks")
    if raw is None:
        return [RoutePreference(None, None)] * count
    out = []
    for i, item in enumerate(raw):
        pref, why = item.get("route_preference"), item.get("em_rationale")
        problem = refusal(f"asks[{i}]", pref, why)
        if problem:
            raise _Refused(problem)
        out.append(parse(pref, why))
    return out


def _stop_after_fields(params: Mapping, count: int) -> list[Optional[str]]:
    """Per ask item its `stop_after` (asks arm only; other arms get None). Only `plan` is admitted."""
    raw = params.get("asks")
    if raw is None:
        return [None] * count
    out: list[Optional[str]] = []
    for i, item in enumerate(raw):
        value = item.get(STOP_AFTER_KEY)
        if value is not None and value != STOP_AFTER_PLAN:
            raise _Refused(f"asks[{i}].{STOP_AFTER_KEY}: {value!r} is not accepted; the one accepted value is {STOP_AFTER_PLAN!r}")
        out.append(value)
    return out


def _load_parent(root: Path, rel: str) -> dict:
    from coordinator_core.session.record_homes import home_dir

    found = contained_path(root / rel, [Path(home_dir(str(root), "sizings"))])
    if found is None or not found.is_file():
        raise _Refused(f"parent_sizing {rel!r} is not a file under the sizings home")
    try:
        doc = yaml.safe_load(found.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _Refused(f"cannot read parent_sizing {rel}: {exc}") from exc
    return doc if isinstance(doc, dict) else {}


def _parent_rows(sizing: Mapping) -> list[dict]:
    register = sizing.get(rr.REGISTER_KEY)
    rows = register.get(rr.ROWS_KEY) if isinstance(register, Mapping) else None
    return [r for r in rows or [] if isinstance(r, Mapping) and r.get(rr.ROW_ID)]


def _verbatim_turns(parent: Optional[Mapping], sid: Optional[str], cwd: str) -> list[dict]:
    """The PM verbatim a pm-marked slice carries: the parent sizing's own, else the newest recorded
    turn. Empty when the session has recorded no PM turn. Read only for the slices arm or a pm item."""
    if parent:
        kept = [v for v in parent.get("pm_verbatims") or [] if isinstance(v, Mapping) and isinstance(v.get("text"), str)]
        if kept:
            return [dict(v) for v in kept]
    if not sid:
        return []
    newest = baton_pm_turns.recent_window(session_id=sid, cwd=cwd, after_turn=-1)[:1]
    if not newest:
        return []
    return [dict(newest[0])]


def _slice_turns(sl: contract.AskSlice, shared: list[dict]) -> list[dict]:
    """The turns one asks item carries: only an explicit `pm` item carries the shared turns; an
    unmarked or relayed item carries none and is stamped relayed. Never consults the turn log."""
    return shared if sl.source == ASK_SOURCE_PM else []


def _slice_record(
    sl: contract.AskSlice, *, name: str, turns: list[dict], marker: str, today: str,
    parent_rows: Optional[dict[str, dict]], sources: Optional[list] = None,
    sender: Optional[str] = None, relayed_text: Optional[str] = None,
    route_pref: Optional[RoutePreference] = None,
    context: tuple[Optional[str], tuple[str, ...]] = (None, ()),
    stop_after: Optional[str] = None,
) -> dict:
    """A slice sizing. A relayed or unmarked asks item (it arrives with no `turns`) is stamped relayed,
    carries its text and sender in `em_analysis`, and never gets a `pm_verbatims` row."""
    relayed = sl.source == ASK_SOURCE_RELAYED or not turns
    record = intake_record(name=name, turn={"text": sl.ask} if not turns else turns[-1], today=today)
    record["intent"] = sl.ask
    record["intent_source"] = "em-elaborated"
    if relayed:
        record.pop("pm_verbatims", None)
        analysis = record["em_analysis"]
        analysis[contract.SLICE_SOURCE_KEY] = ASK_SOURCE_RELAYED
        analysis[RELAYED_TEXT_KEY] = relayed_text or sl.ask
        if sender:
            analysis[RELAYED_FROM_KEY] = sender
    else:
        record["pm_verbatims"] = [
            {k: v for k, v in (("text", t.get("text", t.get("prompt"))), ("turn", t.get("turn")), ("ts", t.get("ts"))) if v is not None}
            for t in turns
        ]
    record["em_analysis"][ASK_PARENT_KEY] = marker
    prose_block, context_paths = context
    if prose_block:
        record["em_analysis"][EM_BRIEF_KEY] = prose_block
    paths_value = em_context_paths_value(context_paths)
    if paths_value is not None:
        record["em_analysis"][EM_CONTEXT_PATHS_KEY] = paths_value
    if route_pref is not None:
        record["em_analysis"].update(to_em_analysis(route_pref))
    if stop_after:
        record["em_analysis"][STOP_AFTER_KEY] = stop_after
    if parent_rows is not None:
        record[rr.REGISTER_KEY] = {
            rr.SOURCES_KEY: sources,
            rr.ROWS_KEY: [dict(parent_rows[r]) for r in sl.rows],
        }
    return record


def fanout(params: dict, root: Path) -> dict:
    """Partition, mint and write one multi-ask submission; see the module docstring.

    Returns the frozen reply keys (`parent_id`, `runs`) plus `ready` (`[{run_id, sizing}]`, the runs
    firable now, submission order) and `fire` (one `Workflow` call string per ready run, same order).
    A refusal returns `{exit_code: 1, applied: False, error}` having written nothing.
    """
    try:
        return _fanout(params, Path(root))
    except _Refused as exc:
        return _err(str(exc))


def _str_list(value: Any, where: str, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _Refused(f"{where}: {key} must be a list of non-empty strings")
    return [v.strip() for v in value]


def _read_sizings(
    params: Mapping,
) -> list[tuple[str, str, Optional[tuple[str, ...]], Optional[int], list[str], list[str]]]:
    """The sizings arm items as (sizing path, label, writes, wave, ids, blocked_by); waves must not decrease."""
    raw = params.get("sizings")
    if not isinstance(raw, list) or not raw:
        raise _Refused("sizings must be a non-empty list")
    out = []
    top_wave: Optional[int] = None
    for i, item in enumerate(raw):
        where = f"sizings[{i}]"
        if not isinstance(item, Mapping):
            raise _Refused(f"{where}: must be an object")
        rel = item.get("sizing")
        if not isinstance(rel, str) or not rel.strip():
            raise _Refused(f"{where}: sizing must be a non-empty path string")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise _Refused(f"{where}: label must be a string")
        wave = item.get("wave")
        if wave is not None:
            if isinstance(wave, bool) or not isinstance(wave, int) or wave < 0:
                raise _Refused(f"{where}: wave must be an integer >= 0")
            if top_wave is not None and wave < top_wave:
                raise _Refused(f"{where}: wave {wave} is lower than an earlier item's wave {top_wave}; order items by wave")
            top_wave = wave
        out.append((
            rel.strip(), (label or "").strip() or f"sizing-{i + 1}", _writes(item.get("writes"), where), wave,
            _str_list(item.get(contract.ITEM_IDS_KEY), where, contract.ITEM_IDS_KEY),
            _str_list(item.get(contract.ITEM_BLOCKED_BY_KEY), where, contract.ITEM_BLOCKED_BY_KEY),
        ))
    return out


def _fanout_sizings(params: Mapping, worktree: Path, confirm: bool) -> dict:
    """The sizings arm: sibling runs over existing planned sizings, each stamped with its marker."""
    items = _read_sizings(params)
    inherited = params.get(contract.INTERACTION_MODE_PARAM)
    if inherited is not None and (not isinstance(inherited, str) or not inherited.strip()):
        raise _Refused(f"{contract.INTERACTION_MODE_PARAM} must be a non-empty string")
    mode = params.get(contract.EDGES_FROM_PARAM)
    if mode is not None and mode != contract.EDGES_FROM_BLOCKED_BY:
        raise _Refused(f"{contract.EDGES_FROM_PARAM} must be absent or {contract.EDGES_FROM_BLOCKED_BY!r}")
    slices = [contract.AskSlice(label, rel, (), writes, wave) for rel, label, writes, wave, _i, _b in items]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    parent_id = f"ask-parent-{stamp}-{uuid.uuid4().hex[:6]}"
    run_ids = [f"ask-{stamp}-{uuid.uuid4().hex[:6]}" for _ in items]
    holds: dict[int, str] = {}
    if mode is None:
        edges = contract.after_edges(slices)
    else:
        try:
            edges, holds = contract.blocked_by_edges(
                [it[4] for it in items], [it[5] for it in items], [it[2] for it in items]
            )
        except ValueError as exc:
            raise _Refused(str(exc)) from exc

    stamped: list[tuple[Path, str]] = []
    for i, (rel, label, *_rest) in enumerate(items):
        doc = _load_parent(worktree, rel)
        edge = doc.get("plan")
        if not isinstance(edge, str) or not edge.strip():
            raise _Refused(f"sizings[{i}] {rel!r} has no plan edge")
        analysis = doc.get("em_analysis")
        if analysis is None:
            analysis = doc["em_analysis"] = {}
        if not isinstance(analysis, dict):
            raise _Refused(f"sizings[{i}] {rel!r}: em_analysis is not a mapping")
        held = analysis.get(ASK_PARENT_KEY)
        if held:
            raise _Refused(f"sizings[{i}] {rel!r} already carries ask_parent {held!r}")
        analysis[ASK_PARENT_KEY] = contract.ask_parent_marker(parent_id, run_ids[i])
        if not doc.get("interaction_mode"):
            if inherited is None:
                from coordinator_core.ops.dispatch_emit.sizing_fire import _fleet_interaction_mode

                inherited = _fleet_interaction_mode()
            if inherited is None:
                raise _Refused(f"sizings[{i}] {rel!r} records no interaction_mode and the fan-out inherits none")
            doc["interaction_mode"] = inherited.strip()
        errors = validate_frontmatter(doc, _SIZING_SCHEMA_PATH)
        if errors:
            raise _Refused(f"sizings[{i}] {rel!r} failed schema validation: {format_validation_errors(errors)}")
        stamped.append((worktree / rel, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)))

    runs = tuple(
        contract.SiblingRun(
            run_id=run_ids[i],
            label=label,
            after=tuple(run_ids[j] for j in edges[i]),
            slice_sizing=rel,
            status=(
                contract.STATUS_HELD if i in holds
                else contract.STATUS_AFTER if edges[i] else contract.STATUS_READY
            ),
            hold_reason=holds.get(i, ""),
        )
        for i, (rel, label, *_rest) in enumerate(items)
    )
    for path, text in stamped:
        claimed_write.replace_text(path, text)
    parent_path = worktree / contract.parent_record_rel(parent_id)
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    record_json = contract.ParentRecord(parent_id, runs, confirm).to_json()
    claimed_write.create_exclusive(parent_path, json.dumps(record_json, indent=2) + "\n")
    ready = [r for r in runs if r.status == contract.STATUS_READY]
    return {
        "exit_code": 0,
        "applied": True,
        **contract.FanoutReply(parent_id, runs).to_json(),
        "ready": [{"run_id": r.run_id, "sizing": r.slice_sizing} for r in ready],
        "fire": [fire_command(r.slice_sizing) for r in ready],
        contract.HELD_KEY: [
            {"run_id": r.run_id, "label": r.label, "reason": r.hold_reason}
            for r in runs if r.status == contract.STATUS_HELD
        ],
    }


def _fanout(params: Mapping, root: Path) -> dict:
    confirm = params.get("confirm_split", False)
    if not isinstance(confirm, bool):
        raise _Refused("confirm_split must be a boolean")
    if params.get("sizings") is not None:
        if params.get("asks") is not None or params.get("slices") is not None:
            raise _Refused("pass exactly one of asks, slices or sizings")
        return _fanout_sizings(params, main_worktree_root(root), confirm)
    slices, parent_rel = _read_slices(params)
    cwd = str(root)
    worktree = main_worktree_root(root)

    parent_doc: Optional[dict] = None
    if parent_rel is None:
        parent_row_ids = [f"R{i + 1}" for i in range(len(slices))]
        parent_rows = None
    else:
        parent_doc = _load_parent(worktree, parent_rel)
        by_id = {str(r[rr.ROW_ID]): r for r in _parent_rows(parent_doc)}
        parent_row_ids = list(by_id)
        parent_rows = by_id
    refusal = contract.ledger_partition(parent_row_ids, slices)
    if refusal:
        raise _Refused(refusal)

    sid = params.get("session_id") or resolve_current_session_id(Path(cwd))
    log_cwd = params.get("session_root") or cwd
    pm_items = [i for i, sl in enumerate(slices) if sl.source == ASK_SOURCE_PM]
    turns = _verbatim_turns(parent_doc, sid, log_cwd) if parent_rel is not None or pm_items else []
    if pm_items and not turns:
        raise _Refused(
            f"asks[{pm_items[0]}]: source pm, but this session records no PM turn to carry. "
            "Mark it relayed, or submit from the session the PM spoke in."
        )
    relay = _relay_fields(params, len(slices))
    route_prefs = _route_fields(params, len(slices))
    contexts = _context_fields(params, len(slices))
    stop_afters = _stop_after_fields(params, len(slices))

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    parent_hex = uuid.uuid4().hex[:6]
    parent_id = f"ask-parent-{stamp}-{parent_hex}"
    run_ids = [f"ask-{stamp}-{uuid.uuid4().hex[:6]}" for _ in slices]
    today = date.today().isoformat()
    edges = contract.after_edges(slices)

    sources = None
    if parent_doc is not None:
        parent_register = parent_doc.get(rr.REGISTER_KEY)
        sources = (parent_register.get(rr.SOURCES_KEY) if isinstance(parent_register, Mapping) else None) or [
            {"doc": parent_rel, "ref": "split parent"}
        ]
    records: list[tuple[str, str]] = []
    for i, sl in enumerate(slices):
        slug = _slug(sl.label) or f"slice-{i + 1}"
        rel = f"{Path(home_dir('', 'sizings')).as_posix()}/{today}-{parent_hex}-{i + 1}-{slug}.yaml"
        record = _slice_record(
            sl, name=f"{sl.label}"[:60],
            turns=turns if parent_rel is not None else _slice_turns(sl, turns),
            marker=contract.ask_parent_marker(parent_id, run_ids[i]),
            today=today, parent_rows=parent_rows, sources=sources,
            sender=relay[i][0], relayed_text=relay[i][1], route_pref=route_prefs[i],
            context=contexts[i], stop_after=stop_afters[i],
        )
        errors = validate_frontmatter(record, _SIZING_SCHEMA_PATH)
        if errors:
            raise _Refused(f"slice {sl.label!r} failed schema validation: {format_validation_errors(errors)}")
        records.append((rel, yaml.safe_dump(record, sort_keys=False, allow_unicode=True)))

    runs = tuple(
        contract.SiblingRun(
            run_id=run_ids[i],
            label=sl.label,
            after=tuple(run_ids[j] for j in edges[i]),
            slice_sizing=records[i][0],
            status=contract.STATUS_AFTER if edges[i] else contract.STATUS_READY,
        )
        for i, sl in enumerate(slices)
    )

    for rel, text in records:
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            claimed_write.create_exclusive(target, text)
        except FileExistsError as exc:
            raise _Refused(f"sizing already exists: {rel}") from exc
    parent_path = worktree / contract.parent_record_rel(parent_id)
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    record_json = contract.ParentRecord(parent_id, runs, confirm).to_json()
    claimed_write.create_exclusive(parent_path, json.dumps(record_json, indent=2) + "\n")
    turn_no = turns[-1].get("turn") if turns else None
    if sid and isinstance(turn_no, int) and not isinstance(turn_no, bool):
        baton_pm_turns.mark_sized_through(turn_no, session_id=sid, cwd=cwd)

    ready = [r for r in runs if r.status == contract.STATUS_READY]
    reply = contract.FanoutReply(parent_id, runs).to_json()
    return {
        "exit_code": 0,
        "applied": True,
        **reply,
        "ready": [{"run_id": r.run_id, "sizing": r.slice_sizing} for r in ready],
        "fire": [fire_command(r.slice_sizing) for r in ready],
    }


@register_op(contract.FANOUT_OP)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_fanout" handler: `fanout` over the repo root. Params as `fanout`, plus
    optional session_id."""
    if repo_root is None:
        return _err("dispatch.ask_fanout: repo_root is required")
    return fanout(params, repo_root)
