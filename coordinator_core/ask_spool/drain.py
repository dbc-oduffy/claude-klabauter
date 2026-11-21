"""Drain the engine-ask submit spool: claim each entry, mint its run once, record a notice.

Contract (docs/reference/ask-spool.md): `run(repo_root) -> {minted, skipped, failed, notified, coalesced, withdrawn}`.
An entry reaches `.minted` only with a run id: found already (`skipped`, no new mint) or minted
through `submit_engine_ask.evaluate` on the stored payload, and only when `RUN_DIR_ROOT/<run_id>`
exists. A failed mint, or a run id with no run dir, leaves the entry pending with a bumped
`attempts`; an entry is never deleted.

Traps:
- The original emit may have minted before the spool existed (post-delivery spool) or may still
  be finishing after a -32004. Such a run carries no spool_id, so it is matched by the
  submission record's session_id + tool_use_id, else by a lone un-submitted ask token of the
  session near the spool time. -32004 is indeterminate, never not-started.
- After the ask entries, `run` drains the queued remote-engine failover retries
  (`remote_engine.background.drain_pending`, a bounded batch); they are not part of the counts.
- The notice is written before `.minted`: a crash between them re-notices, never loses one.
- Last, `run` sweeps `.minted` entries (`_sweep_minted_without_dispatch`): a run still at stage `minted`
  `MINTED_DISPATCH_DEADLINE_SECONDS` after its mint, with no activity on it or any chained child, is halted
  `minted_without_dispatch` and its submitting session noticed. Halts are not part of the counts.
  Then `_sweep_stalled_launcher_children` halts a launcher child (no `.minted` entry) at `minted`/`executing` with no run dir.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Optional

from coordinator_core.ask_spool import notice, record

STALE_CLAIM_S = 300.0
NOTICES_DIR = notice.NOTICES_DIRNAME
_SUBMISSION = "submission.json"
_LOOKBACK_S = 120.0
_LOOKAHEAD_S = 600.0
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ASK_RUN = re.compile(r"\bask-\d{8}T\d{6}-[0-9a-f]{6}\b")
_FAILURE_NOTICE_EVERY = 10
#: Also the child-activity bound of the sweep. Deliberately tighter than `warp_run_deliverable.MINTED_STALE_SECONDS`
#: (the reaper's bound, 1800): the sweep notices the submitter, the reaper only releases peer claims.
MINTED_DISPATCH_DEADLINE_SECONDS = 600
MINTED_SWEEP_LOOKBACK_SECONDS = 24 * 3600
MINTED_WITHOUT_DISPATCH = "minted_without_dispatch"
#: Fires the drain makes for one minted run; the fire registry dedupes by script digest, so this bounds retries, not duplicates.
FIRE_ATTEMPT_CAP = 3

Evaluate = Callable[[dict], dict]


class _Refused(Exception):
    pass


class _Ambiguous(Exception):
    pass


def _submissions(root: Path, floor: float) -> Iterator[tuple[str, dict]]:
    """(run_id, submission record) for run dirs touched since `floor`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    try:
        it = os.scandir(root / RUN_DIR_ROOT)
    except OSError:
        return
    with it:
        for e in it:
            try:
                if e.stat().st_mtime < floor:
                    continue
                data = json.loads((Path(e.path) / _SUBMISSION).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                yield e.name, data


def _lone_token_run(root: Path, session: str, spooled_at: float, taken: set) -> Optional[str]:
    """The session's one ask token minted near the spool time with no submission record; _Ambiguous on several."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.run_token import live_records

    found: list = []
    try:
        for rec in live_records(root):
            rid = rec.get("run_id")
            minted = float(rec.get("minted_at") or 0)
            if (
                rec.get("session_id") == session
                and rec.get("kind", "ask") == "ask"
                and spooled_at - _LOOKBACK_S <= minted <= spooled_at + _LOOKAHEAD_S
                and rid not in taken
                and not (root / RUN_DIR_ROOT / rid / _SUBMISSION).exists()
            ):
                found.append(rid)
    except (OSError, ValueError, TypeError):
        return None
    if len(found) > 1:
        raise _Ambiguous(f"{len(found)} unsubmitted ask runs of the session near the spool time: {', '.join(sorted(found))}")
    return found[0] if found else None


def _live_token_run(root: Path, key: Optional[str], taken: set) -> Optional[str]:
    """The run still holding a live token for the entry's sizing or `run:` key, whatever its dir age."""
    if not key:
        return None
    from coordinator_core.ops.dispatch_emit import run_token

    kind, _, target = key.partition(":")
    want = run_token.normalize_sizing_rel(root, target) if kind == "sizing" else target
    try:
        for rec in run_token.live_records(root):
            rid = rec.get("run_id")
            if rid in taken:
                continue
            if kind == "run" and rid == want:
                return rid
            named = rec.get("sizing_rel")
            if kind == "sizing" and isinstance(named, str) and run_token.normalize_sizing_rel(root, named) == want:
                return rid
    except (OSError, ValueError, TypeError):
        return None
    return None


_raw_ask_text = record.raw_ask_text


def _any_ask_run(root: Path, ask: str) -> Optional[str]:
    """A live token run recording the same normalized ask hash, whatever its session or age."""
    from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash
    from coordinator_core.ops.dispatch_emit.run_token import live_records

    want = normalized_ask_hash(ask)
    try:
        for rec in live_records(root):
            if rec.get("ask_sha256") == want and rec.get("run_id"):
                return rec["run_id"]
    except (OSError, ValueError, TypeError):
        return None
    return None


def _same_ask_run(root: Path, session: str, ask: Optional[str], spooled_at: float, taken: set) -> Optional[str]:
    """The session's live token run recording the same normalized ask hash near the spool time.

    The submission record carries no ask text; the run token's `ask_sha256` does."""
    if not ask or not session:
        return None
    from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash
    from coordinator_core.ops.dispatch_emit.run_token import live_records

    want = normalized_ask_hash(ask)
    try:
        for rec in live_records(root):
            minted = float(rec.get("minted_at") or 0)
            if (
                rec.get("ask_sha256") == want
                and rec.get("session_id") == session
                and rec.get("run_id") not in taken
                and spooled_at - _LOOKBACK_S <= minted <= spooled_at + _LOOKAHEAD_S
            ):
                return rec["run_id"]
    except (OSError, ValueError, TypeError):
        return None
    return None


def _find_run(
    root: Path,
    header: dict,
    tool_use_id: Optional[str],
    taken: set,
    sizing_key: Optional[str] = None,
    ask: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """(run_id, coalesced): the run already minted for this entry, else (None, False).

    Stamp, then the original dispatch key, then a submission naming the same sizing
    (`sizing_key` "sizing:<rel>" only; coalesced True), then a run whose live token names the
    entry's sizing or resumed run id (a refire reuses an older run dir the mtime floor skips;
    coalesced True), then, for a bare entry, a same-session run whose token records the same
    normalized `ask` (a retry under a new tool_use_id; coalesced True), then the session's lone token."""
    session = header.get("session_id") or ""
    spooled_at = float(header.get("spooled_at") or 0)
    sizing_rel = sizing_key[len("sizing:"):] if sizing_key and sizing_key.startswith("sizing:") else None
    by_sizing: Optional[str] = None
    for run_id, sub in _submissions(root, spooled_at - _LOOKBACK_S):
        if run_id in taken:
            continue
        if sub.get("spool_id") == header["spool_id"]:
            return run_id, False
        if tool_use_id and session and sub.get("session_id") == session and sub.get("tool_use_id") == tool_use_id:
            return run_id, False
        if by_sizing is None and sizing_rel and str(sub.get("sizing_rel") or "").replace("\\", "/") == sizing_rel:
            by_sizing = run_id
    if by_sizing:
        return by_sizing, True
    live = _live_token_run(root, sizing_key, taken)
    if live:
        return live, True
    if not sizing_key:
        same = _same_ask_run(root, session, ask, spooled_at, taken)
        if same:
            return same, True
    if session:
        return _lone_token_run(root, session, spooled_at, taken), False
    return None, False


def _existing_run(root: Path, header: dict, tool_use_id: Optional[str], taken: set) -> Optional[str]:
    """The run already minted for this entry, or None. Stamp first, then the original dispatch key."""
    return _find_run(root, header, tool_use_id, taken)[0]


def _stale_reason(root: Path, key: str) -> Optional[str]:
    """Why the entry's declared target moved or landed, else None. Stats and reads only; no git spawn.

    "sizing:" keys: the sizing file is gone, its `plan:` edge names a missing file, or the plan
    spine has rows that are all closed by disposition with nothing rulable and no ledger-halt
    pointer (the all-landed cases `resumable_plan` still resumes are not stale). A sizing with no
    `plan:` edge is fresh. "run:" keys: the run dir is gone. Any read trouble is not stale."""
    try:
        kind, _, target = key.partition(":")
        if kind == "run":
            if not _SAFE_NAME.match(target):
                return None
            from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

            return None if os.path.isdir(root / RUN_DIR_ROOT / target) else f"run {target} is gone"
        if kind != "sizing":
            return None
        rel = PurePosixPath(target)
        if rel.is_absolute() or ".." in rel.parts:
            return None
        sizing_path = root / rel
        if not sizing_path.is_file():
            return f"sizing {target} is gone"
        import yaml

        doc = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
        edge = doc.get("plan") if isinstance(doc, dict) else None
        if not isinstance(edge, str) or not edge.strip():
            return None
        from coordinator_core.ops.dispatch_emit.sizing_fire import single_dated_plan_edge

        plan_rel = edge.strip()
        plan_path = root / plan_rel
        if not plan_path.is_file():
            twin = single_dated_plan_edge(plan_rel)
            plan_path = root / twin
            if not plan_path.is_file():
                return f"plan {plan_rel} is gone"
            plan_rel = twin
        from coordinator_core.ops.dispatch_emit import ask_contract
        from coordinator_core.ops.dispatch_emit.spine_read import (
            NON_DISPATCHABLE_DISPOSITIONS,
            LocateStatus,
            load_rows_memo,
            with_canonical_disposition,
        )

        located = load_rows_memo(plan_path.read_text(encoding="utf-8"))
        if located.status is not LocateStatus.LOCATED:
            return None
        rows = [with_canonical_disposition(r) for r in located.rows]
        if not rows or any(
            not isinstance(r, dict) or r.get("disposition") not in NON_DISPATCHABLE_DISPOSITIONS for r in rows
        ):
            return None
        register = doc.get("requirement_register")
        register_rows = register.get("rows") if isinstance(register, dict) else None
        if isinstance(register_rows, list) and ask_contract.rulable_ids(register_rows):
            return None
        if ask_contract.ledger_halt_path(root, plan_rel).exists():
            return None
        return f"plan {plan_rel} has every row closed"
    except Exception:  # noqa: BLE001 - an unreadable target is the mint's to refuse, never a withdrawal
        return None


def _naming_submitting_repo(payload: dict) -> dict:
    """`payload` with the submitting session's anchor repo as the call's absolute `repo`, when that anchor and the
    launch repo differ and the call names no `repo` of its own; else `payload` unchanged."""
    from coordinator_core.bash_guards._write_bump_applicability import launch_anchor_disagreement

    session = payload.get("session_id")
    tool_input = payload.get("tool_input")
    if not (isinstance(session, str) and session and isinstance(tool_input, dict)):
        return payload
    args = tool_input.get("args")
    args = {"ask": args} if isinstance(args, str) and args.strip() else args
    if not isinstance(args, dict) or args.get("repo"):
        return payload
    if any(isinstance(item, dict) and item.get("repo") for item in (args.get("asks") or ())):
        return payload
    cwd = payload.get("cwd")
    env = payload.get("env") if isinstance(payload.get("env"), dict) else None
    if env and env.get("CLAUDE_PROJECT_DIR"):
        env = {**os.environ, **env}
    disagree = launch_anchor_disagreement(session, cwd if isinstance(cwd, str) and cwd else None, env)
    if not (disagree and Path(disagree[0]).is_absolute()):
        return payload
    return {**payload, "tool_input": {**tool_input, "args": {**args, "repo": str(Path(disagree[0]))}}}


def _mint(root: Path, header: dict, payload: dict, evaluate: Optional[Evaluate], taken: set) -> str:
    """Mint through the submit hook's own emit path, into the repo the ask was submitted from; the run id, or _Refused."""
    if evaluate is None:
        from coordinator_core.hooks.submit_engine_ask import evaluate as evaluate_fn
    else:
        evaluate_fn = evaluate
    envelope = evaluate_fn(_naming_submitting_repo(payload))
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    hso = hso if isinstance(hso, dict) else {}
    reason = str(hso.get("permissionDecisionReason") or "")
    if hso.get("permissionDecision") != "allow" or not hso.get("updatedInput"):
        raise _Refused(reason or "the submit hook answered without a run")
    named = _ASK_RUN.search(reason)
    run_id = (named.group(0) if named else None) or _existing_run(root, header, payload.get("tool_use_id"), taken)
    if not run_id:
        raise _Refused("the submit hook allowed the ask but named no run")
    return run_id


def _rewrite(path: str, header: dict, payload: bytes) -> None:
    """Replace the entry atomically, keeping mode 0600."""
    tmp = f"{path}.{os.getpid()}.tmp"
    body = json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + payload
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, record.FILE_MODE)
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, path)


