"""
coordinator_core.group_em.nomination -- in-engine read and claim of the Group EM nomination
record, mirroring (never importing, never shelling out to)
`coordinator-content-repo:coordinator/bin/group-em-nomination.py`.

Spec backlink: docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md § C3
Read side: docs/plans/2026-10-01-groupem-standing-op.md (M06.C0)

READER AND CLAIMER (2026-10-01): this module is also the reader. `who()` and `standing()` answer
"who holds it, and are they live" off the SAME `is_live` join `claim()` decides on, so the read
and the claim cannot disagree about a holder. They never write, lock or claim.

Purpose: exactly one Group EM per repo is a filesystem invariant expressed by ONE JSON file per
repo under ``<settings-home>/state/group-em/<repo-key>.json`` -- machine-global, in NEITHER
repo's tree. The standing itself is box-wide: every claim also writes
``<settings-home>/state/group-em/box/holder.json``, read by `box_holder()`, and a per-repo record
naming any other session is stale. This module is the in-plane reader/claimer for ``groupem.enter`` (C5); the record
shape and repo-key derivation are mirrored from the reference script above, not imported from it
-- that repo's shell-out carve-out list does not name this site.

NEGATIVE SPEC -- what this deliberately does NOT do:

  - It NEVER refuses. Group EM standing is taken, not requested (DoE doctrine): a live or
    unregistered incumbent is displaced and reported in `displaced_holder*`; a positively dead
    one (`live_reason: "pid_not_running"`) is also reported in `replaced_holder`.

  - Liveness is NEVER read off a pid recorded in the nomination record itself. See
    `state/lessons/2026-08-29-a-claim-records-pid-is-not-a-liveness-signal.yaml`: a pid captured
    by a past writer is only ever confirmed via the harness's own session registry, joined on
    `session_id` -- here that join is `coordinator_core.session.liveness.session_live`, which
    already performs the registry-first, `stable_pid_alive`-confirmed check this module would
    otherwise have to reimplement. The SAME TTL-cached registry read `session_live` already
    performed (`session.liveness._cached_registry_lookup`) is consulted ONLY to distinguish the
    two not-live reasons (`no_registry_record` vs `pid_not_running`) for the report -- never as
    an input to the liveness verdict itself. Deliberately NOT a second, uncached
    `harness_registry.lookup` (== a second full `snapshot()` glob-and-parse of the registry
    directory): see `is_live`'s own docstring (Review: overengineering-reviewer, Finding 8).

  - The record MUST NOT carry a `pid`, for the same reason. See `_build_record`.

  - No directory scan for the record: `_record_path` is O(1)-deterministic from `repo_root`,
    exactly like the reference.

  - Writes are atomic (write-temp, `os.replace`) -- this machine runs 50-70 concurrent sessions;
    a torn record is a real outcome. See `_write_json_atomic`.

KNOWN GAP, carried forward from the reference and not fixed here: `claim()` has a TOCTOU window
between its read of the existing record and its write of a new one -- no lock is held. Left open
deliberately, matching the reference's own documented limitation: this is an operator/entry-path
verb, not a background daemon, and no cross-platform lock is taken here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import human_entry, session_registry
from coordinator_core.session import liveness as _liveness
from coordinator_core.session.liveness import session_live

SCHEMA_VERSION = 1

#: Generator-provenance declaration (coordinator_core/ops/generator_census). `_write_json_
#: atomic` writes under `settings_home() / "state" / "group-em"` --
#: `settings_home()` resolves to `${CLAUDE_HOME:-$HOME}/.coordinator-claude-
#: settings` (or `COORDINATOR_SETTINGS_HOME` when set), never a path inside
#: this repo's own tracked tree. Same disposition as `async_hook_status.py`'s
#: `claude_config_dir()`-rooted marker: an operator-home cache, not a repo
#: artifact.
GENERATES = []


def _safe_stem(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in "-_")


def repo_key(repo_root: str) -> str:
    """Deterministic, collision-resistant filename stem for a repo root.

    Mirrors the reference's `_repo_key` exactly: case/separator-normalised path, SHA1-hashed
    (first 10 hex chars) appended to a sanitised trailing path component, so two repo roots
    sharing a basename never collide on this key.

    NOT NORMALISED, DELIBERATELY UNCLAIMED (same as the reference): a mapped drive letter versus
    its UNC equivalent, or two drive letters mapped to the same network share, resolve to
    different keys here -- unifying those needs filesystem-level identity (volume GUID / stat
    device+inode) this function does not perform.

    Public: this is also the derivation `group_em_enter.py`'s baseline leg calls to key its
    baseline snapshot file, so a future change here renames every consumer's on-disk key --
    keep this the one definition, never duplicated at a call site.
    """
    normalised = os.path.normcase(os.path.normpath(repo_root))
    digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:10]
    stem = _safe_stem(Path(repo_root).name) or "repo"
    return f"{stem}-{digest}"


_repo_key = repo_key


def _record_path(repo_root: str, directory: Optional[Path] = None) -> Path:
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / f"{repo_key(repo_root)}.json"


def _box_record_path(directory: Optional[Path] = None) -> Path:
    """The ONE box-wide holder record. In its own subdirectory so nothing that lists the per-repo
    records ever reads it as one."""
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / "box" / "holder.json"


def _write_json_atomic(target: Path, record: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _paths_match(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def read_record(repo_root: str, directory: Optional[Path] = None) -> Optional[dict]:
    path = _record_path(repo_root, directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not _paths_match(str(data.get("repo_root") or ""), repo_root):
        return None
    return data


@dataclass(frozen=True)
class LivenessResult:

    live: bool
    live_reason: str


def is_live(record: dict) -> LivenessResult:
    session_id = str(record.get("session_id") or "")
    if not session_id:
        return LivenessResult(False, "no_registry_record")
    live = session_live(session_id)
    if live:
        return LivenessResult(True, "live")
    row = _liveness._cached_registry_lookup(session_id)
    if row is None:
        return LivenessResult(False, "no_registry_record")
    return LivenessResult(False, "pid_not_running")


def _build_record(
    repo_root: str,
    session_id: str,
    peer_name: Optional[str],
    nominated_by: Optional[str],
    note: Optional[str] = None,
    entry_evidence: Optional[dict] = None,
) -> dict:
    record = {
        "version": SCHEMA_VERSION,
        "repo_root": repo_root,
        "session_id": session_id,
        "peer_name": peer_name,
        "nominated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nominated_by": nominated_by,
        "note": note,
    }
    if entry_evidence:
        record["entered_via"] = "human-slash-command"
        record["entry_evidence"] = entry_evidence
    return record


def _write_claim(repo_root: str, record: dict, directory: Optional[Path]) -> None:
    """The standing is box-wide: the entering repo's record and the box record are the same claim,
    so a per-repo record another session holds elsewhere is stale, never a rival."""
    _write_json_atomic(_record_path(repo_root, directory), record)
    _write_json_atomic(_box_record_path(directory), record)


class NotHumanEnteredError(RuntimeError):
    """The claim carries no prompt_id or a malformed session id; no record was written."""


def pending_evidence(session_id: str, prompt_id: Optional[str], now: Optional[float] = None) -> dict:
    """The claim-time evidence stub, or `NotHumanEnteredError`. Verification is deferred to
    read time (`entry_status`): the hook fires before the harness writes the transcript entry."""
    if not human_entry.valid_session_id(session_id) or not isinstance(prompt_id, str) or not prompt_id:
        raise NotHumanEnteredError(human_entry.REFUSAL_MESSAGE)
    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return {
        "status": "pending",
        "prompt_id": prompt_id,
        "claimed_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _verdict_cache_path(record: dict, directory: Optional[Path] = None) -> Optional[Path]:
    """Per-repo entry-verdict cache beside the record (own subdirectory, so nothing that lists
    records ever reads it as one). Keyed inside by (session_id, prompt_id); a new claim simply
    misses and overwrites it."""
    repo_root = record.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return None
    record_path = _record_path(repo_root, directory)
    return record_path.parent / "entry-verdicts" / record_path.name


def entry_status(
    record: dict, now: Optional[float] = None, directory: Optional[Path] = None
) -> dict:
    """`human_entry.resolve_entry_evidence` for `record` at `now` (default: the wall clock),
    through the verdict cache: a verified or rejected standing costs one small file read."""
    return human_entry.resolve_entry_evidence(
        record,
        time.time() if now is None else now,
        cache_path=_verdict_cache_path(record, directory),
    )


def read_authoritative(
    repo_root: str, directory: Optional[Path] = None, now: Optional[float] = None
) -> Optional[dict]:
    """The nomination record only when its entry evidence is verified; None for no record, a
    pending claim or a rejected one. Every authority read of the standing goes through here."""
    record = read_record(repo_root, directory)
    if record is None or entry_status(record, now, directory)["status"] != "verified":
        return None
    return record


def box_holder(directory: Optional[Path] = None, now: Optional[float] = None) -> Optional[dict]:
    """The box-wide holder, verified exactly as `read_authoritative` verifies a per-repo record,
    plus ``live``/``live_reason``; None for no record, a pending claim or a rejected one. A
    per-repo record whose ``session_id`` differs from this holder's is stale."""
    try:
        record = json.loads(_box_record_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or entry_status(record, now, directory)["status"] != "verified":
        return None
    liveness = is_live(record)
    return {**record, "live": liveness.live, "live_reason": liveness.live_reason}


def claim(
    repo_root_str: str,
    session_id: str,
    *,
    peer_name: Optional[str] = None,
    nominated_by: Optional[str] = None,
    note: Optional[str] = None,
    directory: Optional[Path] = None,
    prompt_id: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Read the nomination record for `repo_root_str` and return a verdict -- never a unilateral
    supersede of a LIVE holder, or one merely UNACCOUNTED FOR.

    The one refusal: a claim with no `prompt_id` (or a non-token session id) raises
    ``NotHumanEnteredError`` before any write. The record stores `entry_evidence`
    `{status: pending, prompt_id, claimed_at}`; `entry_status` verifies it at read time against
    the claimant's own transcript. Release paths (stand-down) do not go through here.

    Otherwise NEVER REFUSES. Group EM standing is taken, not requested: the last claimant holds it. A
    record naming another session is overwritten whether that holder is live, dead or
    unaccounted for, and the takeover is reported in ``displaced_holder`` /
    ``displaced_holder_live`` / ``displaced`` (who was taken from, and whether still running --
    a live one is owed a message). A holder whose pid is positively dead additionally fills
    ``replaced_holder``. ``superseded_incumbent`` is retained as a key and is always None.
    Result keys: ``{claimed, holder, already_held, superseded_incumbent, replaced_holder}``,
    plus the three ``displaced*`` keys when another session was taken from.

    `repo_root_str` is normalised (resolved to an absolute path) before use, matching the
    reference's own `_normalised_repo_root`.

    `peer_name` and `nominated_by` are write-side-only fields (Review: overengineering-reviewer,
    Finding 9, nit): neither is read by any branch of the five-case decision above -- claiming,
    refusing, and replacing all turn on `session_id` and `is_live`'s verdict alone. They exist
    here purely for on-disk format parity with `coordinator-content-repo:coordinator/bin/group-em-
    nomination.py`, the OTHER writer of this same record shape (module docstring, line 1) --
    dropping them would desync the two writers' record shape even though this reader never
    consumes them. Do not "clean them up" as unused.
    """
    entry_evidence = pending_evidence(session_id, prompt_id, now)
    repo_root = str(Path(repo_root_str).resolve())
    existing = read_record(repo_root, directory)

    if existing is None:
        record = _build_record(repo_root, session_id, peer_name, nominated_by, note, entry_evidence)
        _write_claim(repo_root, record, directory)
        return {
            "claimed": True,
            "holder": session_id,
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": None,
        }

    incumbent_sid = str(existing.get("session_id") or "")
    if incumbent_sid == session_id:
        record = _build_record(repo_root, session_id, peer_name, nominated_by, note, entry_evidence)
        _write_claim(repo_root, record, directory)
        return {
            "claimed": True,
            "holder": session_id,
            "already_held": True,
            "superseded_incumbent": None,
            "replaced_holder": None,
        }

    liveness = is_live(existing)
    displaced = {
        "session_id": incumbent_sid,
        "peer_name": existing.get("peer_name"),
        "nominated_at": existing.get("nominated_at"),
        "nominated_by": existing.get("nominated_by"),
        "live": liveness.live,
        "live_reason": liveness.live_reason,
    }
    record = _build_record(repo_root, session_id, peer_name, nominated_by, note, entry_evidence)
    record["displaced_holder"] = incumbent_sid
    record["displaced_holder_live"] = liveness.live
    replaced_holder = None
    if liveness.live_reason == "pid_not_running":
        replaced_holder = displaced
        record["replaced_holder_session_id"] = incumbent_sid
        record["replaced_holder_name"] = displaced["peer_name"]
        record["replaced_nominated_at"] = displaced["nominated_at"]
        record["replaced_live_reason"] = liveness.live_reason
    _write_claim(repo_root, record, directory)
    return {
        "claimed": True,
        "holder": session_id,
        "already_held": False,
        "superseded_incumbent": None,
        "replaced_holder": replaced_holder,
        "displaced_holder": incumbent_sid,
        "displaced_holder_live": liveness.live,
        "displaced": displaced,
    }


def who(repo_root: str, directory: Optional[Path] = None) -> Optional[dict]:
    """Return the nomination record plus ``live``/``live_reason`` from `is_live`, or None when no
    record is on file. Read-only; `repo_root` is normalised exactly as `claim()` does."""
    repo_root = str(Path(repo_root).resolve())
    record = read_record(repo_root, directory)
    if record is None:
        return None
    liveness = is_live(record)
    annotated = dict(record)
    annotated["live"] = liveness.live
    annotated["live_reason"] = liveness.live_reason
    annotated["entry_status"] = entry_status(record, directory=directory)["status"]
    return annotated


def _session_id_for_name(name: str) -> Optional[str]:
    """Resolve a registry name to a session id; None unless exactly one session carries it. A
    name join, not a liveness join."""
    if not name:
        return None
    matches = {row.session_id for row in session_registry.read_rows() if row.name == name}
    if len(matches) != 1:
        return None
    return matches.pop()


def standing(
    repo_root: str, peer: str, directory: Optional[Path] = None
) -> Optional[dict]:
    """`who()` plus ``standing``: ``"live"`` / ``"not_live"`` when `peer` (a session id, or a
    registry name resolving to exactly one session id) is the recorded holder, else
    ``"no_match"``. None when no record is on file."""
    record = who(repo_root, directory)
    if record is None:
        return None
    holder = str(record.get("session_id") or "")
    matches = bool(peer) and (peer == holder or _session_id_for_name(peer) == holder)
    if not matches or record["entry_status"] != "verified":
        record["standing"] = "no_match"
    elif record["live"]:
        record["standing"] = "live"
    else:
        record["standing"] = "not_live"
    return record
