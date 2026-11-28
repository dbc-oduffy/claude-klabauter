"""``dispatch.adopt_predecessor_work``: a fresh-chain refire adopts the kept uncommitted work of a halted run.

Params: ``run_id`` (the fresh run), ``predecessor`` (a halted or superseded run of the same sizing). Each path
the predecessor's ``kept.json`` records is credited into the fresh run's ``manifest.json`` ``write_baseline``
(set to HEAD's blob, or null when HEAD lacks it, so the live bytes read as the run's own write) only while the
live blob carries a recorded print (``write_baseline._carries_print``); a path a foreign rewrite dropped, or that
is absent, is refused and named. ``run.json`` then records ``adopted_from``; both writes are ``locked_rmw``.
Refuses a predecessor of another sizing, one with no halt record (still running: its kept set is not final), a
landed or resumed one, and the run itself; a repeat adoption of the same predecessor replies ``applied: false``.
Adoption is explicit: ``resumed_from`` semantics are untouched. In-process, zero git spawns.
Reply: ``{ok, run_id, predecessor, applied, adopted: [path], refused: {path: reason}}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit.rebind_contract import (
    ADOPTED,
    APPLIED,
    OP_ADOPT_PREDECESSOR_WORK,
    REFUSED,
)

OP_NAME = OP_ADOPT_PREDECESSOR_WORK

#: run.json key listing the predecessor runs whose kept work the run adopted.
ADOPTED_FROM_FIELD = "adopted_from"

_ADOPTABLE = ("halted", "superseded")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _norm_sizing(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def _sizing_of(home: Path) -> Optional[str]:
    """The sizing a run record binds: ``run.json`` ``sizing_rel``, else ``halted.json``'s."""
    from coordinator_core.ops.dispatch_emit.ask_contract import _read_json_object

    for name in ("run.json", "halted.json"):
        found = _norm_sizing((_read_json_object(home / name) or {}).get("sizing_rel"))
        if found:
            return found
    return None


def _predecessor_refusal(root: Path, run_id: str, predecessor: str, home: Path) -> Optional[str]:
    """Why ``predecessor`` is not adoptable, else None."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    marker = ask_contract._read_json_object(home / "halted.json")
    if marker is None:
        stage = (ask_contract.read_run_stage(root, predecessor) or {}).get("stage")
        if stage == "landed":
            return f"{predecessor} landed; it is not a halted or superseded run"
        return f"{predecessor} has no halt record: it is still running and its kept set is not final"
    status = marker.get("status")
    if status not in _ADOPTABLE:
        return f"{predecessor} is {status!r}; only a halted or superseded run is adoptable"
    fresh = _sizing_of(ask_contract.record_dir(root, run_id))
    theirs = _sizing_of(home)
    if fresh is None or theirs is None:
        return f"cannot compare sizings: {run_id if fresh is None else predecessor} binds none"
    if fresh != theirs:
        return f"{predecessor} is a run of {theirs}, not {fresh}"
    return None


def _credit(root: Path, kept: dict) -> tuple[dict, dict]:
    """``(credited {path: baseline value}, refused {path: reason})`` over the predecessor's kept prints."""
    from coordinator_core.git.git_state import head_blobs
    from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
    from coordinator_core.ops.dispatch_emit.write_baseline import _carries_print

    held: list[str] = []
    refused: dict[str, str] = {}
    for rel in sorted(kept):
        live = fingerprint(root, rel)
        if live is None:
            refused[rel] = "absent from the worktree"
        elif live in kept[rel] or any(_carries_print(root, rel, p, live) for p in sorted(kept[rel])):
            held.append(rel)
        else:
            refused[rel] = "live blob carries no recorded print (rewritten by foreign work)"
    head = head_blobs(root, held) if held else {}
    return {rel: (head[rel][1] if rel in head else None) for rel in held}, refused