def _stamp(root: Path, run_id: str, spool_id: str) -> None:
    """Add spool_id to the run's submission record so a re-drain finds it by stamp."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    path = root / RUN_DIR_ROOT / run_id / _SUBMISSION
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("spool_id") != spool_id:
            data["spool_id"] = spool_id
            path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    except (OSError, ValueError):
        pass


def _notify(root: str, header: dict, fields: dict) -> bool:
    """Append one line to notices/<session_id>.jsonl; False when the entry names no usable session."""
    session = header.get("session_id")
    if not isinstance(session, str) or not _SAFE_NAME.match(session):
        return False
    directory = os.path.join(record.spool_dir(root), NOTICES_DIR)
    os.makedirs(directory, mode=record.DIR_MODE, exist_ok=True)
    line = json.dumps({"spool_id": header["spool_id"], "at": time.time(), **fields}, separators=(",", ":")) + "\n"
    fd = os.open(os.path.join(directory, session + ".jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, record.FILE_MODE)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _reclaim_stale(root: str, stale_after_s: float, now: float) -> None:
    """Return claims whose drainer died back to pending."""
    try:
        names = os.listdir(record.spool_dir(root))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(record.CLAIMED_SUFFIX):
            continue
        sid = name[: -len(record.CLAIMED_SUFFIX)]
        try:
            if now - os.stat(record.entry_path(root, sid, record.CLAIMED_SUFFIX)).st_mtime >= stale_after_s:
                record.release(root, sid)
        except OSError:
            continue


def _fail(root: str, sid: str, path: str, header: dict, payload: bytes, why: str, counts: dict, loaded: bool) -> None:
    """`loaded` False (entry unreadable or still being written) keeps the entry's bytes untouched."""
    attempts = int(header.get("attempts") or 0) + 1
    header = {**header, "attempts": attempts, "last_error": why[:500]}
    if loaded:
        try:
            _rewrite(path, header, payload)
        except OSError:
            pass
    try:
        record.release(root, sid)
    except OSError:
        pass  # the claim stays; _reclaim_stale returns it to pending
    counts["failed"] += 1
    if attempts == 1 or attempts % _FAILURE_NOTICE_EVERY == 0:
        if _notify(root, header, {"status": "failed", "attempts": attempts, "error": header["last_error"]}):
            counts["notified"] += 1


