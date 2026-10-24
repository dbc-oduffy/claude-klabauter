"""``dispatch.record_halted_run_landed``: records a halted run as landed at a verified commit.

Params: ``{run_id, sha}``; ``sha`` is a lowercase hex commit sha, full or abbreviated to at least 4
characters; an abbreviation must name exactly one commit in the first-parent window and the reply and
recorded marker carry the full 40-hex sha. The run's sibling status must read
``halted`` (or already ``landed`` at the same sha) and ``sha`` must be a commit on HEAD's first-parent
chain within ``ANCESTRY_WINDOW``. A solo run's marker may also read ``resumed`` when no live run's
ledger names it as ``resumed_from``. The write and the release of ``after`` successors are
``run_close.sibling_release``'s; this op never writes the sibling status file itself. A solo run has
no sibling status: its halt marker (``halted_run``) is read and rewritten to ``landed`` instead, and
the reply names that tracked path as ``recorded``; a sibling run's still-``halted`` marker is flipped
the same way. The op never commits: commit existence and ancestry are in-process object reads, and
the close legs (``CLOSE_LEGS``) are queued in a close-job record paid by one detached launch
(``_launch``), never inline. The record is written only when the marker names a ``plan`` and no
close-job record exists yet.
Reply: ``{ok, run_id, sha, written, siblings?, recorded?, close, withheld}`` or ``{ok: false, error}``;
``withheld`` is the marker's ``blocking`` register-row ids.
When the halt marker records withheld paths (``run_artifacts.withheld``), ``sha`` must have changed
every one of them versus its first parent, so recording landed cannot release the withheld-commit
guard for a commit that never carried them; a later follow-up commit that changed them qualifies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.record_halted_run_landed"

#: First-parent commits from HEAD searched for the sha; matches rollback_check's default window.
ANCESTRY_WINDOW = 1000

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SHA_PREFIX = re.compile(r"[0-9a-f]{4,40}")


#: close_background.BACKGROUND_LEGS order; review-stamp precedes plan-reconciliation, which fails
#: "no review stamp minted" without it.
CLOSE_LEGS = (
    "review-stamp", "coded-stamp", "sizing-ship", "plan-reconciliation", "deliverable-cascade", "close-commit",
)
_CLOSE_JOB_WINDOW_MINUTES = 5
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _launch(root: Path, run_id: str) -> bool:
    """Start the detached close job for *run_id*; False when it did not start."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    script = str(engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py")
    params = json.dumps({"claimed_batons": [], "source_memo": None})
    return spawn_detached(
        str(engine), script, [run_id, "--root", str(root), "--params", params], site_free=True,
    )


def _withheld(marker: dict) -> list:
    """The marker's ``blocking`` register-row ids that are strings."""
    blocking = marker.get("blocking")
    return [b for b in blocking if isinstance(b, str)] if isinstance(blocking, list) else []


def _review_record(root: Path, run_id: str, session_id: Optional[str]) -> Optional[str]:
    """Repo-relative posix path of the run's review sidecar, or None when none resolves.

    Candidates, first existing file wins: ``inline_review.integration.sidecar`` (relative, posix, no
    ``..``, under *root*), then ``inline_review.integration_stem`` (a bare stem) at
    ``.coordinator-local/subagent-share/<session_id>/<stem>.md``. The stem carries no session, and
    *session_id* is the session calling this op, which need not be the one whose wave review wrote the
    sidecar; a mismatch resolves None, and the caller settles ``no-review-outcome``. That is the honest
    outcome, not a bug. One json read, at most two stats.
    """
    from coordinator_core.ops.dispatch_emit import ask_contract

    outcome = ask_contract.read_run_outcome(root, run_id)
    inline_review = outcome.get("inline_review") if outcome else None
    if not isinstance(inline_review, dict):
        return None
    candidates: list[Path] = []
    integration = inline_review.get("integration")
    sidecar = integration.get("sidecar") if isinstance(integration, dict) else None
    if isinstance(sidecar, str) and sidecar and "\\" not in sidecar:
        parts = sidecar.split("/")
        if not sidecar.startswith("/") and ".." not in parts and not re.match(r"[A-Za-z]:", sidecar):
            candidates.append(root / sidecar)
    stem = inline_review.get("integration_stem")
    if (
        session_id and isinstance(stem, str) and stem and stem != ".." and "/" not in stem and "\\" not in stem
    ):
        candidates.append(root / ".coordinator-local" / "subagent-share" / session_id / f"{stem}.md")
    for path in candidates:
        try:
            rel = path.resolve().relative_to(root.resolve())
            if path.is_file():
                return rel.as_posix()
        except (OSError, ValueError):
            continue
    return None