def _merge_baseline(root: Path, manifest: Path, credited: dict) -> None:
    """Set each credited path's ``write_baseline`` entry under one manifest RMW, in canonical form."""
    from coordinator_core.locked_write import MutateAbort, locked_rmw
    from coordinator_core.ops.dispatch_emit.reply_sidecar import canonical_json

    def mutate(old: str) -> str:
        try:
            data = json.loads(old)
        except ValueError as exc:
            raise MutateAbort(f"{manifest.name} is not JSON") from exc
        base = data.get("write_baseline") if isinstance(data, dict) else None
        if not isinstance(base, dict):
            raise MutateAbort(f"{manifest.name} carries no write_baseline to credit into")
        if all(p in base and base[p] == v for p, v in credited.items()):
            return old
        base.update(credited)
        return canonical_json(data)

    locked_rmw(manifest, mutate, repo_root=root)


def _record_adoption(root: Path, run_json: Path, predecessor: str) -> bool:
    """Append ``predecessor`` to run.json ``adopted_from`` under one RMW; False when already listed."""
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    added = [False]

    def mutate(old: str) -> str:
        try:
            data = json.loads(old)
        except ValueError as exc:
            raise MutateAbort("run.json is not JSON") from exc
        if not isinstance(data, dict):
            raise MutateAbort("run.json is not an object")
        prior = data.get(ADOPTED_FROM_FIELD)
        prior = [p for p in prior if isinstance(p, str)] if isinstance(prior, list) else []
        if predecessor in prior:
            return old
        data[ADOPTED_FROM_FIELD] = prior + [predecessor]
        added[0] = True
        return json.dumps(data, sort_keys=True) + "\n"

    locked_rmw(run_json, mutate, repo_root=root)
    return added[0]


def adopt_predecessor_work(root: Path, run_id: str, predecessor: str) -> dict:
    """Adopt ``predecessor``'s kept work into ``run_id``; the handler's body, callable without IPC."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort
    from coordinator_core.ops.dispatch_emit import ask_contract
    from coordinator_core.ops.dispatch_emit.kept_work import kept_fingerprints
    from coordinator_core.session.declared_writes import declare_write

    if run_id == predecessor:
        return _refuse(f"{run_id} cannot adopt its own work")
    try:
        fresh_home = ask_contract.record_dir(root, run_id)
        pred_home = ask_contract.record_dir(root, predecessor)
    except ValueError as exc:
        return _refuse(str(exc))
    run_json, manifest = fresh_home / "run.json", fresh_home / "manifest.json"
    run = ask_contract._read_json_object(run_json)
    if run is None:
        return _refuse(f"{run_id} has no run.json")
    if not manifest.is_file():
        return _refuse(f"{run_id} has no manifest.json; adopt after the stage")
    why = _predecessor_refusal(root, run_id, predecessor, pred_home)
    if why:
        return _refuse(why)
    reply = {"ok": True, "run_id": run_id, "predecessor": predecessor, APPLIED: False, ADOPTED: [], REFUSED: {}}
    prior = run.get(ADOPTED_FROM_FIELD)
    if isinstance(prior, list) and predecessor in prior:
        return reply
    credited, refused = _credit(root, kept_fingerprints(pred_home))
    reply[REFUSED] = refused
    if not credited:
        return reply
    try:
        _merge_baseline(root, manifest, credited)
        reply[APPLIED] = _record_adoption(root, run_json, predecessor)
    except (OSError, LockTimeout, MutateAbort) as exc:
        return _refuse(f"adoption of {predecessor} not recorded: {exc}")
    declare_write(manifest)
    declare_write(run_json)
    if reply[APPLIED]:
        reply[ADOPTED] = sorted(credited)
    return reply


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.adopt_predecessor_work": credit a halted or superseded predecessor's kept work to a fresh run.

    Params `{run_id, predecessor}`; reply as the module docstring."""
    from coordinator_core.lifecycle import main_worktree_root

    if repo_root is None:
        return {"error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    run_id, predecessor = params.get("run_id"), params.get("predecessor")
    if not isinstance(run_id, str) or not run_id:
        return {"error": f"{OP_NAME} requires run_id"}
    if not isinstance(predecessor, str) or not predecessor:
        return {"error": f"{OP_NAME} requires predecessor"}
    return adopt_predecessor_work(main_worktree_root(Path(repo_root)), run_id, predecessor)