def _drain_remote_failover() -> None:
    """Run the queued remote-engine failover retries (bounded batch); never raises, not counted."""
    try:
        from coordinator_core.remote_engine import background

        background.drain_pending()
    except Exception:  # noqa: BLE001 - the ask entries are already settled
        pass


#: Pending listings one drain takes: the first, then a re-list for entries spooled while it ran.
_MAX_PASSES = 3


def _pending_passes(repo_root: str, seen: set) -> Iterator[str]:
    """Pending ids not yet in `seen`, re-listed after each batch is consumed, until a listing finds none
    new or `_MAX_PASSES` listings are taken. An entry that failed and went back to pending is not re-yielded."""
    for _ in range(_MAX_PASSES):
        fresh = [sid for sid in record.list_pending(repo_root) if sid not in seen]
        if not fresh:
            return
        seen.update(fresh)
        yield from fresh


def _joins_a_live_run(root: Path, header: dict, parsed: dict, by_key: dict) -> bool:
    """True when `run` would coalesce the entry into a run that already exists: its key is held by this drain, a live
    token names its sizing or run (any spelling; the run may predate the drain), or a bare ask matches a session run."""
    key = record.coalesce_key(parsed)
    if key:
        return key in by_key or _live_token_run(root, key, set()) is not None
    session = header.get("session_id") or ""
    spooled_at = float(header.get("spooled_at") or 0)
    return _same_ask_run(root, session, _raw_ask_text(parsed), spooled_at, set()) is not None


