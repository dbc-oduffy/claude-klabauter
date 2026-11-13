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

Invariants: the record lives under `engine_record_dir` in the settings home (never the worktree,
so no commit can carry it; off the repo's drive, so a stalled drive cannot hold a write); only a
sha256 of the token is stored, except a launcher child's escrow
(`escrow/<run_id>.json`), which only the stage-time twin write reads (`escrowed`) and which dies
with the record; the record names the run's script and a token admits nothing without that
script; a record older than `TTL_SECONDS` admits nothing. In-process file I/O only: no spawn.

Removal rule: only a landed commit, an ended or refused run, or a sanctioned peer release
removes a record; a removal without a landed commit (`consume`, `discard`, a re-`mint`) first
leaves `retired/<run_id>.json`, which `halt_admits` reads so a halted run's record-only commit
survives it; `readmit_reviewed` is the only retired->live path, and only while no live, claimed or spent record exists.
`consume` and `discard` then wake the stages parked behind the ended run (`_wake_stage_waiters`);
`release` restores a running run and wakes nothing.
`tests/test_run_token_removers_enumerated.py` names every `consume` / `discard`
caller with its condition. This module's own unlinks are the protocol and are outside that walk:
`mint` unlinks a stale claimed record, a stale halted receipt, a stale escrow
and (when no live record is replaced) a stale retired receipt, `redeem` unlinks
the live record it claims, and `consume` / `discard` unlink their targets and the escrow.
`sweep_expired_escrows` unlinks escrows older than `TTL_SECONDS`. A receipt is never removed
otherwise; it ages out by `TTL_SECONDS` and `live_records` / `held_records` (non-recursive) never list it.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core.ops.dispatch_emit.engine_record_home import engine_record_dir, legacy_record_dir
from coordinator_core.ops.dispatch_emit.halt_resume_record import bounded_read

TTL_SECONDS = 24 * 3600
#: Deadline for a read of the legacy in-`.git` home (the stalling drive); a stall reads as absent.
LEGACY_READ_DEADLINE_MS = 150.0
KINDS = ("ask", "grind")
#: A legacy claim (no holder pid recorded) older than this is adopted by the next `redeem`
#: (twice the dispatcher's 30s backstop: the original op's reply is long abandoned).
CLAIM_MAX_AGE_SECONDS = 60.0
#: A same-process claim whose holder thread exited is adoptable only after this age: racing
#: redeemers that read the claim just after a fast winner exits must lose, not adopt it.
THREAD_EXIT_GRACE_SECONDS = 2.0
_DIR = "coordinator-warp-run-tokens"
#: Where `redeem` creates the claim record; `consume` or `release` ends it.
_CLAIMED = "claimed"
#: Where `consume(landed=...)` leaves the receipt of a landed commit; `mint` removes a stale one.
_SPENT = "spent"
#: Where `note_halt_landing` leaves the receipt of a landed halted-run record commit, which never spends the token.
_HALTED = "halted"
#: Where `escrow` keeps a launcher child's plaintext token for the stage-time twin write.
_ESCROW = "escrow"
#: Where a record removed without a landed commit (`consume`, `discard`, a re-`mint`) leaves its
#: receipt, which `halt_admits` reads; `mint` removes a stale one.
_RETIRED = "retired"
#: Digests of earlier tokens a `carry_prior` re-mint keeps admitting, newest first, and how many.
_PRIOR = "prior"
MAX_PRIOR = 7
#: The record fields every receipt (retired, spent, halted) copies from the record it replaces.
_RECEIPT_KEYS = ("run_id", "sha256", "script_path", "minted_at", _PRIOR)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


class RecordWriteCancelled(Exception):
    """`mint` was cancelled by its `WriteGate` before the record was published; nothing was minted."""


class WriteGate:
    """Lets a caller that abandons a `mint` thread keep it from publishing its record later.

    `mint` calls `begin` before the record write and `commit` after it, just ahead of the publishing
    replace; `cancel` (the caller, on its own thread) wins unless `commit` already ran. Every
    transition is a flag flip under one lock with no I/O inside it, so `cancel` never waits on a
    stalled disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"

    def begin(self) -> bool:
        with self._lock:
            if self._state == "cancelled":
                return False
            self._state = "writing"
            return True

    def commit(self) -> bool:
        with self._lock:
            if self._state == "cancelled":
                return False
            self._state = "committed"
            return True

    def cancel(self) -> str:
        """The state the mint was in: "writing" (record write in flight), "idle" (not reached), or
        "committed" (record published; the cancel did not take)."""
        with self._lock:
            state = self._state
            if state != "committed":
                self._state = "cancelled"
            return state


#: The gate of the emit running in this context; `mint` honours it. Unset outside the submit hook.
WRITE_GATE: "contextvars.ContextVar[Optional[WriteGate]]" = contextvars.ContextVar("run_token_write_gate", default=None)


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
    retired: bool = False,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """The record path for `run_id`; a caller looping over many ids passes `home` (from `_record_home`) to resolve it once."""
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    home = home if home is not None else _record_home(repo_root)
    if home is None:
        return None
    return _under(home / _DIR, run_id, claimed, spent, halted, escrow, retired)


_HOME_MEMO: dict = {}


def _record_home(repo_root: Path) -> Optional[Path]:
    """`engine_record_dir` memoized per (root, settings home, resolver): it resolves the git common dir
    (a realpath syscall) and a sibling scan asks for it once per run. Only a found home is kept."""
    from coordinator_core import _settings_home

    key = (str(repo_root), str(_settings_home.settings_home()), engine_record_dir)
    home = _HOME_MEMO.get(key)
    if home is None:
        home = engine_record_dir(repo_root)
        if home is not None:
            if len(_HOME_MEMO) > 256:
                _HOME_MEMO.clear()
            _HOME_MEMO[key] = home
    return home


def _under(base: Path, run_id: str, claimed: bool, spent: bool, halted: bool, escrow: bool, retired: bool) -> Path:
    sub = _SPENT if spent else _HALTED if halted else _ESCROW if escrow else _RETIRED if retired else _CLAIMED if claimed else None
    return (base / sub if sub else base) / f"{run_id}.json"


def _legacy_path(repo_root: Path, run_id: str, **kind: bool) -> Optional[Path]:
    """The record's pre-relocation path under the git common dir; None for a malformed id or no common dir."""
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    common = legacy_record_dir(repo_root)
    if common is None:
        return None
    flags = {k: bool(kind.get(k)) for k in ("claimed", "spent", "halted", "escrow", "retired")}
    return _under(common / _DIR, run_id, **flags)


def _read_legacy(repo_root: Path, run_id: str, **kind: bool) -> Optional[dict]:
    """Legacy-home read through `bounded_read`: the old home is on the drive that stalls.

    Sunset: records are written only under `engine_record_dir`, and a record dies after
    `TTL_SECONDS`, so once one TTL has passed since this lands no live token is legacy-only;
    delete `_read_legacy`, `_read_rec`, `_drop_legacy`, `_present` and the legacy listing then."""
    path = _legacy_path(repo_root, run_id, **kind)
    if path is None:
        return None
    raw = bounded_read(path, LEGACY_READ_DEADLINE_MS)
    if not isinstance(raw, bytes):
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _read_rec(repo_root: Path, run_id: str, **kind: bool) -> Optional[dict]:
    """The record under the settings home; only when that is missing, the legacy copy (new root wins)."""
    record = _read(_record_path(repo_root, run_id, **kind))
    return record if record is not None else _read_legacy(repo_root, run_id, **kind)


def minted_at(repo_root: Path, run_id: str) -> Optional[float]:
    """Epoch seconds the run's live token record was minted, or None (no live record). Never raises."""
    try:
        value = (_read_rec(repo_root, run_id) or {}).get("minted_at")
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _present(repo_root: Path, run_id: str, **kind: bool) -> bool:
    path = _record_path(repo_root, run_id, **kind)
    return (path is not None and path.exists()) or _read_legacy(repo_root, run_id, **kind) is not None


def _drop_legacy(repo_root: Path, run_id: str, **kind: bool) -> None:
    """Unlink the legacy copy of a record, only when a bounded read finds one: a removed record
    must not resurface through the read fallback."""
    if _read_legacy(repo_root, run_id, **kind) is None:
        return
    path = _legacy_path(repo_root, run_id, **kind)
    try:
        if path is not None:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _unlink_both(repo_root: Path, run_id: str, **kind: bool) -> None:
    path = _record_path(repo_root, run_id, **kind)
    if path is not None:
        path.unlink(missing_ok=True)
    _drop_legacy(repo_root, run_id, **kind)


def _bounded_glob(directory: Path, deadline_ms: float, *, reap: bool = False) -> list[Path]:
    """`directory`'s `*.json` modified inside one `TTL_SECONDS`, [] when the listing outlives
    `deadline_ms` or fails. An older legacy file holds an expired token: reading it (one bounded
    thread per file) cost every landing and hook ~2s CPU over ~300 dead records. `reap` deletes
    those expired files in the same pass, so the legacy dir drains instead of being statted forever;
    only the unclaimed dir reaps (a claimed record's expiry is `_claim_expirable`'s call)."""
    box: list[list[Path]] = []
    done = threading.Event()

    def work() -> None:
        try:
            cutoff = time.time() - TTL_SECONDS
            kept: list[Path] = []
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.name.endswith(".json"):
                        continue
                    if entry.stat().st_mtime >= cutoff:
                        kept.append(Path(entry.path))
                    elif reap:
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
            box.append(sorted(kept))
        except OSError:
            box.append([])
        finally:
            done.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except RuntimeError:
        return []
    return box[0] if done.wait(deadline_ms / 1000.0) and box else []


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _read(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    from coordinator_core.ops.dispatch_emit.halt_resume_record import bounded_json_object

    return bounded_json_object(path)


_RECORD_CACHE: dict = {}
_RECORD_CACHE_MAX = 4096


def _read_signed(key: str, sig: tuple, fetched: Optional[dict] = None) -> Optional[dict]:
    """The record at `key` whose stat signature is `sig`: the cached read while `sig` is unchanged, else
    `fetched[key]` (a `_read_batch` result) or one bounded read, cached. None when the file is unreadable.
    The returned dict is shared; copy before mutating."""
    hit = _RECORD_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    record = fetched.get(key) if fetched is not None else _read(Path(key))
    if record is None:
        _RECORD_CACHE.pop(key, None)
        return None
    if len(_RECORD_CACHE) >= _RECORD_CACHE_MAX:
        _RECORD_CACHE.clear()
    _RECORD_CACHE[key] = (sig, record)
    return record


def read_cached(path: Optional[Path]) -> Optional[dict]:
    """`_read(path)` for the question "is this one run's token readable, and what does it say?": one stat,
    then the record cache `_cached_records` shares. An absent file costs the stat alone (no worker thread),
    an unchanged one nothing more, so a scan over many runs reads each token file once. The dict is a copy."""
    if path is None:
        return None
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        _RECORD_CACHE.pop(key, None)
        return None
    record = _read_signed(key, (st.st_mtime_ns, st.st_size))
    return None if record is None else dict(record)


def _cached_records(directory: Path) -> list[tuple[str, dict]]:
    """`directory`'s `*.json` records, sorted by name. One listing; a file is opened only when its
    (mtime, size) from the listing differs from the cached read, so an unchanged token dir opens nothing.
    A write replaces the file (new mtime/size) and so invalidates its own entry."""
    records: list[tuple[str, dict]] = []
    try:
        with os.scandir(directory) as it:
            entries = sorted((e for e in it if e.name.endswith(".json")), key=lambda e: e.name)
            signed = []
            for entry in entries:
                try:
                    st = entry.stat()
                except OSError:
                    continue
                signed.append((entry, (st.st_mtime_ns, st.st_size)))
            misses = [e.path for e, sig in signed if (_RECORD_CACHE.get(e.path) or (None,))[0] != sig]
            fetched = _read_batch(misses) if misses else {}
            for entry, sig in signed:
                record = _read_signed(entry.path, sig, fetched)
                if record is not None:
                    records.append((entry.name[:-5], dict(record)))
    except OSError:
        return []
    return records


def _read_batch(keys: list) -> dict:
    """`{path: record}` for the token files at `keys`, all read on ONE bounded worker thread (a thread per
    small file is the cost of a cold token dir). The wait is `JSON_READ_DEADLINE_MS` plus 1ms per file; a
    file the worker has not reached by then, or that is absent, oversize or not a JSON object, is absent
    from the result. The worker is left to finish after a stall."""
    from coordinator_core.ops.dispatch_emit.halt_resume_record import JSON_READ_DEADLINE_MS, JSON_READ_MAX_BYTES

    got: list = []
    done = threading.Event()

    def work() -> None:
        try:
            for key in keys:
                try:
                    with open(key, "rb") as fh:
                        # Sized from fstat: `read(JSON_READ_MAX_BYTES + 1)` allocates the whole cap per file.
                        size = os.fstat(fh.fileno()).st_size
                        got.append((key, fh.read(min(size, JSON_READ_MAX_BYTES) + 1)))
                except OSError:
                    continue
        finally:
            done.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except RuntimeError:
        return {}
    done.wait((JSON_READ_DEADLINE_MS + len(keys)) / 1000.0)
    out: dict = {}
    for key, raw in list(got):
        if len(raw) > JSON_READ_MAX_BYTES:
            continue
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out[key] = parsed
    return out


def _fresh(record: dict) -> bool:
    minted = record.get("minted_at")
    return isinstance(minted, (int, float)) and time.time() - minted <= TTL_SECONDS


_ASK_RUN_ID_RE = re.compile(r"^ask-\d{8}T\d{6}-[0-9a-f]{6}$")


def new_ask_run_id() -> str:
    """A fresh raw-ask run id, `ask-<YYYYmmddTHHMMSS>-<hex6>`."""
    return f"ask-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def is_ask_run_id(value: object) -> bool:
    return isinstance(value, str) and _ASK_RUN_ID_RE.match(value) is not None


def mint(
    repo_root: Path,
    run_id: str,
    script_path: str,
    *,
    session_id: Optional[str] = None,
    sizing_rel: Optional[str] = None,
    kind: str = "ask",
    marker: bool = False,
    carry_prior: bool = False,
    ask_sha256: Optional[str] = None,
) -> str:
    """Write the run's record and return the token. Raises ValueError when no git dir resolves,
    the run id is malformed, `script_path` is empty or `kind` is not one of `KINDS`: an unbound
    token is never minted.

    `session_id` and `sizing_rel` (None for a raw ask until its gate binds the sizing it minted,
    `bind_sizing`) let `run_owns_sizing` tell an in-flight run's sizing from an abandoned one.
    `kind` ("ask" raw-ask run, "grind" queue-grind run) is recorded as `"kind"`; a record without
    it reads as "ask". Raises `RecordWriteCancelled` when the context's `WRITE_GATE` was cancelled
    before the record published.

    `carry_prior=True` is a re-emit of a run id that may already be running (a resume or refire
    onto the same id): when the live record it replaces is fresh and names the same script, the
    replaced digests stay admitted on the new record, so the token a running script already holds
    does not die with the re-mint. Still one record, so still one landed commit. Default False: a
    re-mint retires every earlier token.

    `marker=True` is the submit hook's pre-answer mint of a brand-new run id: one record write through
    the `WRITE_GATE`, no prior-record read, no stale-twin sweep, no live-index update. The run's own
    emit re-mints the id in full.

    `ask_sha256` is the normalized hash of a raw ask's text (`extant_work.normalized_ask_hash`),
    recorded so a refire of the same ask finds this run. A full mint passing None keeps the key of
    the record it replaces."""
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
    if ask_sha256:
        record["ask_sha256"] = ask_sha256
    if marker:
        _publish_record(path, json.dumps(record, sort_keys=True))
        return token
    # Receipt first: unlinking first would erase the remint case's receipt. The spent receipt stays:
    # it matches only the landed token's digest, so a re-mint still answers that token already_landed.
    replaced = _read_rec(repo_root, run_id)
    if not ask_sha256 and replaced is not None and isinstance(replaced.get("ask_sha256"), str):
        record["ask_sha256"] = replaced["ask_sha256"]
    if (
        carry_prior and replaced is not None and _fresh(replaced)
        and replaced.get("script_path") == script_path and isinstance(replaced.get("sha256"), str)
    ):
        earlier = [d for d in (replaced.get(_PRIOR) or ()) if isinstance(d, str)]
        record[_PRIOR] = [replaced["sha256"], *earlier][:MAX_PRIOR]
    if replaced is not None:
        _write_retired(repo_root, run_id, replaced)
    else:
        _unlink_both(repo_root, run_id, retired=True)
    for stale in ({"claimed": True}, {"halted": True}, {"escrow": True}):
        _unlink_both(repo_root, run_id, **stale)
    # A params file left by the prior mint carries its token; a no-prompt-block land_run would
    # read it back and be refused as "spent, expired or not the one minted". A carry_prior record
    # still admits that token, so its params file stays. Deferred: the channel module imports this one.
    if not record.get(_PRIOR):
        from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as _chan

        for stale_params in (_chan.params_path(repo_root, run_id), _chan.land_params_path(repo_root, run_id)):
            if stale_params is not None:
                stale_params.unlink(missing_ok=True)
    _publish_record(path, json.dumps(record, sort_keys=True))
    _drop_legacy(repo_root, run_id)
    _index("index_mint", repo_root, run_id, record["minted_at"])
    return token


def _publish_record(path: Path, text: str) -> None:
    """Write `text` to a sibling temp file and replace `path` with it, unless the context's `WriteGate`
    was cancelled meanwhile: a write that outlives its abandoned caller leaves no record, only a
    removed temp file. Raises `RecordWriteCancelled` then."""
    from coordinator_core.atomic_replace import _replace_with_retry

    gate = WRITE_GATE.get()
    if gate is not None and not gate.begin():
        raise RecordWriteCancelled(path.stem)
    fd, tmp = tempfile.mkstemp(prefix=".mint.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if gate is not None and not gate.commit():
            raise RecordWriteCancelled(path.stem)
        _replace_with_retry(tmp, str(path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_retired(repo_root: Path, run_id: str, record: Optional[dict]) -> None:
    """Leave `retired/<run_id>.json` ({run_id, sha256, script_path, minted_at}) for a record about
    to be removed without a landed commit. Never raises: a missing receipt only costs the halt admit."""
    target = _record_path(repo_root, run_id, retired=True)
    if record is None or target is None:
        return
    from coordinator_core.atomic_replace import atomic_write_bytes

    receipt = {k: record.get(k) for k in _RECEIPT_KEYS if record.get(k) is not None}
    chain = _retired_chain(record, _read_rec(repo_root, run_id, retired=True))
    if chain:
        receipt[_PRIOR] = chain
    else:
        receipt.pop(_PRIOR, None)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, json.dumps(receipt, sort_keys=True).encode("utf-8"), preserve_mode=False)
    except OSError:
        pass


def _retired_chain(record: dict, earlier: Optional[dict]) -> list[str]:
    """The digests a retired receipt keeps admitting besides its own `sha256`, newest first and at
    most `MAX_PRIOR`: the record's `_PRIOR`, then the earlier receipt's own digest and `_PRIOR`.
    An earlier receipt is folded in only while fresh and naming the same script, so one overwrite
    never drops a token the run still holds."""
    own = record.get("sha256")
    chain = [d for d in (record.get(_PRIOR) or ()) if isinstance(d, str)]
    if earlier is not None and _fresh(earlier) and earlier.get("script_path") == record.get("script_path"):
        chain.append(earlier.get("sha256"))
        chain.extend(earlier.get(_PRIOR) or ())
    seen: set[str] = set()
    kept: list[str] = []
    for digest in chain:
        if isinstance(digest, str) and digest != own and digest not in seen:
            seen.add(digest)
            kept.append(digest)
    return kept[:MAX_PRIOR]


def _index(fn: str, repo_root: Path, *args) -> None:
    """Keep the live-run index in step with the records. Deferred import: the index module
    imports this one."""
    from coordinator_core.ops.dispatch_emit import live_run_writes

    getattr(live_run_writes, fn)(repo_root, *args)


def escrow(repo_root: Path, run_id: str, token: str) -> bool:
    """Keep `token` in plaintext at `escrow/<run_id>.json` so the stage-time twin write can read it
    back without an agent. False when no record dir resolves or the run id is malformed.

    Trap: the plaintext token sits on disk from emit, not from stage. It lives under the settings
    home (never the worktree, never the script's bytes), so the invariant that no commit or script
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
        held = _read_rec(repo_root, run_id, escrow=True)
        token = held.get("token") if held else None
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return None
        live = _read_rec(repo_root, run_id)
        if live is None or not _fresh(live):
            return None
        if not hmac.compare_digest(str(live.get("sha256") or ""), _digest(token)):
            return None
        return token
    except Exception:  # noqa: BLE001 -- a peek never fails its caller
        return None


def sweep_expired_escrows(repo_root: Path, now: Optional[float] = None) -> int:
    """Unlink escrow files older than `TTL_SECONDS` (a run that died without consume/discard);
    return the count. Age is the escrow's own `minted_at`, else file mtime. Never raises. Sweeps the
    settings-home dir only: a legacy escrow ages out with its record and is never listed."""
    home = engine_record_dir(repo_root)
    if home is None:
        return 0
    cutoff = (time.time() if now is None else now) - TTL_SECONDS
    removed = 0
    try:
        paths = list((home / _DIR / _ESCROW).glob("*.json"))
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


def _canonical_script(repo_root: Optional[Path], script: str) -> str:
    """`script` as one real absolute path: a relative one is read from the checkout `repo_root` (a
    worktree root or its `.git`) and symlinks and case are resolved, so two spellings of one file agree."""
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    if repo_root is not None and base.name == ".git":
        base = base.parent
    return os.path.normcase(os.path.realpath(os.path.join(str(base), script)))


def _same_script(repo_root: Optional[Path], recorded: str, script_path: object) -> bool:
    """True when `script_path` names the script the record was minted for: the identical string, or a
    spelling that resolves to the same file. A different file never matches."""
    if not isinstance(script_path, str):
        return False
    if script_path == recorded:
        return True
    return _canonical_script(repo_root, script_path) == _canonical_script(repo_root, recorded)


def _matches(
    record: Optional[dict], run_id: object, token: object, script_path: object, repo_root: Optional[Path] = None
) -> bool:
    if record is None or record.get("run_id") != run_id or not _fresh(record):
        return False
    recorded_script = record.get("script_path")
    if not isinstance(recorded_script, str) or not recorded_script or not _same_script(repo_root, recorded_script, script_path):
        return False
    want = _digest(str(token))
    admitted = [record.get("sha256"), *(record.get(_PRIOR) or ())]
    return any(isinstance(d, str) and hmac.compare_digest(d, want) for d in admitted)


def _well_formed(run_id: object, token: object, script_path: object) -> bool:
    return (
        isinstance(run_id, str) and isinstance(token, str) and bool(TOKEN_RE.match(token))
        and isinstance(script_path, str) and bool(script_path)
    )


def admits(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Read-only: True while the run's live record is fresh and `token` and `script_path` match
    it. Claims nothing, so a guard that admits a command whose op then fails to parse strands
    no record; two racing invocations can both pass here and `redeem` refuses the loser.
    An admit is real run activity: it also throttle-stamps the run's `last_activity_at`
    (`ask_contract.touch_run_activity`); that heartbeat never fails the admit."""
    if not _well_formed(run_id, token, script_path):
        return False
    admitted = _matches(_read_rec(repo_root, run_id), run_id, token, script_path, repo_root)
    if admitted:
        _heartbeat(repo_root, run_id)
    return admitted


def _heartbeat(repo_root: Path, run_id: str) -> None:
    try:
        from coordinator_core.ops.dispatch_emit.ask_contract import touch_run_activity
        from coordinator_core.session import record_homes

        touch_run_activity(Path(record_homes.home_dir(str(repo_root), "warp")) / run_id)
    except Exception:  # noqa: BLE001 -- a heartbeat must never fail an admit
        pass


def halt_admits(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Read-only: True when `token` and `script_path` match the run's live, claimed or retired
    record and it is inside `TTL_SECONDS`. For a halted run's record-only commit, which spends
    nothing; a product commit still needs `admits`. Claims nothing."""
    if not _well_formed(run_id, token, script_path):
        return False
    return any(
        _matches(_read_rec(repo_root, run_id, **kind), run_id, token, script_path, repo_root)
        for kind in ({}, {"claimed": True}, {"retired": True})
    )


def readmit_reviewed(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Restore the run's retired receipt to a live record so `redeem` can claim it. True only for
    the caller that created the live record.

    Requires no live, claimed or spent record for the run (any token) and a retired receipt that
    `_matches`, so at most one commit lands per token. The caller decides the review is recorded.
    Trap: the receipt keeps only `_RECEIPT_KEYS`, so the record has no session_id, sizing_rel or
    kind and `session_holds_run` never matches it; the redeem that follows claims it at once.
    Removes nothing."""
    if not _well_formed(run_id, token, script_path):
        return False
    live = _record_path(repo_root, run_id)
    if live is None or any(_present(repo_root, run_id, **k) for k in ({}, {"claimed": True}, {"spent": True})):
        return False
    receipt = _read_rec(repo_root, run_id, retired=True)
    if not _matches(receipt, run_id, token, script_path, repo_root):
        return False
    record = {k: receipt.get(k) for k in _RECEIPT_KEYS if receipt.get(k) is not None}
    record["readmitted_at"] = time.time()
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(live, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, sort_keys=True)
    _index("index_mint", repo_root, run_id, record["minted_at"])
    return True


def redeem(
    repo_root: Path, run_id: object, token: object, script_path: object, *, budget_secs: Optional[float] = None
) -> bool:
    """The op's atomic claim: True for exactly one caller whose `token` and `script_path` match
    the live record. The claim is an exclusive create of the claimed record (a rename of one
    source can succeed for several racing callers on Windows). The claimed record stays until
    `consume` (the commit landed) or `release` (it did not). `budget_secs` stamps the claim's
    `deadline`; see `redeem_claim`."""
    return redeem_claim(repo_root, run_id, token, script_path, budget_secs=budget_secs) is not None


def redeem_claim(
    repo_root: Path, run_id: object, token: object, script_path: object, *, budget_secs: Optional[float] = None
) -> Optional[dict]:
    """`redeem`, returning the winning claim's identity `{claimed_at, pid, thread}` plus its `deadline`
    when stamped (None when this call did not win); pass it to `holds_claim` to learn whether a later
    caller adopted the claim. A positive `budget_secs` records `deadline` = claimed_at + budget_secs:
    past it the claim is adoptable by a later call even while its holder lives."""
    if not _well_formed(run_id, token, script_path):
        return None
    live = _record_path(repo_root, run_id)
    claimed = _record_path(repo_root, run_id, claimed=True)
    if live is None or claimed is None:
        return None
    record = _read_rec(repo_root, run_id)
    if not _matches(record, run_id, token, script_path, repo_root):
        held = _read_rec(repo_root, run_id, claimed=True)
        if held is None:
            return None
        identity, aside = _adopt_expired_claim(claimed, held, run_id, token, script_path, budget_secs, repo_root)
        if aside is not None:
            aside.unlink(missing_ok=True)
        return identity
    try:
        claimed.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return None
    identity = _write_claim(fd, record, budget_secs)
    for _ in range(50):
        try:
            live.unlink(missing_ok=True)
            break
        except PermissionError:  # a racing reader holds the live record open (Windows)
            time.sleep(0.005)
    _drop_legacy(repo_root, run_id)
    _drop_staged_twins(repo_root, run_id)
    return identity


def holds_claim(repo_root: Path, run_id: object, token: object, script_path: object, identity: object) -> bool:
    """Read-only: True while the claimed record matches `token` and `script_path` and still carries
    `identity` (from `redeem_claim`); an adopter's re-created claim carries its own, and `consume` or
    `release` removes it. Never raises."""
    if not isinstance(identity, dict) or not _well_formed(run_id, token, script_path):
        return False
    record = _read_rec(repo_root, run_id, claimed=True)
    return _matches(record, run_id, token, script_path, repo_root) and all(
        record.get(k) == identity.get(k) for k in _IDENTITY_KEYS
    )


_IDENTITY_KEYS = ("claimed_at", "pid", "thread")


def _write_claim(fd: int, record: dict, budget_secs: Optional[float] = None) -> dict:
    identity: dict = {"claimed_at": time.time(), "pid": os.getpid(), "thread": threading.get_ident()}
    if isinstance(budget_secs, (int, float)) and budget_secs > 0:
        identity["deadline"] = identity["claimed_at"] + budget_secs
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({**record, **identity}, fh, sort_keys=True)
    return identity


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
    exited; or when the claim is past its recorded `deadline` (the op budget), since a warm op past
    its budget is abandoned, not stopped, and its late settle is a no-op (`holds_claim`). Age alone
    never expires a claim whose holder is recorded without a `deadline`. A legacy claim without `pid`
    ages by `claimed_at` or the file mtime."""
    deadline = record.get("deadline")
    if isinstance(deadline, (int, float)) and time.time() > deadline:
        return True
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0:
        if pid == os.getpid():
            thread = record.get("thread")
            if not isinstance(thread, int) or thread in {t.ident for t in threading.enumerate()}:
                return False
            made = record.get("claimed_at")
            return not isinstance(made, (int, float)) or time.time() - made > THREAD_EXIT_GRACE_SECONDS
        return not _holder_alive(pid)
    stamp = record.get("claimed_at")
    if not isinstance(stamp, (int, float)):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return False
    return time.time() - stamp > CLAIM_MAX_AGE_SECONDS


def _adopt_expired_claim(
    claimed: Path,
    record: dict,
    run_id: object,
    token: object,
    script_path: object,
    budget_secs: Optional[float] = None,
    repo_root: Optional[Path] = None,
) -> tuple[Optional[dict], Optional[Path]]:
    """Take over a matching, expirable claim: the rename out of `claimed` has exactly one winner,
    who re-creates the claim under its own pid, time and `budget_secs` deadline. Returns (the new
    claim identity or None, the renamed-aside file the caller unlinks)."""
    if not _matches(record, run_id, token, script_path, repo_root) or not _claim_expirable(record, claimed):
        return None, None
    aside = claimed.with_name(f"{claimed.name}.adopt.{os.getpid()}")
    try:
        os.replace(claimed, aside)
    except OSError:
        return None, None
    try:
        fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return None, aside
    unclaimed = {k: v for k, v in record.items() if k not in (*_IDENTITY_KEYS, "deadline")}
    identity = _write_claim(fd, unclaimed, budget_secs)
    return identity, aside


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
    if not _matches(_read_rec(repo_root, run_id, claimed=True), run_id, token, script_path, repo_root) or live.exists():
        return False
    try:
        os.replace(claimed, live)
    except OSError:
        return False
    return True


def release_own_ended_claim(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """`release` for a matching claim recorded by this very process and thread: a thread runs one call
    at a time, so a new call there finds the claiming call over (its settle never ran). False for a
    claim any other thread or process holds, however live, and for no matching claim."""
    if not _well_formed(run_id, token, script_path):
        return False
    record = _read_rec(repo_root, run_id, claimed=True)
    if (
        not _matches(record, run_id, token, script_path, repo_root)
        or record.get("pid") != os.getpid()
        or record.get("thread") != threading.get_ident()
    ):
        return False
    return release(repo_root, run_id, token, script_path)


def is_live(repo_root: Path, run_id: str) -> bool:
    """True while the run's record is unclaimed and inside its TTL."""
    record = _read_rec(repo_root, run_id)
    return record is not None and _fresh(record)


def _listed(repo_root: Path, *, claimed: bool) -> list[dict]:
    """Records in the settings-home dir (`claimed/` or the live dir), then those only the legacy
    dir holds (bounded listing; sunset per `_read_legacy`). A run id in both reads from the new root."""
    home = _record_home(repo_root)
    if home is None:
        return []
    sub = (_CLAIMED,) if claimed else ()
    new_dir = (home / _DIR).joinpath(*sub)
    listed = _cached_records(new_dir)
    records = [r for _stem, r in listed]
    seen = {stem for stem, _r in listed}
    common = legacy_record_dir(repo_root)
    if common is not None:
        for path in _bounded_glob((common / _DIR).joinpath(*sub), LEGACY_READ_DEADLINE_MS, reap=not claimed):
            if path.stem not in seen:
                legacy = _read_legacy(repo_root, path.stem, claimed=claimed)
                if legacy is not None:
                    records.append(legacy)
    return records


def live_records(repo_root: Path) -> Iterator[dict]:
    """Every unclaimed, unexpired record, new root first then legacy-only ones."""
    for record in _listed(repo_root, claimed=False):
        if isinstance(record.get("run_id"), str) and _fresh(record):
            yield record


def _predates_halt(repo_root: Path, record: dict) -> bool:
    """True when `record` is the token its run minted before the run's `halted` marker was written:
    the run stopped, so the token holds nothing. A refire's re-mint is newer than the marker and
    holds. Only the listing hides it: `halt_admits`, `escrowed` and `readmit_reviewed` read the
    record directly, so the refire still reclaims. Unknown (no marker, unreadable) is not halted."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        directory = record_dir(Path(repo_root), record["run_id"])
        marker = halted_run.read_strict(directory)
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            return False
        minted = record.get("minted_at")
        return isinstance(minted, (int, float)) and (directory / halted_run.HALTED_FILE).stat().st_mtime >= minted
    except Exception:  # noqa: BLE001 -- unknown: the token holds
        return False


def held_records(repo_root: Path) -> list[dict]:
    """Every record a run still holds: each fresh unclaimed one (`live_records`) plus every
    claimed one (a run mid terminal-commit). One directory listing each; no spawn. A claimed
    record is not freshness-checked here. An unclaimed record its run minted before halting
    (`_predates_halt`) is not listed: a halted run holds nothing."""
    records = [r for r in live_records(repo_root) if not _predates_halt(repo_root, r)]
    records.extend(r for r in _listed(repo_root, claimed=True) if isinstance(r.get("run_id"), str))
    return records


def spent_receipt(repo_root: Path, run_id: object, token: object, script_path: object) -> Optional[dict]:
    """Read-only: the `landed` dict (sha, rows, paths) that `consume(landed=...)` recorded for this
    run, when `token` and `script_path` match the receipt and it is inside its TTL; else None
    (wrong token or script, expired, re-minted since, never landed). Claims nothing, lists nothing."""
    if not _well_formed(run_id, token, script_path):
        return None
    receipt = _read_rec(repo_root, run_id, spent=True)
    if not _matches(receipt, run_id, token, script_path, repo_root):
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
    record = _read_rec(repo_root, run_id, claimed=True)
    return _matches(record, run_id, token, script_path, repo_root) and not _claim_expirable(record, path)


def note_halt_landing(repo_root: Path, run_id: object, token: object, script_path: object, landed: dict) -> None:
    """Record the landed halted-run record commit (`landed`: sha, paths, halted kind) beside the
    token without spending it: the live record stays, so the resumed run's product commit can
    still redeem. Writes nothing when the token no longer matches the live record."""
    if not _well_formed(run_id, token, script_path):
        return
    record = _read_rec(repo_root, run_id)
    target = _record_path(repo_root, run_id, halted=True)
    if not _matches(record, run_id, token, script_path, repo_root) or target is None:
        return
    from coordinator_core.atomic_replace import atomic_write_bytes

    receipt = {k: record.get(k) for k in _RECEIPT_KEYS if record.get(k) is not None}
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
    receipt = _read_rec(repo_root, run_id, halted=True)
    if not _matches(receipt, run_id, token, script_path, repo_root):
        return None
    landed = receipt.get("landed")
    return landed if isinstance(landed, dict) else None


def consume(repo_root: Path, run_id: str, *, landed: Optional[dict] = None) -> None:
    """Remove the run's live and claimed records (the commit landed, or the run ended); a
    missing record is already consumed, and nothing is left for `release` to restore.

    `landed` (a dict: sha, rows, paths) first writes the spent receipt `spent_receipt` reads, from
    the claimed record (else the live one). No record to copy, no receipt."""
    record = _read_rec(repo_root, run_id, claimed=True) or _read_rec(repo_root, run_id)
    if landed is None:
        _write_retired(repo_root, run_id, record)
    else:
        spent = _record_path(repo_root, run_id, spent=True)
        if record is not None and spent is not None:
            from coordinator_core.atomic_replace import atomic_write_bytes

            receipt = {k: record.get(k) for k in _RECEIPT_KEYS if record.get(k) is not None}
            receipt["landed"] = landed
            try:
                spent.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(spent, json.dumps(receipt, sort_keys=True).encode("utf-8"), preserve_mode=False)
            except OSError:  # the commit already landed; a missing receipt only costs the repeat-call answer
                pass
    for claimed in (False, True):
        _unlink_both(repo_root, run_id, claimed=claimed)
    _unlink_both(repo_root, run_id, escrow=True)
    _index("index_remove", repo_root, run_id)
    _wake_stage_waiters(repo_root, run_id)


def _wake_stage_waiters(repo_root: Path, ended_run_id: str) -> None:
    """The end of `ended_run_id` (`consume`, `discard`; never `release`, which restores a running
    run) frees every stage parked behind it (`stage_wait.wake_on_end`). Each freed waiter's token is
    retired, so the spool's live-token coalescing cannot swallow its refire, then its
    `engine-ask {sizing, resume_run_id}` refire is spooled for the warm server's own drain; no
    drain, mint or spawn runs inside this call. Never raises: a failure is a logged advisory."""
    import logging

    log = logging.getLogger(__name__)
    try:
        from coordinator_core.ops.dispatch_emit import stage_wait

        waiters = stage_wait.wake_on_end(repo_root, ended_run_id)
    except Exception:  # noqa: BLE001 -- a wake must never fail the blocker's own removal
        log.warning("stage wake after %s ended failed", ended_run_id, exc_info=True)
        return
    for waiter in waiters:
        try:
            _resume_stage_waiter(repo_root, waiter)
        except Exception:  # noqa: BLE001
            log.warning("stage resume of %s after %s ended failed", waiter.get("run_id"), ended_run_id, exc_info=True)


def _resume_stage_waiter(repo_root: Path, waiter: dict) -> bool:
    """Retire the waiter's token and spool its refire; True when a refire entry was spooled.

    A launcher child (a live escrow) is left untouched and spools nothing: its parked script holds in
    place and re-stages itself once woken, and the staged twin that script writes reads the token back
    from escrow. Trap: retiring it here unlinks the escrow, so the twin is tokenless and the child halts
    at its commit stage with no run token."""
    import logging

    from coordinator_core.ask_spool import record as spool
    from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG

    run_id = waiter["run_id"]
    if escrowed(repo_root, run_id) is not None:
        return False
    sizing_rel = (_read_rec(repo_root, run_id) or {}).get("sizing_rel")
    discard(repo_root, run_id)
    if not (isinstance(sizing_rel, str) and sizing_rel):
        logging.getLogger(__name__).warning("stage-wait %s has no sizing to refire; its token is retired", run_id)
        return False
    if _run_landed(repo_root, run_id):
        logging.getLogger(__name__).warning("stage-wait %s already landed; no refire spooled", run_id)
        return False
    # An absolute `repo` pins the drain to the run's own root; without it the drain launches
    # against the waiting session's anchor repo.
    payload = {
        "tool_name": "Workflow",
        "tool_input": {
            "name": "coordinator:engine-ask",
            "args": {"sizing": sizing_rel, RESUME_RUN_ID_ARG: run_id, "repo": str(Path(repo_root).resolve())},
        },
    }
    session = waiter.get("session_id") or "stage-wait"
    tool_use_id = f"stage-wait-{run_id}-{int(waiter.get('started_at') or 0)}"
    payload.update(session_id=session, tool_use_id=tool_use_id)
    spool.spool_ask(
        str(repo_root),
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        session_id=session,
        tool_use_id=tool_use_id,
        reason="stage-wait: blocker ended",
    )
    return True


def _run_landed(repo_root: Path, run_id: str) -> bool:
    """True when the run's `halted.json` marker reads `landed`; an unreadable marker is not landed."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        marker = halted_run.read_strict(record_dir(Path(repo_root), run_id))
    except (OSError, ValueError):
        return False
    return bool(marker) and marker.get("status") == halted_run.STATUS_LANDED


def discard(repo_root: Path, run_id: str) -> Optional[dict]:
    """Peer-facing release: remove the run's unclaimed record only and return what it held (None
    when absent or the id is malformed). The claimed path is never touched, so a terminal commit
    in flight keeps its claim. No liveness check: discarding a genuinely running run's record
    leaves its later terminal commit nothing to redeem. Not atomic against a concurrent `redeem`:
    a record claimed between the read and the unlink is still reported."""
    path = _record_path(repo_root, run_id)
    if path is None:
        return None
    record = _read_rec(repo_root, run_id)
    _write_retired(repo_root, run_id, record)
    _unlink_both(repo_root, run_id)
    _unlink_both(repo_root, run_id, escrow=True)
    _index("index_remove", repo_root, run_id)
    _wake_stage_waiters(repo_root, run_id)
    return record


def bind_sizing(repo_root: Path, run_id: str, sizing_rel: str) -> bool:
    """Record the sizing a raw ask's run minted on its live record (the gate calls this once the
    sizing exists). A record naming another sizing is rebound to the newer one: the run's ledger
    binding (`run.json`) decides when the run has one, else the newer `sizing_stamp` wins; the
    older never displaces it. True on a write."""
    path = _record_path(repo_root, run_id)
    record = _read_rec(repo_root, run_id)
    if path is None or record is None or not _fresh(record):
        return False
    held = record.get("sizing_rel")
    if held and normalize_sizing_rel(repo_root, str(held)) == normalize_sizing_rel(repo_root, sizing_rel):
        return False
    stamp = sizing_stamp(repo_root, sizing_rel)
    if held:
        ledger_bound = _ledger_bound(repo_root, run_id)
        if ledger_bound:
            if normalize_sizing_rel(repo_root, ledger_bound) != normalize_sizing_rel(repo_root, sizing_rel):
                return False
        else:
            kept = record.get("sizing_stamp")
            if stamp < (kept if isinstance(kept, int) else sizing_stamp(repo_root, str(held))):
                return False
    from coordinator_core.atomic_replace import atomic_write_bytes

    record["sizing_rel"] = sizing_rel
    record["sizing_stamp"] = stamp
    atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"), preserve_mode=False)
    return True


def sizing_stamp(repo_root: Path, sizing_rel: str) -> int:
    """The sizing file's mtime in ns, 0 when it is absent: the order two sizings were minted in.
    A bind takes it before it writes the sizing's register."""
    try:
        return (Path(repo_root) / sizing_rel).stat().st_mtime_ns
    except OSError:
        return 0


def _ledger_bound(repo_root: Path, run_id: str) -> str:
    """The sizing the run's ledger (`run.json`) is bound to, '' when it has none."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        data = json.loads((record_dir(Path(repo_root), run_id) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    bound = data.get("sizing_rel") if isinstance(data, dict) else None
    return bound if isinstance(bound, str) else ""


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


def holds_grind_run_record(repo_root: Path, run_id: object) -> bool:
    """True when `repo_root` holds a live, claimed or spent session-less `kind="grind"` record for `run_id`.

    Record presence only: it admits nothing by itself (the caller still checks the token and
    script). Names the repo a queue-grind run minted in, whose mint carries no session; a
    session-owned record never qualifies. One to three record reads; no spawn; never raises."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        return False
    try:
        for kind in ({}, {"claimed": True}, {"spent": True}):
            record = _read_rec(Path(repo_root), run_id, **kind)
            if record is not None and record.get("kind") == "grind" and not record.get("session_id"):
                return True
        return False
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