def _queue_close(root: Path, run_id: str, sha: str, marker: Optional[dict], session_id: Optional[str]) -> dict:
    """The reply's ``close`` and ``withheld`` for a run just recorded landed at *sha*.

    Writes the close-job record and launches it once; a failed write or launch reports
    ``launched: false`` and never fails the op. With a resolvable review record the review-stamp leg
    is queued and handed it; otherwise that leg is settled ``review skipped: no-review-outcome``.
    """
    from datetime import datetime, timedelta

    from coordinator_core.ops.dispatch_emit import run_close

    marker = marker if isinstance(marker, dict) else {}
    withheld = _withheld(marker)
    plan = marker.get("plan")
    if not isinstance(plan, str) or not plan:
        return {"close": {"status": "not-applicable", "detail": "halt record names no plan"}, "withheld": withheld}
    base = marker.get("run_base_sha")
    deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
    try:
        record = _review_record(root, run_id, session_id)
        if record is not None:
            queued, settled, inputs = CLOSE_LEGS, None, {"review_record": record}
        else:
            queued = tuple(leg for leg in CLOSE_LEGS if leg != "review-stamp")
            settled = {"review-stamp": run_close.review_skipped("no-review-outcome")}
            inputs = None
        job = run_close.write_close_job(
            root, run_id, queued, deadline,
            session_id=session_id,
            plan_rel=plan,
            terminal_sha=sha,
            run_base_sha=base if isinstance(base, str) and _FULL_SHA.fullmatch(base) else None,
            settled=settled,
            inputs=inputs,
        )
    except Exception as exc:  # noqa: BLE001 -- the landed write stands; the close reports itself
        return {
            "close": {"job": None, "launched": False, "queued": list(CLOSE_LEGS), "error": f"{type(exc).__name__}: {exc}"},
            "withheld": withheld,
        }
    launch_error = None
    try:
        launched = bool(_launch(root, run_id))
    except Exception as exc:  # noqa: BLE001 -- the job record stands; the reply reports launched: false
        launched = False
        launch_error = f"{type(exc).__name__}: {exc}"
    close = {"job": job, "launched": launched, "queued": list(queued)}
    if launch_error is not None:
        close["error"] = launch_error
    return {"close": close, "withheld": withheld}