def _late_duplicates(repo_root: str, seen: set, by_key: dict) -> Iterator[str]:
    """The drain's last act: run the remote failover tail, then list pending ids not in `seen` that join a live run
    (`_joins_a_live_run`), re-listing after each batch (at most `_MAX_PASSES` listings).
    The tail runs first so no work sits between the final listing and the drain's exit.
    Entries are read unclaimed; a concurrent claimer winning the race is handled by `record.claim` returning None in `run`."""
    root = Path(repo_root)
    _drain_remote_failover()
    for _ in range(_MAX_PASSES):
        found = False
        for sid in record.list_pending(repo_root):
            if sid in seen:
                continue
            try:
                header, payload = record.read_entry(record.entry_path(repo_root, sid))
                parsed = json.loads(payload)
                joins = isinstance(parsed, dict) and _joins_a_live_run(root, header, parsed, by_key)
            except (OSError, ValueError, TypeError):
                continue
            if joins:
                seen.add(sid)
                found = True
                yield sid
        if not found:
            return


def _chained_child_active(root: Path, run_id: str, now: float, bound: float) -> bool:
    """True when a live run chained from `run_id` (`run.json` `chain_parent` or `resumed_from`) shows activity within `bound`.

    A launcher parent stays at `minted` by design while its chained child runs, so only a silent chain is stalled.
    One listing of the record root plus reads for dirs touched within `bound`; stat and read calls only."""
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            recent = [e.name for e in it if e.name != run_id and e.is_dir() and now - e.stat().st_mtime <= bound]
    except OSError:
        return False
    for name in recent:
        link = wrd._run_json(root, name)
        if run_id not in (link.get("chain_parent"), link.get("resumed_from")):
            continue
        if wrd._halted(root, name) or wrd._landed(root, name):
            continue
        seen = wrd._own_activity_epoch(root, name)
        if seen is not None and now - seen <= bound:
            return True
    return False


