"""``dispatch.minted_run``: start or abandon a run left at stage ``minted``.

Params: ``{run_id: str, action: "start" | "abandon", dry_run?: bool}``.
Reply: ``{ok, run_id, action, fire_args | null, landed_sha | null, refusal | null}``; a refusal
carries ``ok: false`` and a one-line ``refusal`` naming the cause. ``start`` returns the
``fire_args`` of the Workflow start plus ``script_path`` (the run's script under ``scratch/warp``;
``runToken`` rides ``fire_args`` only while the run's token is escrowed); ``abandon`` writes the halt
marker, discards the run token and lands the record through ``abandoned_runs`` (``run_ids=[run_id]``).
The reap holds a run younger than its quiet window, so a fresh abandon replies ``landed_sha: null`` and
``note`` names the reap's skip reason; the halt marker still blocks any start.

``abandon`` on an id with no run dir (announced, never written) only discards its token and replies
ok; with no token either it refuses ``run-not-found``, and ``start`` on it always refuses.

A run is startable or abandonable only while it is minted-only (``halted_run._is_shell``: no halt
marker, progress record absent or at ``minted``); a run past ``minted`` is refused. In-process file
reads only; no git spawn on the refusal path or on ``start``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import _is_shell, write_halt_marker
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX

OP_NAME = "dispatch.minted_run"
ACTION_START = "start"
ACTION_ABANDON = "abandon"
ACTIONS = (ACTION_START, ACTION_ABANDON)

REFUSAL_BAD_PARAMS = "bad-params"
REFUSAL_NO_REPO = "no-repo-root"
REFUSAL_NOT_FOUND = "run-not-found"
REFUSAL_PAST_MINTED = "run-past-minted"
REFUSAL_NO_SCRIPT = "script-missing"
REFUSAL_REAP_FAILED = "reap-failed"
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


def _start(repo_root: Path, run_id: str) -> dict:
    """``fire_args`` for the run's script. Reads only: the script file's existence and the token escrow."""
    from coordinator_core.ops.dispatch_emit import run_token

    script_rel = f"{RUN_DIR_ROOT}/{run_id}{RUN_SCRIPT_SUFFIX}"
    if not (repo_root / script_rel).is_file():
        return reply(run_id, ACTION_START, refusal=f"{REFUSAL_NO_SCRIPT}: no {script_rel}; abandon the run instead")
    fire_args = {"repoRoot": repo_root.as_posix()}
    token = run_token.escrowed(repo_root, run_id)
    if token:
        fire_args["runToken"] = token
    return reply(run_id, ACTION_START, fire_args=fire_args, script_path=script_rel)


def _abandon(repo_root: Path, run_id: str, dry_run: bool) -> dict:
    """Halt marker, token discard, then ``abandoned_runs`` reaps just this run. Trap: the marker lands
    before the reap, so a reap that skips (quiet window) still leaves the run unstartable."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs, run_token

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
        return reply(run_id, action, refusal=f"{REFUSAL_NOT_FOUND}: no {RECORD_DIR_ROOT}/{run_id}")
    if not is_minted_only(run_dir):
        return reply(run_id, action, refusal=f"{REFUSAL_PAST_MINTED}: {run_id} is past stage minted or halt-marked")
    if action == ACTION_START:
        return _start(root, run_id)
    return _abandon(root, run_id, bool(p.get("dry_run")))