def _close_landed(root: Path, run_id: str, sha: str, session_id: Optional[str]) -> dict:
    """Flip a still-``halted`` or ``resumed`` marker to landed, then queue the close unless a job record exists."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, run_close

    out: dict = {}
    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError:
        return {"close": {"status": "not-applicable", "detail": "no halt record"}, "withheld": []}
    marker = halted_run._read(record_dir)
    if marker is not None and marker.get("status") in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
        if halted_run.mark_landed(record_dir, sha):
            out["recorded"] = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    if marker is None:
        return {**out, "close": {"status": "not-applicable", "detail": "no halt record"}, "withheld": []}
    if run_close.resolve_close_job_stem(root, run_id) is not None:
        return {**out, "close": {"status": "not-applicable", "detail": "close job already recorded"}, "withheld": _withheld(marker)}
    return {**out, **_queue_close(root, run_id, sha, marker, session_id)}


def _resolve_prefix(root: Path, prefix: str) -> tuple[Optional[str], Optional[str]]:
    """``(full_sha, None)`` when *prefix* names exactly one commit in the first-parent window, else ``(None, refusal)``."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import _first_parent_ancestors

    head = head_sha(root)
    if not head:
        return None, "HEAD is unreadable; retry"
    common = resolve_git_common_dir(root)
    matches = {s for _, s in _first_parent_ancestors(common, head, ANCESTRY_WINDOW) if s.startswith(prefix)}
    if len(matches) > 1:
        return None, f"sha prefix {prefix} is ambiguous ({len(matches)} commits match); pass more characters"
    if not matches:
        return None, (
            f"sha prefix {prefix} matches no commit on HEAD's first-parent chain within "
            f"{ANCESTRY_WINDOW} commits; pass the commit that landed the work"
        )
    return next(iter(matches)), None


def _reachable(root: Path, sha: str) -> Optional[str]:
    """None when *sha* is a commit on HEAD's first-parent chain, else the refusal text."""
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import _first_parent_ancestors

    common = resolve_git_common_dir(root)
    if commit_meta(common, sha) is None:
        return f"{sha} is not a commit in this repo; pass the landing commit's full sha"
    head = head_sha(root)
    if not head:
        return "HEAD is unreadable; retry"
    if not any(s == sha for _, s in _first_parent_ancestors(common, head, ANCESTRY_WINDOW)):
        return (
            f"{sha} is not on HEAD's first-parent chain within {ANCESTRY_WINDOW} commits; "
            "pass the commit that landed the work"
        )
    return None