def _fire_cause(exc: BaseException) -> str:
    """Typed cause for a fire that raised before any child ran. Named, never a bare class string."""
    name = type(exc).__name__
    return {
        "ConcurrencyCapExceededError": "fire_cap_exceeded",
        "ChildSpawnFailedError": "fire_spawn_failed",
        "ScriptNotFoundError": "fire_script_absent",
        "FiredDriftError": "fire_script_drifted",
        "PluginDirResolutionError": "fire_plugin_unresolved",
    }.get(name, "fire_preflight_failed") + f": {name}: {str(exc)[:200]}"


def _default_fire(root: Path, run_id: str) -> dict:
    """Fire the minted run's workflow script through the engine's own fire registry; returns the fire record."""
    import hashlib

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
    from coordinator_core.ops.workflow_fire import fire

    script = root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    return fire.fire_workflow(
        str(script), cwd=str(root), run_id=run_id, expected=hashlib.sha256(script.read_bytes()).hexdigest()
    )


def _fire_minted(repo_root: str, fire: Optional[Callable[[Path, str], dict]], now: float) -> None:
    """Fire each run this drain minted that no launcher holds: at most once per run, bounded retries on a typed failure.

    Candidates are `.minted` entries stamped `minted_here` with no `fired` and fewer than `FIRE_ATTEMPT_CAP` attempts, whose
    run is still at stage `minted` and whose script exists. `fired` is the fire id (an `already_fired` handle counts: the
    registry dedupes by digest); a failure stamps `fire_failed` (typed cause) and `fire_attempts`, which the sweep quotes in
    the halt. A fire record that exited with `execution_started` false is a failure, not a fire. Never raises."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    root = Path(repo_root)
    try:
        with os.scandir(record.spool_dir(repo_root)) as it:
            names = [
                e.name for e in it
                if e.name.endswith(record.MINTED_SUFFIX) and now - e.stat().st_mtime <= MINTED_DISPATCH_DEADLINE_SECONDS
            ]
    except OSError:
        return
    do_fire = fire or _default_fire
    for name in names:
        try:
            sid = name[: -len(record.MINTED_SUFFIX)]
            path = record.entry_path(repo_root, sid, record.MINTED_SUFFIX)
            header, payload = record.read_entry(path)
            run_id = header.get("run_id")
            if not (header.get("minted_here") and isinstance(run_id, str) and _SAFE_NAME.match(run_id)):
                continue
            if header.get("fired") or int(header.get("fire_attempts") or 0) >= FIRE_ATTEMPT_CAP:
                continue
            if not (root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs").is_file():
                continue
            if notice.run_disposition(repo_root, run_id, sid) not in (None, "minted"):
                continue
            attempts = int(header.get("fire_attempts") or 0) + 1
            try:
                handle = do_fire(root, run_id)
                if isinstance(handle, dict) and handle.get("state") == "exited" and handle.get("execution_started") is False:
                    raise RuntimeError(f"fire {handle.get('fire_id')} exited before execution: {handle.get('startup_log_tail') or ''}"[:300])
                header = {**header, "fired": (handle or {}).get("fire_id") or True, "fire_attempts": attempts}
                header.pop("fire_failed", None)
            except Exception as exc:  # noqa: BLE001 - typed onto the entry; the next drain retries, the sweep quotes it
                header = {**header, "fire_attempts": attempts, "fire_failed": _fire_cause(exc)}
            _rewrite(path, header, payload)
        except Exception:  # noqa: BLE001 - one unreadable entry never blocks the rest
            continue


def _halt_minted_without_dispatch(
    root: Path, run_id: str, session: object, age: float, *, cause: Optional[str] = None
) -> bool:
    """Halt `run_id` as `minted_without_dispatch` and notice `session`; False when the marker could not be written.

    A non-None `cause` is the whole halt reason; None words it for a spool entry that was never dispatched."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

    if cause is not None:
        reason = cause
    else:
        script = root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
        why = (
            "no launcher started the run's workflow"
            if script.is_file()
            else "its workflow script is absent, so nothing could start it"
        )
        reason = (
            f"never dispatched: minted {int(age)}s ago (bound {int(MINTED_DISPATCH_DEADLINE_SECONDS)}s) and still at stage "
            f"minted with no activity on the run or any chained child; {why}. "
            "Instead: re-fire the ask, or start the run's workflow script."
        )
    try:
        halted_run.write_halt_marker(
            record_dir(root, run_id),
            halted=MINTED_WITHOUT_DISPATCH,
            sizing_rel=halted_run.bound_sizing_rel(root, run_id),
            reason=reason,
        )
    except Exception:  # noqa: BLE001 - a failed write leaves the entry for the next drain
        return False
    if isinstance(session, str) and session:
        notice.append_halt_notice(str(root), session, run_id, MINTED_WITHOUT_DISPATCH, reason)
    return True


