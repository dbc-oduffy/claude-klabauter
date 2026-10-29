"""Run-scoped commit authorization for a warp `--ask` Workflow's in-run terminal commit.

The emit mints one token per run and returns it in the fire args, never in the script's bytes:
the harness hands it to the run as `args.runToken`, so no file a peer or a run agent can read
carries it. The run's final stage hands it to `dispatch.terminal_commit` through a
`coordinator:git-commit-agent`. `block_subagent_commit` admits that one invocation through
`admits`, a read-only match; the op then `redeem`s the record, an atomic claim exactly one
caller wins (a racing second invocation passes the guard and is refused there). After commit_v2
the op `consume`s the claim when a commit landed, else `release`s it back to live. A token
admits one landed product commit, never two; a halted run's record-only commit redeems and
releases it. A landed grind commit passes `landed` to `consume`, which leaves a spent receipt
(`spent/<run_id>.json`: the record's digest, script and mint time plus the landed sha, rows and
paths); `spent_receipt` answers a repeat call from it and lands nothing. Accepted residual: an op
process killed between `redeem` and `release` strands the claim until its holder is dead (its pid
gone, or in the same process its thread exited); the next `redeem` then adopts it. A live holder
past any age keeps its claim: it may still be committing. Every other subagent commit stays denied.

Invariants: the record lives in the git common dir (never the worktree, so no commit can carry
it); only a sha256 of the token is stored, except a launcher child's escrow
(`escrow/<run_id>.json`), which only the stage-time twin write reads (`escrowed`) and which dies
with the record; the record names the run's script and a token admits nothing without that
script; a record older than `TTL_SECONDS` admits nothing. In-process file I/O only: no spawn.

Removal rule: only a landed commit, an ended or refused run, or a sanctioned peer release
removes a record. `tests/test_run_token_removers_enumerated.py` names every `consume` / `discard`
caller with its condition. This module's own unlinks are the protocol and are outside that walk:
`mint` unlinks a stale claimed record, a stale spent receipt and a stale escrow, `redeem` unlinks
the live record it claims, and `consume` / `discard` unlink their targets and the escrow.
`sweep_expired_escrows` unlinks escrows older than `TTL_SECONDS`. A receipt is never removed
otherwise; it ages out by `TTL_SECONDS` and `live_records` / `held_records` (non-recursive) never list it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

TTL_SECONDS = 24 * 3600
KINDS = ("ask", "grind")
#: A legacy claim (no holder pid recorded) older than this is adopted by the next `redeem`
#: (twice the dispatcher's 30s backstop: the original op's reply is long abandoned).
CLAIM_MAX_AGE_SECONDS = 60.0
_DIR = "coordinator-warp-run-tokens"
#: Where `redeem` creates the claim record; `consume` or `release` ends it.
_CLAIMED = "claimed"
#: Where `consume(landed=...)` leaves the receipt of a landed commit; `mint` removes a stale one.
_SPENT = "spent"
#: Where `note_halt_landing` leaves the receipt of a landed halted-run record commit, which never spends the token.
_HALTED = "halted"
#: Where `escrow` keeps a launcher child's plaintext token for the stage-time twin write.
_ESCROW = "escrow"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _common_dir(repo_root: Path) -> Optional[Path]:
    """`<main worktree>/.git` for a worktree root or a common dir; None when neither holds."""
    root = Path(repo_root)
    if root.name == ".git" and root.is_dir():
        return root
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        # A linked worktree: `gitdir: <common>/worktrees/<name>`.
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (root / gitdir).resolve()
            return gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
    return None


def _record_path(
    repo_root: Path,
    run_id: str,
    *,
    claimed: bool = False,
    spent: bool = False,
    halted: bool = False,
    escrow: bool = False,
) -> Optional[Path]:
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    common = _common_dir(repo_root)
    if common is None:
        return None
    base = common / _DIR
    sub = _SPENT if spent else _HALTED if halted else _ESCROW if escrow else _CLAIMED if claimed else None
    return (base / sub if sub else base) / f"{run_id}.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _read(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _fresh(record: dict) -> bool:
    minted = record.get("minted_at")
    return isinstance(minted, (int, float)) and time.time() - minted <= TTL_SECONDS


def mint(
    repo_root: Path,
    run_id: str,
    script_path: str,
    *,
    session_id: Optional[str] = None,
    sizing_rel: Optional[str] = None,
    kind: str = "ask",
) -> str:
    """Write the run's record and return the token. Raises ValueError when no git dir resolves,
    the run id is malformed, `script_path` is empty or `kind` is not one of `KINDS`: an unbound
    token is never minted.

    `session_id` and `sizing_rel` (None for a raw ask until its gate binds the sizing it minted,
    `bind_sizing`) let `run_owns_sizing` tell an in-flight run's sizing from an abandoned one.
    `kind` ("ask" raw-ask run, "grind" queue-grind run) is recorded as `"kind"`; a record without
    it reads as "ask"."""
    if not isinstance(script_path, str) or not script_path.strip():
        raise ValueError("run token: script_path is required")
    if kind not in KINDS:
        raise ValueError(f"run token: kind must be one of {KINDS}, got {kind!r}")
    path = _record_path(repo_root, run_id)
    if path is None:
        raise ValueError(f"run token: no git common dir under {repo_root} or a malformed run id {run_id!r}")
    token = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "sha256": _digest(token),
        "script_path": script_path,
        "minted_at": time.time(),
        "session_id": session_id,
        "sizing_rel": sizing_rel,
        "kind": kind,
    }
    for stale in (
        _record_path(repo_root, run_id, claimed=True),
        _record_path(repo_root, run_id, spent=True),
        _record_path(repo_root, run_id, halted=True),
        _record_path(repo_root, run_id, escrow=True),
    ):
        if stale is not None:
            stale.unlink(missing_ok=True)
    # A params file left by the prior mint carries its token; a no-prompt-block land_run would
    # read it back and be refused as "spent, expired or not the one minted". Deferred: the
    # channel module imports this one.
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as _chan

    stale_params = _chan.params_path(repo_root, run_id)
    if stale_params is not None:
        stale_params.unlink(missing_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return token


def escrow(repo_root: Path, run_id: str, token: str) -> bool:
    """Keep `token` in plaintext at `escrow/<run_id>.json` so the stage-time twin write can read it
    back without an agent. False when no common dir resolves or the run id is malformed.

    Trap: the plaintext token sits on disk from emit, not from stage. It lives under the git common
    dir (never the worktree, never the script's bytes), so the invariant that no commit or script
    carries the token still holds; do not move it into the script."""
    path = _record_path(repo_root, run_id, escrow=True)
    if path is None or not isinstance(token, str):
        return False
    from coordinator_core.atomic_replace import atomic_write_bytes

    record = {"run_id": run_id, "token": token, "minted_at": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"), preserve_mode=False)
    except OSError:
        return False
    return True


def escrowed(repo_root: Path, run_id: str) -> Optional[str]:
    """Read-only: the escrowed token while it is TOKEN_RE-shaped and the run's live (unclaimed)
    record is fresh with a matching sha256; else None. Never raises. A claimed record never
    admits (`redeem` dropped the plaintext twins; a later write must not re-create one); a
    `release` restores the live record, so the token is readable again."""
    try:
        held = _read(_record_path(repo_root, run_id, escrow=True))
        token = held.get("token") if held else None
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return None
        live = _read(_record_path(repo_root, run_id))
        if live is None or not _fresh(live):
            return None
        if not hmac.compare_digest(str(live.get("sha256") or ""), _digest(token)):
            return None
        return token
    except Exception:  # noqa: BLE001 -- a peek never fails its caller
        return None


def sweep_expired_escrows(repo_root: Path, now: Optional[float] = None) -> int:
    """Unlink escrow files older than `TTL_SECONDS` (a run that died without consume/discard);
    return the count. Age is the escrow's own `minted_at`, else file mtime. Never raises."""
    common = _common_dir(repo_root)
    if common is None:
        return 0
    cutoff = (time.time() if now is None else now) - TTL_SECONDS
    removed = 0
    try:
        paths = list((common / _DIR / _ESCROW).glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            minted = (_read(path) or {}).get("minted_at")
            stamp = minted if isinstance(minted, (int, float)) else path.stat().st_mtime
            if stamp < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _matches(record: Optional[dict], run_id: object, token: object, script_path: object) -> bool:
    if record is None or record.get("run_id") != run_id or not _fresh(record):
        return False
    recorded_script = record.get("script_path")
    if not isinstance(recorded_script, str) or not recorded_script or script_path != recorded_script:
        return False
    return hmac.compare_digest(str(record.get("sha256") or ""), _digest(str(token)))


def _well_formed(run_id: object, token: object, script_path: object) -> bool:
    return (
        isinstance(run_id, str) and isinstance(token, str) and bool(TOKEN_RE.match(token))
        and isinstance(script_path, str) and bool(script_path)
    )


def admits(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Read-only: True while the run's live record is fresh and `token` and `script_path` match
    it. Claims nothing, so a guard that admits a command whose op then fails to parse strands
    no record; two racing invocations can both pass here and `redeem` refuses the loser."""
    if not _well_formed(run_id, token, script_path):
        return False
    return _matches(_read(_record_path(repo_root, run_id)), run_id, token, script_path)


def redeem(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """The op's atomic claim: True for exactly one caller whose `token` and `script_path` match
    the live record. The claim is an exclusive create of the claimed record (a rename of one
    source can succeed for several racing callers on Windows). The claimed record stays until
    `consume` (the commit landed) or `release` (it did not)."""
    if not _well_formed(run_id, token, script_path):
        return False
    live = _record_path(repo_root, run_id)
    claimed = _record_path(repo_root, run_id, claimed=True)
    if live is None or claimed is None:
        return False
    record = _read(live)
    if not _matches(record, run_id, token, script_path):
        held = _read(claimed)
        if held is None:
            return False
        adopted, aside = _adopt_expired_claim(claimed, held, run_id, token, script_path)
        if aside is not None:
            aside.unlink(missing_ok=True)
        return adopted
    try:
        claimed.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    _write_claim(fd, record)
    for _ in range(50):
        try:
            live.unlink(missing_ok=True)
            break
        except PermissionError:  # a racing reader holds the live record open (Windows)
            time.sleep(0.005)
    _drop_staged_twins(repo_root, run_id)
    return True


def _write_claim(fd: int, record: dict) -> None:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {**record, "claimed_at": time.time(), "pid": os.getpid(), "thread": threading.get_ident()},
            fh, sort_keys=True,
        )


def _holder_alive(pid: object) -> bool:
    """False only for a pid that is provably gone; any doubt reads as alive."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        from coordinator_core.session.core import pid_alive

        return bool(pid_alive(pid))
    except Exception:  # noqa: BLE001 -- liveness is advisory; age still expires the claim
        return True


def _claim_expirable(record: dict, path: Path) -> bool:
    """True when the claim's holder is provably dead: its pid gone, or, in this process, its thread
    exited. A warm op past its budget is abandoned, not stopped, so age alone never expires a claim
    whose holder is recorded. A legacy claim without `pid` ages by `claimed_at` or the file mtime."""
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0:
        if pid == os.getpid():
            thread = record.get("thread")
            return isinstance(thread, int) and thread not in {t.ident for t in threading.enumerate()}
        return not _holder_alive(pid)
    stamp = record.get("claimed_at")
    if not isinstance(stamp, (int, float)):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return False
    return time.time() - stamp > CLAIM_MAX_AGE_SECONDS


def _adopt_expired_claim(
    claimed: Path, record: dict, run_id: object, token: object, script_path: object
) -> tuple[bool, Optional[Path]]:
    """Take over a matching, expirable claim: the rename out of `claimed` has exactly one winner,
    who re-creates the claim under its own pid and time. Returns (adopted, the renamed-aside
    file the caller unlinks)."""
    if not _matches(record, run_id, token, script_path) or not _claim_expirable(record, claimed):
        return False, None
    aside = claimed.with_name(f"{claimed.name}.adopt.{os.getpid()}")
    try:
        os.replace(claimed, aside)
    except OSError:
        return False, None
    try:
        fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False, aside
    _write_claim(fd, {k: v for k, v in record.items() if k not in ("claimed_at", "pid", "thread")})
    return True, aside


def _drop_staged_twins(repo_root: Path, run_id: object) -> None:
    """A won claim drops the run's staged twins (they carry the plaintext token): the one seam for every
    redeeming caller. Never fails the redeem; an in-process unlink, no spawn."""
    from coordinator_core.ops.dispatch_emit import launcher_staged_twin

    try:
        launcher_staged_twin.remove_twins(repo_root, run_id)
    except Exception:  # noqa: BLE001 -- cleanup must not fail the claim
        pass


def release(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Return the record this invocation claimed to live after a commit that landed nothing.
    True when restored; False when the claim is gone (`consume`d, re-minted) or a live record
    already exists, so a release never revives a spent token or overwrites a fresh mint."""
    if not _well_formed(run_id, token, script_path):
        return False
    live = _record_path(repo_root, run_id)
    claimed = _record_path(repo_root, run_id, claimed=True)
    if live is None or claimed is None:
        return False
    if not _matches(_read(claimed), run_id, token, script_path) or live.exists():
        return False
    try:
        os.replace(claimed, live)
    except OSError:
        return False
    return True


def is_live(repo_root: Path, run_id: str) -> bool:
    """True while the run's record is unclaimed and inside its TTL."""
    record = _read(_record_path(repo_root, run_id))
    return record is not None and _fresh(record)


def live_records(repo_root: Path) -> Iterator[dict]:
    """Every unclaimed, unexpired record, in path order."""
    common = _common_dir(repo_root)
    if common is None:
        return
    try:
        paths = sorted((common / _DIR).glob("*.json"))
    except OSError:
        return
    for path in paths:
        record = _read(path)
        if record is not None and isinstance(record.get("run_id"), str) and _fresh(record):
            yield record


def held_records(repo_root: Path) -> list[dict]:
    """Every record a run still holds: each fresh unclaimed one (`live_records`) plus every
    claimed one (a run mid terminal-commit). One directory listing each; no spawn. A claimed
    record is not freshness-checked here."""
    records = list(live_records(repo_root))
    common = _common_dir(repo_root)
    if common is not None:
        try:
            claimed = sorted((common / _DIR / _CLAIMED).glob("*.json"))
        except OSError:
            claimed = []
        for path in claimed:
            record = _read(path)
            if record is not None and isinstance(record.get("run_id"), str):
                records.append(record)
    return records


def spent_receipt(repo_root: Path, run_id: object, token: object, script_path: object) -> Optional[dict]:
    """Read-only: the `landed` dict (sha, rows, paths) that `consume(landed=...)` recorded for this
    run, when `token` and `script_path` match the receipt and it is inside its TTL; else None
    (wrong token or script, expired, re-minted since, never landed). Claims nothing, lists nothing."""
    if not _well_formed(run_id, token, script_path):
        return None
    receipt = _read(_record_path(repo_root, run_id, spent=True))
    if not _matches(receipt, run_id, token, script_path):
        return None
    landed = receipt.get("landed")
    return landed if isinstance(landed, dict) else None


def claim_pending(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Read-only: True while a `redeem`ed claim for this `token` and `script_path` is neither
    consumed nor released and its holder is live and in budget, i.e. a terminal commit is running.
    An expirable claim (`_claim_expirable`) is not pending: `redeem` adopts it. Claims nothing."""
    if not _well_formed(run_id, token, script_path):
        return False
    path = _record_path(repo_root, run_id, claimed=True)
    record = _read(path)
    return _matches(record, run_id, token, script_path) and not _claim_expirable(record, path)


def note_halt_landing(repo_root: Path, run_id: object, token: object, script_path: object, landed: dict) -> None:
    """Record the landed halted-run record commit (`landed`: sha, paths, halted kind) beside the
    token without spending it: the live record stays, so the resumed run's product commit can
    still redeem. Writes nothing when the token no longer matches the live record."""
    if not _well_formed(run_id, token, script_path):
        return
    record = _read(_record_path(repo_root, run_id))
    target = _record_path(repo_root, run_id, halted=True)
    if not _matches(record, run_id, token, script_path) or target is None:
        return
    from coordinator_core.atomic_replace import atomic_write_bytes

    receipt = {k: record.get(k) for k in ("run_id", "sha256", "script_path", "minted_at")}
    receipt["landed"] = landed
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, json.dumps(receipt, sort_keys=True).encode("utf-8"), preserve_mode=False)
    except OSError:  # the commit already landed; a missing receipt only costs the repeat-call answer
        pass


def halt_landing(repo_root: Path, run_id: object, token: object, script_path: object) -> Optional[dict]:
    """Read-only: the `landed` dict `note_halt_landing` recorded, under the same match and TTL
    rule as `spent_receipt`; else None."""
    if not _well_formed(run_id, token, script_path):
        return None
    receipt = _read(_record_path(repo_root, run_id, halted=True))
    if not _matches(receipt, run_id, token, script_path):
        return None
    landed = receipt.get("landed")
    return landed if isinstance(landed, dict) else None


def consume(repo_root: Path, run_id: str, *, landed: Optional[dict] = None) -> None:
    """Remove the run's live and claimed records (the commit landed, or the run ended); a
    missing record is already consumed, and nothing is left for `release` to restore.

    `landed` (a dict: sha, rows, paths) first writes the spent receipt `spent_receipt` reads, from
    the claimed record (else the live one). No record to copy, no receipt."""
    if landed is not None:
        record = _read(_record_path(repo_root, run_id, claimed=True)) or _read(_record_path(repo_root, run_id))
        spent = _record_path(repo_root, run_id, spent=True)
        if record is not None and spent is not None:
            from coordinator_core.atomic_replace import atomic_write_bytes

            receipt = {k: record.get(k) for k in ("run_id", "sha256", "script_path", "minted_at")}
            receipt["landed"] = landed
            try:
                spent.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(spent, json.dumps(receipt, sort_keys=True).encode("utf-8"), preserve_mode=False)
            except OSError:  # the commit already landed; a missing receipt only costs the repeat-call answer
                pass
    for claimed in (False, True):
        path = _record_path(repo_root, run_id, claimed=claimed)
        if path is not None:
            path.unlink(missing_ok=True)
    held = _record_path(repo_root, run_id, escrow=True)
    if held is not None:
        held.unlink(missing_ok=True)


def discard(repo_root: Path, run_id: str) -> Optional[dict]:
    """Peer-facing release: remove the run's unclaimed record only and return what it held (None
    when absent or the id is malformed). The claimed path is never touched, so a terminal commit
    in flight keeps its claim. No liveness check: discarding a genuinely running run's record
    leaves its later terminal commit nothing to redeem. Not atomic against a concurrent `redeem`:
    a record claimed between the read and the unlink is still reported."""
    path = _record_path(repo_root, run_id)
    if path is None:
        return None
    record = _read(path)
    path.unlink(missing_ok=True)
    held = _record_path(repo_root, run_id, escrow=True)
    if held is not None:
        held.unlink(missing_ok=True)
    return record


def bind_sizing(repo_root: Path, run_id: str, sizing_rel: str) -> bool:
    """Record the sizing a raw ask's run minted on its live record (the gate calls this once the
    sizing exists). A record already naming a sizing is left as it is. True on a write."""
    path = _record_path(repo_root, run_id)
    record = _read(path)
    if record is None or not _fresh(record) or record.get("sizing_rel"):
        return False
    from coordinator_core.atomic_replace import atomic_write_bytes

    record["sizing_rel"] = sizing_rel
    atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"), preserve_mode=False)
    return True


def normalize_sizing_rel(repo_root: Path, sizing_path: str) -> str:
    """`sizing_path` as a repo-relative forward-slash path: absolute, `./`-prefixed and
    backslash spellings name the same sizing. A path outside the repo is returned in posix form."""
    text = (sizing_path or "").replace("\\", "/")
    if not text:
        return ""
    p = Path(text)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        except (ValueError, OSError):
            return text
    return os.path.normpath(text).replace("\\", "/")


def run_owns_sizing(repo_root: Path, session_id: str, sizing_rel: str) -> bool:
    """True when a live run this session emitted names `sizing_rel`: from its emit (a `--sizing`
    run) or from its gate's bind (a raw ask). A sizing the session merely wrote while a raw ask
    was being sized is not the run's until the gate binds it. Reads only the token dir."""
    if not session_id or not sizing_rel:
        return False
    want = sizing_rel.replace("\\", "/")
    return any(
        record.get("session_id") == session_id
        and str(record.get("sizing_rel") or "").replace("\\", "/") == want
        for record in live_records(repo_root)
    )


def session_holds_run(repo_root: Path, session_id: str) -> bool:
    """True when a run this session minted is held in `repo_root`: a fresh live record or a fresh
    claimed one (a run mid terminal-commit still writes and commits) carries `session_id`.
    Ownership is the engine-minted token record, never a caller value. `held_records` does not
    age claimed records, so they are checked with `_fresh` here: an orphaned claim admits nothing
    past `TTL_SECONDS`. One directory listing each; no spawn; never raises."""
    if not isinstance(session_id, str) or not session_id:
        return False
    try:
        return any(
            record.get("session_id") == session_id and _fresh(record)
            for record in held_records(Path(repo_root))
        )
    except (OSError, ValueError, TypeError):
        return False


def token_params_from_command(raw_cmd: str) -> Optional[dict]:
    """`{run_id, run_token, script_path}` from a `dispatch.land_run` (or legacy
    `dispatch.terminal_commit`) command line's JSON
    params (inline argument or `--params-file -` heredoc body), else None. Text parse only."""
    start = raw_cmd.find("{")
    end = raw_cmd.rfind("}")
    if start < 0 or end <= start:
        return None
    body = raw_cmd[start:end + 1].replace("'\\''", "'")
    try:
        params = json.loads(body)
    except ValueError:
        return None
    if not isinstance(params, dict):
        return None
    return {k: params.get(k) for k in ("run_id", "run_token", "script_path")}