def _live_successor(root: Path, run_id: str) -> Optional[str]:
    """The id of a live run whose ledger resumes *run_id*, else None; scans both run record roots once, reading one ledger per run dir (cost grows with the run-dir count)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    for base in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        try:
            candidates = sorted(p for p in (root / base).iterdir() if p.is_dir() and p.name != run_id)
        except OSError:
            continue
        for cand in candidates:
            ledger = rl.read_run(cand)
            if (
                ledger and ledger.get(rl.RESUMED_FROM_FIELD) == run_id
                and run_token.is_live(root, cand.name) and not _terminal(cand)
            ):
                return cand.name
    return None


def _terminal(cand: Path) -> bool:
    """True when the run dir records the run landed or halted: its token's TTL is not liveness then."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    marker = halted_run._read(cand)
    if marker is not None and marker.get("status") == halted_run.STATUS_LANDED:
        return True
    try:
        stage = json.loads((cand / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") in ("halted", "landed")


def _carries_withheld(root: Path, run_id: str, sha: str) -> Optional[str]:
    """None when *sha* changed every path the run's halt marker withholds versus its first parent, else the refusal text.

    A run withholding nothing, or with no marker, passes. An unreadable commit or tree refuses.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import read_tree_spine
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        withheld = sorted(halted_run.halted_withheld(ask_contract.record_dir(root, run_id)))
    except ValueError:
        return None
    if not withheld:
        return None
    common = resolve_git_common_dir(root)
    meta = commit_meta(common, sha)
    parents = meta.get("parents") if meta else None
    parent_meta = commit_meta(common, parents[0]) if parents else None
    if meta is None or not meta.get("tree") or (parents and (parent_meta is None or not parent_meta.get("tree"))):
        return f"{sha} or its parent is unreadable; retry"
    after = read_tree_spine(root, withheld, root_tree_sha=meta["tree"])
    before = read_tree_spine(root, withheld, root_tree_sha=parent_meta["tree"]) if parent_meta else {}
    if after is None or before is None:
        return f"{sha} tree is unreadable; retry"

    def _entry(spine: dict, path: str):
        return spine.get(path.rpartition("/")[0], {}).get(path.rpartition("/")[2])

    unchanged = [p for p in withheld if _entry(after, p) == _entry(before, p)]
    if unchanged:
        more = f" (+{len(unchanged) - 1} more)" if len(unchanged) > 1 else ""
        return (
            f"{sha} did not change withheld {unchanged[0]}{more}; "
            "pass the commit that landed the run's withheld paths"
        )
    return None


def _record_solo(root: Path, run_id: str, sha: str, abbreviated: bool, session_id: Optional[str] = None) -> dict:
    """Solo run (no sibling status): record landed on its halt marker; the caller commits ``recorded``."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError as exc:
        return _refuse(str(exc))
    marker = halted_run._read(record_dir)
    if marker is None:
        return _refuse(f"{run_id} has no sibling status or halt record; it never ran")
    status = marker.get("status")
    if status == halted_run.STATUS_LANDED:
        landed = marker.get("sha")
        if isinstance(landed, str) and landed.startswith(sha):
            return {
                "ok": True, "run_id": run_id, "sha": landed, "written": False,
                **_close_landed(root, run_id, landed, session_id),
            }
        return _refuse(f"{run_id} already landed at {marker.get('sha')}; nothing to record")
    if status == halted_run.STATUS_RESUMED:
        successor = _live_successor(root, run_id)
        if successor:
            return _refuse(f"{run_id} is resumed and its successor {successor} is still live; wait for it or stop it")
    elif status != halted_run.STATUS_HALTED:
        return _refuse(f"{run_id} is {status}; only a halted or resumed run is recorded landed")
    if abbreviated:
        sha, problem = _resolve_prefix(root, sha)
        if problem:
            return _refuse(problem)
    problem = _reachable(root, sha) or _carries_withheld(root, run_id, sha)
    if problem:
        return _refuse(problem)
    if not halted_run.mark_landed(record_dir, sha):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    recorded = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    return {
        "ok": True, "run_id": run_id, "sha": sha, "written": True, "recorded": recorded,
        **_queue_close(root, run_id, sha, marker, session_id),
    }


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.record_halted_run_landed": verify the sha, then close the run as landed."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.ops.dispatch_emit import run_close

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_id, sha = params.get("run_id"), params.get("sha")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _refuse(f"{OP_NAME} requires a safe string run_id")
    if not isinstance(sha, str) or not _SHA_PREFIX.fullmatch(sha):
        return _refuse(f"{OP_NAME} requires a lowercase hex sha of 4 to 40 characters")
    abbreviated = not _FULL_SHA.fullmatch(sha)

    sid = params.get("session_id")
    session_id = sid if isinstance(sid, str) and _UUID.fullmatch(sid) else None

    root = main_worktree_root(Path(repo_root))
    try:
        prior = json.loads((root / contract.status_rel(run_id)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _record_solo(root, run_id, sha, abbreviated, session_id)
    except (OSError, ValueError):
        return _refuse(f"{run_id} sibling status is unreadable")
    status = prior.get("status") if isinstance(prior, dict) else None
    if status == contract.STATUS_LANDED:
        landed = prior.get("sha")
        if isinstance(landed, str) and landed.startswith(sha):
            return {
                "ok": True, "run_id": run_id, "sha": landed, "written": False,
                **_close_landed(root, run_id, landed, session_id),
            }
        return _refuse(f"{run_id} already landed at {prior.get('sha')}; nothing to record")
    if status != contract.STATUS_HALTED:
        return _refuse(f"{run_id} is {status}, not halted; only a halted run is recorded landed")

    if abbreviated:
        sha, problem = _resolve_prefix(root, sha)
        if problem:
            return _refuse(problem)
    problem = _reachable(root, sha) or _carries_withheld(root, run_id, sha)
    if problem:
        return _refuse(problem)
    reply = run_close.sibling_release(root, run_id, sha)
    out: dict = {"ok": True, "run_id": run_id, "sha": sha, "written": True}
    if reply is not None:
        out["siblings"] = reply
    out.update(_close_landed(root, run_id, sha, session_id))
    return out