def _sweep_minted_without_dispatch(repo_root: str, now: float) -> None:
    """Halt each `.minted` entry's run still at stage `minted` past `MINTED_DISPATCH_DEADLINE_SECONDS` with nothing active.

    Candidates are entries stamped `minted_at` between the deadline and `MINTED_SWEEP_LOOKBACK_SECONDS` ago (a stat each, a read
    only inside that window); a run past `minted`, halted, withdrawn or with a live chained child is untouched, so a second
    drain finds it halted. Never raises: the entries are already settled."""
    from coordinator_core.ops import warp_run_deliverable as wrd

    root = Path(repo_root)
    try:
        with os.scandir(record.spool_dir(repo_root)) as it:
            names = [
                e.name for e in it
                if e.name.endswith(record.MINTED_SUFFIX) and MINTED_DISPATCH_DEADLINE_SECONDS <= now - e.stat().st_mtime
                <= MINTED_SWEEP_LOOKBACK_SECONDS
            ]
    except OSError:
        return
    for name in names:
        try:
            sid = name[: -len(record.MINTED_SUFFIX)]
            header, _ = record.read_entry(record.entry_path(repo_root, sid, record.MINTED_SUFFIX))
            run_id = header.get("run_id")
            age = now - float(header.get("minted_at") or 0)
            if not (isinstance(run_id, str) and _SAFE_NAME.match(run_id)) or age < MINTED_DISPATCH_DEADLINE_SECONDS:
                continue
            if notice.run_disposition(repo_root, run_id, sid) != "minted":
                continue
            if not wrd._stale_at(root, run_id, "minted", MINTED_DISPATCH_DEADLINE_SECONDS, now):
                continue
            if _chained_child_active(root, run_id, now, MINTED_DISPATCH_DEADLINE_SECONDS):
                continue
            failed = header.get("fire_failed")
            cause = (
                f"the engine fired the run {int(header.get('fire_attempts') or 0)} time(s) and it never started: {failed}. "
                "Instead: re-fire the ask, or start the run's workflow script."
            ) if failed else None
            _halt_minted_without_dispatch(root, run_id, header.get("session_id"), age, cause=cause)
        except Exception:  # noqa: BLE001 - one unreadable entry never blocks the rest
            continue


