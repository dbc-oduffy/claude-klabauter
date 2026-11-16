"""Ref-lock ownership, typed refusal and dead-writer recovery for `git_objects._cas_ref`.

A `<ref>.lock` taken by the engine carries an owner record (`<gitdir>/coordinator-ref-locks/`) and
a live in-process holder. A lock whose writer is provably gone is completed from the reflog when
its sha is the reflog head and a child of the ref, else moved aside; both leave one line in
`<gitdir>/coordinator-stale-ref-locks/recoveries.jsonl`. An ownerless lock is git.exe's: reported,
never touched. Recovery runs only on the refusal path and at engine restart (`sweep_dead_owner_locks`).

Invariant: this module is imported by `git_objects`, so every `coordinator_core.git` import is
function-local.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core import dispatch_abandon

# git holds a ref lock for milliseconds; a lock older than this has no live writer that is merely slow.
STALE_REF_LOCK_FLOOR_SECS = 30

OWNER_DIR = "coordinator-ref-locks"
RECOVERY_DIR = "coordinator-stale-ref-locks"
RECOVERY_LOG = "recoveries.jsonl"

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_TOKENS = itertools.count(1)
_HOLDERS_GUARD = threading.Lock()
_HOLDERS: Dict[str, "RefLockHolder"] = {}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _encode_ref(ref: str) -> str:
    return ref.replace("%", "%25").replace("/", "%2F")


def _decode_ref(name: str) -> str:
    return name.replace("%2F", "/").replace("%25", "%")


def _lock_path(gitdir: Path, ref: str) -> Path:
    return gitdir / (ref + ".lock")


def _owner_path(gitdir: Path, ref: str) -> Path:
    return gitdir / OWNER_DIR / (_encode_ref(ref) + ".json")


def _read_lock_text(lock: Path) -> Optional[str]:
    """Stripped lock content ("" for an empty lock); None when unreadable (Windows reports a
    lock mid-unlink as PermissionError) or absent."""
    try:
        return lock.read_bytes().decode("ascii", errors="replace").strip()
    except OSError:
        return None


def _lock_age_secs(lock: Path) -> int:
    try:
        return max(0, int(time.time() - lock.stat().st_mtime))
    except OSError:
        return 0


class RefLockHeld(RuntimeError):
    """`<ref>.lock` exists, so the ref could not be locked; this is not a compare mismatch.
    `stale` is True once the lock is older than `STALE_REF_LOCK_FLOOR_SECS` (recovery already
    declined it: live or unprovable writer); a fresh one is an ordinary peer race and retryable."""

    def __init__(self, ref: str, lock_path: Path, age_secs: int, content: Optional[str], stale: bool):
        self.ref = ref
        self.lock_path = Path(lock_path)
        self.age_secs = age_secs
        self.content = content
        self.stale = stale
        shown = "unreadable" if content is None else (content or "empty")
        super().__init__(
            f"cannot lock ref {ref}: {self.lock_path} exists (age {age_secs}s, content {shown})"
        )

    @classmethod
    def from_lock(cls, gitdir: Path, ref: str) -> "RefLockHeld":
        lock = _lock_path(gitdir, ref)
        age = _lock_age_secs(lock)
        return cls(ref, lock, age, _read_lock_text(lock), age >= STALE_REF_LOCK_FLOOR_SECS)


class RefLockHolder:
    """A live in-process owner of one `<ref>.lock`. `released` flips (under the registry guard)
    when the dispatcher abandoned the op and `release_abandoned` freed the lock: from then on the
    path may belong to a peer, so the holder must neither rename nor unlink it."""

    def __init__(self, gitdir: Path, ref: str, token: str) -> None:
        self.gitdir = gitdir
        self.ref = ref
        self.token = token
        self.lock_path = _lock_path(gitdir, ref)
        self.thread = threading.current_thread()
        self.abandon = dispatch_abandon.ACTIVE.get()
        self.released = False

    def valid(self) -> bool:
        """False once the lock was released or the dispatcher abandoned the op; the replace
        must not run."""
        return not self.released and not (self.abandon is not None and self.abandon.is_set())

    def reapable(self) -> bool:
        return self.released or not self.thread.is_alive() or (
            self.abandon is not None and self.abandon.is_set()
        )

    def close(self) -> bool:
        """Unregister. True when this holder still owned the lock file (the caller may unlink it);
        False when `release_abandoned` already freed it."""
        with _HOLDERS_GUARD:
            _HOLDERS.pop(self.token, None)
            owned = not self.released
        if owned:
            try:
                _owner_path(self.gitdir, self.ref).unlink()
            except OSError:
                pass
        return owned


def register_holder(gitdir: Path, ref: str) -> RefLockHolder:
    """Record the caller as owner of the `<ref>.lock` it just created with O_EXCL. Registered
    before the owner file is written, so a reader of the file always finds the token."""
    token = f"{os.getpid()}-{threading.get_ident()}-{next(_TOKENS)}"
    holder = RefLockHolder(gitdir, ref, token)
    with _HOLDERS_GUARD:
        _HOLDERS[token] = holder
    owner = _owner_path(gitdir, ref)
    try:
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text(
            json.dumps({"pid": os.getpid(), "token": token, "started": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass  # no owner record: a later reader treats the lock as ownerless and only reports it
    return holder


def _append_record(gitdir: Path, record: dict) -> None:
    log = gitdir / RECOVERY_DIR / RECOVERY_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as fh:
            fh.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass


def _read_owner(owner: Path) -> Optional[tuple]:
    try:
        info = json.loads(owner.read_text(encoding="utf-8"))
        return int(info["pid"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _writer_gone(pid: int, token: str) -> bool:
    """True only on proof: a dead pid, or this pid with no live registered holder. Doubt is alive."""
    if pid == os.getpid():
        with _HOLDERS_GUARD:
            holder = _HOLDERS.get(token)
            if holder is None:
                return True
            return holder.reapable()
    from coordinator_core.git.index_write import _pid_gone

    return _pid_gone(pid)


def _claim_owner(owner: Path) -> bool:
    """Rename claims the recovery: of two racing recoverers only one wins the owner file."""
    claimed = owner.with_name(f"{owner.name}.reap-{os.getpid()}-{threading.get_ident()}")
    try:
        os.replace(owner, claimed)
    except OSError:
        return False
    try:
        claimed.unlink()
    except OSError:
        pass
    return True


def _restore_owner(owner: Path, pid: int, token: str) -> None:
    """Put back an owner record claimed by a recovery that could not resolve the lock, so the lock
    is not demoted to ownerless (never recovered) by a transient OSError."""
    try:
        if not owner.exists():
            owner.write_text(
                json.dumps({"pid": pid, "token": token, "started": time.time()}), encoding="utf-8"
            )
    except OSError:
        pass


def _reflog_head_new(gitdir: Path, ref: str) -> Optional[str]:
    """New-sha of the newest reflog line, from the tail of the file (one read, no spawn)."""
    try:
        with open(gitdir / "logs" / ref, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None
    fields = lines[-1].split(" ", 2)
    return fields[1] if len(fields) >= 2 and _SHA_RE.fullmatch(fields[1]) else None


def _first_parent(gitdir: Path, sha: str) -> tuple:
    """(found, parent): `found` False when `sha` is not a readable commit; `parent` None for a root."""
    from coordinator_core.git import git_objects

    obj = git_objects.read_object(gitdir, sha)
    if obj is None or obj[0] != "commit":
        return False, None
    for raw in obj[1].split(b"\n\n", 1)[0].split(b"\n"):
        if raw.startswith(b"parent "):
            return True, raw[7:].decode("ascii", errors="replace").strip()
    return True, None


def _current_ref(gitdir: Path, ref: str) -> Optional[str]:
    from coordinator_core.git import git_objects

    return git_objects.read_ref_loose_or_packed(gitdir, ref)


def _resolve_lock(gitdir: Path, ref: str, age_secs: int, pid: Optional[int]) -> Optional[str]:
    """Complete or move aside a lock whose writer is gone; the owner record is already claimed."""
    lock = _lock_path(gitdir, ref)
    content = _read_lock_text(lock)
    if content is None:
        return None
    record = {
        "ts": _utc_stamp(), "ref": ref, "lock_path": str(lock),
        "age_secs": age_secs, "pid": pid, "content": content,
    }
    outcome = "moved-aside"
    if _SHA_RE.fullmatch(content):
        found, parent = _first_parent(gitdir, content)
        current = _current_ref(gitdir, ref)
        if found and parent == current and _reflog_head_new(gitdir, ref) == content:
            outcome = "completed"
    if outcome == "completed":
        try:
            if lock.read_bytes() != content.encode("ascii") + b"\n":
                lock.write_bytes(content.encode("ascii") + b"\n")
            os.replace(lock, gitdir / ref)
        except OSError:
            return None
        record["outcome"] = outcome
        _append_record(gitdir, record)
        return f"completed {ref} -> {content} from stale lock {lock} (age {age_secs}s)"
    dest = gitdir / RECOVERY_DIR / f"{_encode_ref(ref)}-{_utc_stamp()}.lock"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(lock, dest)
    except OSError:
        return None
    record["outcome"] = outcome
    record["moved_to"] = str(dest)
    _append_record(gitdir, record)
    return f"moved-aside stale lock {lock} (content {content or 'empty'}, age {age_secs}s) to {dest}"


def recover_stale_ref_lock(gitdir: Path, ref: str) -> Optional[str]:
    """Recover `<ref>.lock` when it is older than `STALE_REF_LOCK_FLOOR_SECS` and its owner
    record proves the writer gone. Returns a one-line description starting "completed" or
    "moved-aside" (also appended to recoveries.jsonl); None when the lock is absent, fresh,
    ownerless (git.exe's), live, or doubtful. Never raises on an unreadable or racing lock."""
    gitdir = Path(gitdir)
    lock = _lock_path(gitdir, ref)
    if not lock.exists():
        return None
    from coordinator_core.ops.reap_stale_locks import stale_and_stable

    if not stale_and_stable(lock, STALE_REF_LOCK_FLOOR_SECS, 0.0, True):
        return None
    owner = _owner_path(gitdir, ref)
    info = _read_owner(owner)
    if info is None or not _writer_gone(*info):
        return None
    age = _lock_age_secs(lock)
    if not _claim_owner(owner):
        return None
    msg = _resolve_lock(gitdir, ref, age, info[0])
    if msg is None:
        _restore_owner(owner, *info)
    return msg


def sweep_dead_owner_locks(gitdir: Path) -> List[str]:
    """Finish or move aside every engine-owned ref lock whose owner pid is gone, with no age
    floor (a dead pid is proof). Lists only the owner dir (absent on a clean repo); an owner
    record whose lock is already gone is cleared. Returns the recovery descriptions."""
    gitdir = Path(gitdir)
    out: List[str] = []
    try:
        names = [n for n in os.listdir(gitdir / OWNER_DIR) if n.endswith(".json")]
    except OSError:
        return out
    for name in names:
        ref = _decode_ref(name[: -len(".json")])
        owner = gitdir / OWNER_DIR / name
        info = _read_owner(owner)
        if info is None or not _writer_gone(*info):
            continue
        if not _claim_owner(owner):
            continue
        lock = _lock_path(gitdir, ref)
        if not lock.exists():
            continue
        msg = _resolve_lock(gitdir, ref, _lock_age_secs(lock), info[0])
        if msg:
            out.append(msg)
        else:
            _restore_owner(owner, *info)
    return out


def release_abandoned(event: threading.Event) -> List[dict]:
    """Free every ref lock held by a handler bound to `event` (the dispatcher just set it).
    Marks each holder released, claims its owner record when it is still the holder's own, and
    unlinks the lock. A record carries `reflog_ahead` when the reflog head names the lock's sha
    but the ref never took it. Returns the records (also appended to recoveries.jsonl)."""
    with _HOLDERS_GUARD:
        targets = [h for h in _HOLDERS.values() if h.abandon is event and not h.released]
        for h in targets:
            h.released = True
    records: List[dict] = []
    for h in targets:
        owner = _owner_path(h.gitdir, h.ref)
        info = _read_owner(owner)
        if info is not None and info[1] == h.token:
            _claim_owner(owner)
        content = _read_lock_text(h.lock_path)
        record = {
            "ts": _utc_stamp(), "ref": h.ref, "lock_path": str(h.lock_path),
            "age_secs": _lock_age_secs(h.lock_path), "pid": os.getpid(),
            "content": content, "outcome": "released-abandoned",
            "reflog_ahead": bool(
                content and _SHA_RE.fullmatch(content)
                and _reflog_head_new(h.gitdir, h.ref) == content
                and _current_ref(h.gitdir, h.ref) != content
            ),
        }
        try:
            h.lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            record["unlink_failed"] = True
        _append_record(h.gitdir, record)
        records.append(record)
    return records
