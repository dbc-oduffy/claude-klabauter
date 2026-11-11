"""Drain the engine-ask submit spool: claim each entry, mint its run once, record a notice.

Contract (docs/reference/ask-spool.md): `run(repo_root) -> {minted, skipped, failed, notified, coalesced, withdrawn}`.
An entry reaches `.minted` only with a run id: found already (`skipped`, no new mint) or minted
through `submit_engine_ask.evaluate` on the stored payload. A failed mint leaves the entry
pending with a bumped `attempts`; an entry is never deleted.

Traps:
- The original emit may have minted before the spool existed (post-delivery spool) or may still
  be finishing after a -32004. Such a run carries no spool_id, so it is matched by the
  submission record's session_id + tool_use_id, else by a lone un-submitted ask token of the
  session near the spool time. -32004 is indeterminate, never not-started.
- After the ask entries, `run` drains the queued remote-engine failover retries
  (`remote_engine.background.drain_pending`, a bounded batch); they are not part of the counts.
- The notice is written before `.minted`: a crash between them re-notices, never loses one.
"""

from __future__ import annotations

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


def _raw_ask_text(payload: dict) -> Optional[str]:
    """The free-text ask a bare entry carries: a string `args`, or a dict `args["ask"]`."""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    args = tool_input.get("args") if isinstance(tool_input, dict) else None
    if isinstance(args, dict):
        args = args.get("ask")
    return args if isinstance(args, str) and args.strip() else None


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


def _mint(root: Path, header: dict, payload: dict, evaluate: Optional[Evaluate], taken: set) -> str:
    """Mint through the submit hook's own emit path; the run id, or _Refused."""
    if evaluate is None:
        from coordinator_core.hooks.submit_engine_ask import evaluate as evaluate_fn
    else:
        evaluate_fn = evaluate
    envelope = evaluate_fn(payload)
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


def _pending_passes(repo_root: str) -> Iterator[str]:
    """Pending ids not yet yielded, re-listed after each batch is consumed, until a listing finds none
    new or `_MAX_PASSES` listings are taken. An entry that failed and went back to pending is not re-yielded."""
    seen: set = set()
    for _ in range(_MAX_PASSES):
        fresh = [sid for sid in record.list_pending(repo_root) if sid not in seen]
        if not fresh:
            return
        seen.update(fresh)
        yield from fresh


def run(
    repo_root: str,
    *,
    evaluate: Optional[Evaluate] = None,
    stale_after_s: float = STALE_CLAIM_S,
    now: Optional[float] = None,
) -> dict:
    """Drain every pending entry once, re-listing pending after each batch (at most `_MAX_PASSES` listings) so a
    duplicate spooled mid-drain coalesces into the run this drain holds; counts `{minted, skipped, failed, notified, coalesced, withdrawn}`.

    `minted` made a new run, `skipped` found its run already minted, `failed` stays pending,
    `coalesced` joined the run another entry of the same sizing or run key holds, `withdrawn`
    was retired as stale (its sizing, plan or run is gone, or the plan's rows all closed).
    A drainer killed mid-entry leaves a claim that `stale_after_s` later returns to pending."""
    repo_root = str(repo_root)
    root = Path(repo_root)
    counts = {"minted": 0, "skipped": 0, "failed": 0, "notified": 0, "coalesced": 0, "withdrawn": 0}
    _reclaim_stale(repo_root, stale_after_s, time.time() if now is None else now)
    taken: set = set()
    by_key: dict = {}
    for sid in _pending_passes(repo_root):
        path = record.claim(repo_root, sid)
        if path is None:
            continue
        os.utime(path)
        header: dict = {"spool_id": sid}
        payload = b""
        loaded = False
        why: Optional[str] = None
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
                run_id = by_key.get(key) if key else None
                coalesced = run_id is not None
                if run_id is None:
                    run_id, coalesced = _find_run(
                        root, header, parsed.get("tool_use_id"), taken, key, None if key else _raw_ask_text(parsed)
                    )
                minted_now = run_id is None
                if run_id is None:
                    run_id = _mint(root, header, parsed, evaluate, taken)
        except Exception as exc:  # BaseException (a kill) leaves the claim for the stale reclaim
            _fail(repo_root, sid, path, header, payload, f"{type(exc).__name__}: {exc}", counts, loaded)
            continue
        if why:
            counts["withdrawn"] += 1
            if _notify(repo_root, header, {"status": "withdrawn", "reason": "stale: " + why}):
                counts["notified"] += 1
            continue
        if key and key not in by_key:
            by_key[key] = run_id
        stamped = {**header, "run_id": run_id, "minted_at": time.time()}
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
        if minted_now:
            fields["unstarted"] = True  # minted here, so no session holds a launcher for it
        if _notify(repo_root, header, fields):
            counts["notified"] += 1
        record.mark_minted(repo_root, sid)
        counts["coalesced" if coalesced else "minted" if minted_now else "skipped"] += 1
    _drain_remote_failover()
    return counts

