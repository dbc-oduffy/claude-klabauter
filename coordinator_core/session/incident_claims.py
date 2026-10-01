"""
coordinator_core.session.incident_claims -- "who else is on this incident?",
keyed on a caller-chosen file path or slug.

Purpose: a session triaging an outage declares `set_claim(repo_root, key)` and
gets back the other live holders of that key (session id, note, claim time,
messaging address). Claims live under
`<core.sessions_dir(repo_root)>/incident-claims/<sha256(key)[:16]>/<session_id>/`;
each holder owns its own session-id subdirectory, so there is no shared mutex
and a second claimant is never refused.

Holder files (one value per file, LF newlines): `session_id`, `pid`,
`claimed_at`, `key`, `note`.

Negative-spec:
    - Never refuses, blocks, or delays a caller because a peer holds the key.
      No return field or message reads as an instruction to the caller about
      what to do next; this is discovery, not arbitration.
    - Does not route through `claims.claim_artifact` and adds no class to the
      classed-claim store: that store is an exclusive mutex.
    - No symptom or free-text matching and no fuzzy key equivalence; two
      sessions that choose different keys do not meet.
    - Writes only under `<git-common-dir>/coordinator-sessions/incident-claims/`;
      nothing is committed to git and nothing is set automatically.
    - Does not call `claims._write_claim_meta`: its `stage` file has no
      meaning for an incident claim.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from coordinator_core.session import core, liveness, reachability

INCIDENT_CLAIMS_DIRNAME = "incident-claims"
NOTE_MAX_CHARS = 200

_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")
_ANY_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class IncidentHolder:
    key: str
    session_id: str
    note: str
    claimed_at: str
    address: Optional[str]
    is_self: bool


@dataclass(frozen=True)
class IncidentClaimResult:
    key: str
    own_session_id: str
    peers: List[IncidentHolder] = field(default_factory=list)


def normalize_key(key: str) -> str:
    """Canonical spelling of an incident key; raises ValueError on refusal.

    Converts `\\` to `/`, trims whitespace, strips leading `./`, and refuses
    an empty key, an absolute path, a drive letter, or a `..` segment. Case is
    preserved.
    """
    if not isinstance(key, str):
        raise ValueError("incident key must be a string")
    k = key.strip().replace("\\", "/")
    while k.startswith("./"):
        k = k[2:]
    if not k:
        raise ValueError("incident key is empty")
    if k.startswith("/"):
        raise ValueError("incident key must not be an absolute path")
    if _DRIVE_LETTER_RE.match(k):
        raise ValueError("incident key must not carry a drive letter")
    if ".." in k.split("/"):
        raise ValueError("incident key must not contain a '..' segment")
    return k


def _key_hash(norm_key: str) -> str:
    return hashlib.sha256(norm_key.encode("utf-8")).hexdigest()[:16]


def _claims_root(repo_root: str) -> Path:
    return Path(core.sessions_dir(repo_root)) / INCIDENT_CLAIMS_DIRNAME


def _read_field(holder_dir: Path, name: str) -> str:
    try:
        return (holder_dir / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_field(holder_dir: Path, name: str, value: str) -> None:
    (holder_dir / name).write_text(f"{value}\n", encoding="utf-8", newline="\n")


def _clean_note(note: Optional[str]) -> str:
    if not note:
        return ""
    return _ANY_WS_RE.sub(" ", note).strip()[:NOTE_MAX_CHARS]


def _live_holder_dirs(key_dir: Path, repo_root: str) -> List[Path]:
    try:
        entries = sorted(e.path for e in os.scandir(key_dir) if e.is_dir())
    except OSError:
        return []
    live: List[Path] = []
    for p in entries:
        hd = Path(p)
        if not _read_field(hd, "session_id"):
            continue
        if liveness.claim_holder_live(str(hd), repo_root):
            live.append(hd)
    return live


def _holders_from_dirs(dirs: List[Path], own_sid: str) -> List[IncidentHolder]:
    sids = [_read_field(d, "session_id") for d in dirs]
    addresses = reachability.resolve_addresses_bulk([s for s in sids if s]) if sids else {}
    return [
        IncidentHolder(
            key=_read_field(d, "key"),
            session_id=sid,
            note=_read_field(d, "note"),
            claimed_at=_read_field(d, "claimed_at"),
            address=addresses.get(sid) or None,
            is_self=bool(own_sid) and sid == own_sid,
        )
        for d, sid in zip(dirs, sids)
    ]


def set_claim(repo_root: str, key: str, note: Optional[str] = None) -> IncidentClaimResult:
    """Record the caller as a holder of `key`; return the other live holders.

    Always succeeds for the caller. Raises ValueError on a refused key or an
    unresolvable session id (no nameless holder is ever written). Dead sibling
    holders under the key are pruned best effort.
    """
    norm = normalize_key(key)
    sid = core.resolve_session_id(repo_root)
    if not sid:
        raise ValueError("session id is unresolvable; incident claim not recorded")

    key_dir = _claims_root(repo_root) / _key_hash(norm)
    own_dir = key_dir / sid
    own_dir.mkdir(parents=True, exist_ok=True)
    _write_field(own_dir, "pid", str(os.getpid()))
    _write_field(own_dir, "claimed_at", core.now_iso())
    _write_field(own_dir, "key", norm)
    _write_field(own_dir, "note", _clean_note(note))
    _write_field(own_dir, "session_id", sid)

    live = {d.name: d for d in _live_holder_dirs(key_dir, repo_root)}
    try:
        for entry in os.scandir(key_dir):
            if entry.is_dir() and entry.name != sid and entry.name not in live:
                shutil.rmtree(entry.path, ignore_errors=True)
    except OSError:
        pass

    peer_dirs = [d for name, d in live.items() if name != sid]
    peers = _holders_from_dirs(peer_dirs, sid)
    return IncidentClaimResult(key=norm, own_session_id=sid, peers=peers)


def release_claim(repo_root: str, key: str) -> bool:
    """Remove the caller's own holder directory; True iff it existed."""
    norm = normalize_key(key)
    sid = core.resolve_session_id(repo_root)
    if not sid:
        return False
    own_dir = _claims_root(repo_root) / _key_hash(norm) / sid
    if not own_dir.is_dir():
        return False
    shutil.rmtree(own_dir, ignore_errors=True)
    return not own_dir.exists()


def list_peers(repo_root: str, key: Optional[str] = None) -> List[IncidentHolder]:
    """Live holders of `key`, or of every incident key in the repo when None.

    Includes the caller's own holder (`is_self` True). Read-only.
    """
    root = _claims_root(repo_root)
    if key is None:
        try:
            key_dirs = sorted(Path(e.path) for e in os.scandir(root) if e.is_dir())
        except OSError:
            return []
    else:
        key_dirs = [root / _key_hash(normalize_key(key))]

    dirs: List[Path] = []
    for kd in key_dirs:
        dirs.extend(_live_holder_dirs(kd, repo_root))
    return _holders_from_dirs(dirs, core.resolve_session_id(repo_root))
