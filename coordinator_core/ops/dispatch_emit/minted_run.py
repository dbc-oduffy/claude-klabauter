"""``dispatch.minted_run``: start or abandon a run left at stage ``minted``.

Params: ``{run_id: str, action: "start" | "abandon", dry_run?: bool}``.
Reply: ``{ok, run_id, action, fire_args | null, landed_sha | null, refusal | null}``; a refusal
carries ``ok: false`` and a one-line ``refusal`` naming the cause. ``start`` returns ``script_path``, a
stage-loop start script under ``scratch/warp``: fire that path with no args (it carries
``repoRoot``/``runToken`` and follows ``stage_pending`` into the staged twin); ``fire_args`` is null and
``run_script`` names the run's own script, which is never fired directly. A run script that holds its own
``workflow()`` reader is the exception: ``script_path`` is that script and ``fire_args`` its args, fired
top-level so the reader is the only nesting level. ``abandon`` writes the halt
marker, discards the run token and lands the record through ``abandoned_runs`` (``run_ids=[run_id]``).
The reap holds a run younger than its quiet window, so a fresh abandon replies ``landed_sha: null`` and
``note`` names the reap's skip reason; the halt marker still blocks any start.

``abandon`` on an id with no run dir (announced, never written) only discards its token and replies
ok; with no token either it refuses ``run-not-found``. ``start`` on it always refuses: with a minted token it
refuses ``never-created`` (reply ``cause: never_created``) naming the ``dispatch.emit`` refire and the abandon;
with no token, ``run-not-found``.

A run is startable or abandonable only while it is minted-only (``halted_run._is_shell``: no halt
marker, progress record absent or at ``minted``); a run past ``minted`` is refused. In-process file
reads only on the refusal path; ``start`` waits up to ``SCRIPT_WAIT_SECS`` for a script an emit is still writing,
mints and escrows a run token when none is escrowed, and also writes one ``scratch/warp/defer-*.workflow.mjs`` file
(``dry_run`` does not apply to it) and appends the caller's session to run-stage ``driver_session_ids``
when one resolves; no git spawn.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import (
    _is_shell, is_unstarted_mint_halt, revive_unstarted_supersede, write_halt_marker,
)
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX, staged_twin_rel

OP_NAME = "dispatch.minted_run"
ACTION_START = "start"
ACTION_ABANDON = "abandon"
ACTIONS = (ACTION_START, ACTION_ABANDON)

REFUSAL_BAD_PARAMS = "bad-params"
REFUSAL_NO_REPO = "no-repo-root"
REFUSAL_NOT_FOUND = "run-not-found"
REFUSAL_NEVER_CREATED = "never-created"
CAUSE_NEVER_CREATED = "never_created"
REFUSAL_PAST_MINTED = "run-past-minted"
REFUSAL_NO_SCRIPT = "script-missing"
REFUSAL_REAP_FAILED = "reap-failed"
REFUSAL_CHILD_LIVE = "child-run-live"
ABANDON_REASON = "minted-run-abandoned-by-em"


def reply(run_id: str, action: Optional[str], *, fire_args: Optional[dict] = None,
          landed_sha: Optional[str] = None, refusal: Optional[str] = None,
          script_path: Optional[str] = None, note: Optional[str] = None) -> dict:
    """The one reply shape; ``ok`` is false exactly when ``refusal`` is set. ``script_path`` and
    ``note`` appear only when given."""
    out = {
        "ok": refusal is None,
        "run_id": run_id,
        "action": action,
        "fire_args": fire_args,
        "landed_sha": landed_sha,
        "refusal": refusal,
    }
    if script_path is not None:
        out["script_path"] = script_path
    if note is not None:
        out["note"] = note
    return out


def is_minted_only(run_dir: Path) -> bool:
    """True when ``run_dir`` exists and holds no work past ``minted`` (the halted_run shell predicate)."""
    return run_dir.is_dir() and _is_shell(run_dir)


def _never_staged(run_dir: Path) -> bool:
    """True when the run's progress record never left ``minted`` (a ``halted`` stamp with no ``staged_at`` is
    that same state); an absent record counts. Unreadable reads False."""
    try:
        stage = json.loads((run_dir / "run-stage.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and "staged_at" not in stage and stage.get("stage") in (None, "minted", "halted")


SCRIPT_WAIT_SECS = 5.0
SCRIPT_POLL_SECS = 0.1


def _wait_for_script(script: Path) -> bool:
    """True once ``script`` is a file, polling up to ``SCRIPT_WAIT_SECS``: an emit still writing lands it within seconds."""
    deadline = time.monotonic() + SCRIPT_WAIT_SECS
    while not script.is_file():
        if time.monotonic() >= deadline:
            return False
        time.sleep(SCRIPT_POLL_SECS)
    return True


def _start_token(repo_root: Path, run_id: str, script_rel: str) -> Optional[str]:
    """The run's escrowed token, else a fresh one bound to ``script_rel`` and escrowed; None when no mint can bind.
    Trap: the commit leg presents ``script_rel`` to ``run_token.admits``, so the mint binds to the run script path;
    ``carry_prior`` keeps a token a running script already holds."""
    from coordinator_core.ops.dispatch_emit import run_token

    token = run_token.escrowed(repo_root, run_id)
    if token:
        return token
    prior = run_token._read_rec(repo_root, run_id) or {}
    try:
        token = run_token.mint(
            repo_root, run_id, script_rel, session_id=prior.get("session_id"),
            sizing_rel=prior.get("sizing_rel"), carry_prior=True,
        )
    except (OSError, ValueError):
        return None
    run_token.escrow(repo_root, run_id, token)
    return token


def start_args(repo_root: Path, run_id: str, *, wait: bool = True, mint: bool = True) -> dict:
    """``fire_args`` and ``script_path`` of the run's own script, waiting up to ``SCRIPT_WAIT_SECS`` for an emit still
    writing it. Carries ``runToken``: the escrowed token, else one minted and escrowed here.
    A predicate caller passes ``wait=False, mint=False``: no sleep, no token write, the escrowed token only."""
    from coordinator_core.ops.dispatch_emit import run_token

    script_rel = f"{RUN_DIR_ROOT}/{run_id}{RUN_SCRIPT_SUFFIX}"
    present = _wait_for_script(repo_root / script_rel) if wait else (repo_root / script_rel).is_file()
    if not present:
        return reply(run_id, ACTION_START, refusal=(
            f"{REFUSAL_NO_SCRIPT}: no {script_rel} after {SCRIPT_WAIT_SECS:g}s; "
            f"retry the start if the emit is still running, else abandon: {OP_NAME} "
            f'{{"run_id":"{run_id}","action":"abandon"}}'
        ))
    fire_args = {"repoRoot": repo_root.as_posix()}
    token = _start_token(repo_root, run_id, script_rel) if mint else run_token.escrowed(repo_root, run_id)
    if token:
        fire_args["runToken"] = token
    return reply(run_id, ACTION_START, fire_args=fire_args, script_path=script_rel)


def holds_workflow_reader(repo_root: Path, script_rel: str) -> bool:
    """True when the script calls ``workflow()`` itself (an emit without ``launcher_child``: its stage reader).
    Trap: a launcher child holds no ``workflow(`` bytes, so only a script that does must be started top-level;
    a wrapper starting it as a child would put that call at a second nesting level."""
    try:
        return "workflow(" in (repo_root / script_rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def reader_script_rel(repo_root: Path, run_id: str, script_rel: str) -> Optional[str]:
    """Repo-relative script that must be fired top-level because it holds a ``workflow()`` reader: the run script itself,
    else the newest staged twin of the run that does; None when neither holds one.
    Trap: a reader-holding twin behind a reader-free run script is reached by firing the twin with the run's args,
    never the run script, which would end at ``stage_pending`` with nothing starting the twin."""
    if holds_workflow_reader(repo_root, script_rel):
        return script_rel
    try:
        twins = sorted(repo_root.glob(staged_twin_rel(run_id, "*")), key=lambda t: t.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for twin in twins:
        rel = twin.relative_to(repo_root).as_posix()
        if holds_workflow_reader(repo_root, rel):
            return rel
    return None


def _record_caller_as_driver(repo_root: Path, run_id: str) -> None:
    """Append the calling session to the run's ``driver_session_ids`` so the staged-stall scan reads its journals.
    Trap: no resolved session means no write (a warm engine must never record its spawner's id); an absent
    run-stage record or a failed write never fails the start."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_driver_session_at
    from coordinator_core.session.core import carried_session_id, in_warm_served_request, resolve_session_id

    session_id = carried_session_id() or ("" if in_warm_served_request() else resolve_session_id())
    if not session_id:
        return
    try:
        record_driver_session_at(repo_root / RECORD_DIR_ROOT / run_id, session_id)
    except (ValueError, OSError):
        return


def _start(repo_root: Path, run_id: str) -> dict:
    """The stage-loop start script for the run: ``script_path`` is that script, ``run_script`` the run's own.
    A run whose script (or staged twin) holds its own ``workflow()`` reader is the start script itself, fired with ``fire_args``.
    Trap: firing a launcher-child ``run_script`` directly ends at ``stage_pending`` with nothing starting the staged twin."""
    from coordinator_core.hooks.submit_engine_ask import write_direct_start_script

    args = start_args(repo_root, run_id)
    if not args["ok"]:
        return args
    _record_caller_as_driver(repo_root, run_id)
    reader = reader_script_rel(repo_root, run_id, args["script_path"])
    if reader is not None:
        out = reply(run_id, ACTION_START, fire_args=args["fire_args"], script_path=reader)
        out["run_script"] = args["script_path"]
        return out
    script = write_direct_start_script(repo_root, run_id, args["script_path"], args["fire_args"])
    start_rel = Path(script).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    out = reply(run_id, ACTION_START, script_path=start_rel)
    out["run_script"] = args["script_path"]
    return out


def live_chained_child(repo_root: Path, run_id: str) -> Optional[str]:
    """Run id of a live run whose ``run.json`` ``chain_parent`` is ``run_id``, else None. Trap: only
    warp dirs dated on or after the ask are opened: a chained child's id need not embed the ask's hash
    (6 of 15 real children do not), but it is minted after the ask."""
    from coordinator_core.session import record_homes
    from coordinator_core.warm.live_runs import run_owner_holds

    stamp = run_id.split("-")[1][:8] if run_id.count("-") >= 2 else ""
    since = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if stamp.isdigit() and len(stamp) == 8 else ""
    try:
        names = sorted(e.name for e in os.scandir(record_homes.home_dir(str(repo_root), "warp")) if e.name != run_id and (not since or e.name[5:15] >= since))
    except OSError:
        return None
    for name in names:
        try:
            data = json.loads((repo_root / RECORD_DIR_ROOT / name / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("chain_parent") == run_id and run_owner_holds(repo_root, name):
            return name
    return None


def _abandon(repo_root: Path, run_id: str, dry_run: bool) -> dict:
    """Halt marker, token discard, then ``abandoned_runs`` reaps just this run. Trap: the marker lands
    before the reap, so a reap that skips (quiet window) still leaves the run unstartable. A live
    chained child refuses first, dry run included: the engine cannot stop its Workflow task."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs, run_token

    child = live_chained_child(repo_root, run_id)
    if child:
        return reply(run_id, ACTION_ABANDON, refusal=f"{REFUSAL_CHILD_LIVE}: chained child run {child} is running; stop its Workflow task, then abandon")
    if dry_run:
        return reply(run_id, ACTION_ABANDON, note="dry-run: no marker written")
    write_halt_marker(repo_root / RECORD_DIR_ROOT / run_id, halted="abandoned", sizing_rel=None, reason=ABANDON_REASON)
    run_token.discard(repo_root, run_id)
    reaped = abandoned_runs._handler({"run_ids": [run_id]}, repo_root)
    if not isinstance(reaped, dict) or reaped.get("error") or reaped.get("ok") is False:
        detail = reaped.get("error") if isinstance(reaped, dict) else None
        return reply(run_id, ACTION_ABANDON, refusal=f"{REFUSAL_REAP_FAILED}: {detail or 'reap returned no result'}")
    sha = reaped.get("committed_sha")
    skip = next((x for x in reaped.get("skipped") or () if isinstance(x, dict) and x.get("run_id") == run_id), None)
    note = f"reap skipped: {skip.get('reason')}" if skip and not sha else None
    return reply(run_id, ACTION_ABANDON, landed_sha=sha, note=note)


def _clear_dirless_mint(repo_root: Path, run_id: str, dry_run: bool) -> Optional[dict]:
    """Abandon of an id whose token record exists but whose run dir never landed: discard the token
    (and its escrow); no marker, no reap, nothing to commit. None when no token record exists."""
    from coordinator_core.ops.dispatch_emit import run_token

    if run_token._read_rec(repo_root, run_id) is None and run_token.escrowed(repo_root, run_id) is None:
        return None
    if dry_run:
        return reply(run_id, ACTION_ABANDON, note="dry-run: token not discarded")
    run_token.discard(repo_root, run_id)
    return reply(run_id, ACTION_ABANDON, note=f"no {RECORD_DIR_ROOT}/{run_id}: stray mint token discarded")


def _minted_token_exists(repo_root: Path, run_id: str) -> bool:
    """True when the id was minted (a token record or its escrow exists) though no run dir landed."""
    from coordinator_core.ops.dispatch_emit import run_token

    return run_token._read_rec(repo_root, run_id) is not None or run_token.escrowed(repo_root, run_id) is not None


def _never_created(run_id: str) -> dict:
    """Typed refusal for a minted id whose emit never created its run dir: names the refire and the abandon.
    Trap: the emit may still be in flight; a start retried once the dir lands succeeds."""
    out = reply(run_id, ACTION_START, refusal=(
        f"{REFUSAL_NEVER_CREATED}: {run_id} was minted but its emit never created {RECORD_DIR_ROOT}/{run_id}; "
        f'refire the ask with dispatch.emit {{"preminted_run_id": "{run_id}", ...the original ask params}} '
        f'(or retry start if the emit is still running), or discard it: {OP_NAME} {{"run_id":"{run_id}","action":"abandon"}}'
    ))
    out["cause"] = CAUSE_NEVER_CREATED
    return out


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.minted_run": start or abandon one minted-only run."""
    p = params if isinstance(params, dict) else {}
    run_id = p.get("run_id")
    action = p.get("action")
    shown = run_id if isinstance(run_id, str) else ""
    if repo_root is None:
        return reply(shown, action if action in ACTIONS else None, refusal=f"{REFUSAL_NO_REPO}: {OP_NAME} requires repo_root")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return reply(shown, None, refusal=f'{REFUSAL_BAD_PARAMS}: run_id must be a run id; pass {{"run_id": "<id>", "action": "start|abandon"}}')
    if action not in ACTIONS:
        return reply(run_id, None, refusal=f"{REFUSAL_BAD_PARAMS}: action must be one of {', '.join(ACTIONS)}")
    # Trap: the engine keys this op `common_dir`, so repo_root arrives as `<repo>/.git`, not the worktree root.
    root = main_worktree_root(Path(repo_root))
    run_dir = root / RECORD_DIR_ROOT / run_id
    if not run_dir.is_dir():
        if action == ACTION_ABANDON:
            cleared = _clear_dirless_mint(root, run_id, bool(p.get("dry_run")))
            if cleared is not None:
                return cleared
        if action == ACTION_START and _minted_token_exists(root, run_id):
            return _never_created(run_id)
        return reply(run_id, action, refusal=f"{REFUSAL_NOT_FOUND}: no {RECORD_DIR_ROOT}/{run_id}")
    # A refire close whose successor never launched is voided here, so the prior run is not stranded past minted.
    revived = action == ACTION_START and _never_staged(run_dir) and revive_unstarted_supersede(root, run_id)
    if not (revived or is_minted_only(run_dir) or (action == ACTION_START and is_unstarted_mint_halt(run_dir))):
        return reply(run_id, action, refusal=f"{REFUSAL_PAST_MINTED}: {run_id} is past stage minted or halt-marked")
    if action == ACTION_START:
        return _start(root, run_id)
    return _abandon(root, run_id, bool(p.get("dry_run")))