def _has_run_dir(path: Path) -> bool:
    """True when `path` holds anything beyond `submission.json` (drain writes that before any start)."""
    try:
        with os.scandir(path) as it:
            return any(e.name != _SUBMISSION for e in it)
    except OSError:
        return False


def _sweep_stalled_launcher_children(repo_root: str, now: float) -> None:
    """Halt each record dir whose run-stage reads `minted`/`executing`, with no run dir, silent past `MINTED_DISPATCH_DEADLINE_SECONDS`.

    Launcher children have no spool `.minted` entry, so `_sweep_minted_without_dispatch` never sees them. One scandir of
    the record root; a read only for dirs whose mtime lies between the deadline and `MINTED_SWEEP_LOOKBACK_SECONDS` ago.
    Halted, landed and chained-child-active runs are untouched. Never raises."""
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, read_run_stage

    root = Path(repo_root)
    names: list = []
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            for e in it:
                try:
                    if not _SAFE_NAME.match(e.name):
                        continue
                    mtime = e.stat().st_mtime
                    if MINTED_DISPATCH_DEADLINE_SECONDS <= now - mtime <= MINTED_SWEEP_LOOKBACK_SECONDS and e.is_dir():
                        names.append((e.name, mtime))
                except OSError:  # an entry that vanished mid-scan never aborts the rest
                    continue
    except OSError:
        return
    for run_id, mtime in names:
        try:
            stage = read_run_stage(root, run_id)
            if not stage or stage.get("stage") != "minted" or stage.get("phase") != "executing":
                continue
            if _has_run_dir(root / RUN_DIR_ROOT / run_id):
                continue
            if wrd._halted(root, run_id) or wrd._landed(root, run_id):
                continue
            if not wrd._stale_at(root, run_id, "minted", MINTED_DISPATCH_DEADLINE_SECONDS, now):
                continue
            if _chained_child_active(root, run_id, now, MINTED_DISPATCH_DEADLINE_SECONDS):
                continue
            seen = wrd._stamp_epoch(stage.get("last_activity_at"))
            if seen is None:
                seen = wrd._stamp_epoch(stage.get("started_at"))
            age = now - (seen if seen is not None else mtime)
            cause = (
                f"stalled at minted/executing with no run dir: no activity for {int(age)}s "
                f"(bound {int(MINTED_DISPATCH_DEADLINE_SECONDS)}s); the child may never have started or may have stopped. "
                "Instead: re-fire the child's ask."
            )
            _halt_minted_without_dispatch(root, run_id, stage.get("session_id"), age, cause=cause)
        except Exception:  # noqa: BLE001 - one unreadable record never blocks the rest
            continue


def run(
    repo_root: str,
    *,
    evaluate: Optional[Evaluate] = None,
    stale_after_s: float = STALE_CLAIM_S,
    now: Optional[float] = None,
    fire: Optional[Callable[[Path, str], dict]] = None,
) -> dict:
    """Drain every pending entry once, re-listing pending after each batch (at most `_MAX_PASSES` listings) and, after the
    remote failover tail, for a duplicate of a run this drain holds, so a refire spooled mid-drain or during the tail coalesces in that drain; counts `{minted, skipped, failed, notified, coalesced, withdrawn}`.

    `minted` made a new run, `skipped` found its run already minted, `failed` stays pending,
    `coalesced` joined the run another entry of the same sizing or run key holds, `withdrawn`
    was retired as stale (its sizing, plan or run is gone, or the plan's rows all closed) or as
    superseded (a bare ask whose normalized text a live run token already records, any session or age;
    or a sizing/run-keyed entry whose key a live run token holds that this drain did not mint).
    A drainer killed mid-entry leaves a claim that `stale_after_s` later returns to pending."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    repo_root = str(repo_root)
    root = Path(repo_root)
    counts = {"minted": 0, "skipped": 0, "failed": 0, "notified": 0, "coalesced": 0, "withdrawn": 0}
    _reclaim_stale(repo_root, stale_after_s, time.time() if now is None else now)
    taken: set = set()
    by_key: dict = {}
    by_ask: dict = {}  # normalized bare-ask hash -> the run this drain holds, so an identical entry joins it
    seen: set = set()
    for sid in itertools.chain(_pending_passes(repo_root, seen), _late_duplicates(repo_root, seen, by_key)):
        path = record.claim(repo_root, sid)
        if path is None:
            continue
        os.utime(path)
        header: dict = {"spool_id": sid}
        payload = b""
        loaded = False
        why: Optional[str] = None
        superseded: Optional[str] = None
        try:
            header, payload = record.read_entry(path)
            loaded = True
            parsed: Any = json.loads(payload)
            if not isinstance(parsed, dict):
                raise ValueError("the spooled payload is not a JSON object")
            key = record.coalesce_key(parsed)
            why = _stale_reason(root, key) if key else None
            if why:
                if not record.withdraw(repo_root, sid, "stale: " + why, by="drain")["withdrawn"]:
                    raise RuntimeError("the stale entry could not be withdrawn")
            else:
                ask_hash = None
                if not key and _raw_ask_text(parsed):
                    from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash

                    ask_hash = normalized_ask_hash(_raw_ask_text(parsed))
                run_id = by_key.get(key) if key else by_ask.get(ask_hash)
                coalesced = run_id is not None
                if run_id is None:
                    run_id, coalesced = _find_run(
                        root, header, parsed.get("tool_use_id"), taken, key, None if key else _raw_ask_text(parsed)
                    )
                if key and coalesced and key not in by_key:
                    superseded = _live_token_run(root, key, set())
                elif run_id is None and not key and _raw_ask_text(parsed):
                    superseded = _any_ask_run(root, _raw_ask_text(parsed))
                if superseded:
                    if not record.withdraw(
                        repo_root, sid, f"superseded by {superseded}", by="drain", superseded_by=superseded
                    )["withdrawn"]:
                        raise RuntimeError("the superseded entry could not be withdrawn")
                minted_now = run_id is None
                if run_id is None and superseded is None:
                    run_id = _mint(root, header, parsed, evaluate, taken)
                if superseded is None and not os.path.isdir(root / RUN_DIR_ROOT / run_id):
                    raise RuntimeError(f"run {run_id} has no run dir")
        except Exception as exc:  # BaseException (a kill) leaves the claim for the stale reclaim
            _fail(repo_root, sid, path, header, payload, f"{type(exc).__name__}: {exc}", counts, loaded)
            continue
        if why or superseded:
            counts["withdrawn"] += 1
            fields = {"status": "withdrawn", "reason": "stale: " + why} if why else {
                "status": "withdrawn", "reason": "superseded", "run_id": superseded,
            }
            if _notify(repo_root, header, fields):
                counts["notified"] += 1
            continue
        if key and key not in by_key:
            by_key[key] = run_id
        elif ask_hash and ask_hash not in by_ask:
            by_ask[ask_hash] = run_id
        stamped = {**header, "run_id": run_id, "minted_at": time.time()}
        if minted_now and not coalesced:
            stamped["minted_here"] = True  # no session holds a launcher: the engine fires it (`_fire_minted`)
        if coalesced:
            stamped["coalesced_into"] = run_id
        else:
            taken.add(run_id)
            _stamp(root, run_id, sid)
        try:
            _rewrite(path, stamped, payload)
        except OSError:
            pass
        fields = {"status": "coalesced" if coalesced else "minted", "run_id": run_id}
        if minted_now and notice.run_disposition(repo_root, run_id, sid) in (None, "minted"):
            fields["unstarted"] = True  # minted here, so no session holds a launcher for it; a terminal run has nothing to start
        if _notify(repo_root, header, fields):
            counts["notified"] += 1
        record.mark_minted(repo_root, sid)
        counts["coalesced" if coalesced else "minted" if minted_now else "skipped"] += 1
    sweep_now = time.time() if now is None else now
    _fire_minted(repo_root, fire, sweep_now)
    _sweep_minted_without_dispatch(repo_root, sweep_now)
    _sweep_stalled_launcher_children(repo_root, sweep_now)
    return counts

