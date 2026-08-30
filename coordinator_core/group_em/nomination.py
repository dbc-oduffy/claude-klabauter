"""
coordinator_core.group_em.nomination -- in-engine read and claim of the Group EM nomination
record, mirroring (never importing, never shelling out to)
`DoE-claude:coordinator/bin/group-em-nomination.py`.

Spec backlink: docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md § C3

Purpose: exactly one Group EM per repo is a filesystem invariant expressed by ONE JSON file per
repo under ``<settings-home>/state/group-em/<repo-key>.json`` -- machine-global, in NEITHER
repo's tree. This module is the in-plane reader/claimer for ``groupem.enter`` (C5); the record
shape and repo-key derivation are mirrored from the reference script above, not imported from it
-- that repo's shell-out carve-out list does not name this site.

NEGATIVE SPEC -- what this deliberately does NOT do:

  - It NEVER auto-supersedes a LIVE incumbent, or one merely UNACCOUNTED FOR
    (`live_reason: "no_registry_record"` -- absence of registry evidence, not evidence of
    absence, on a fleet that is multi-machine). Both report `superseded_incumbent` for the PM to
    act on; no code path writes over either one's record. This is the exact 2026-08-30 DoE
    failure this chunk exists to not repeat for a LIVE crown, and the multi-machine ambiguity
    means an unaccounted-for one gets the same treatment.

  - It DOES auto-replace an incumbent with POSITIVE evidence of death
    (`live_reason: "pid_not_running"` -- a harness registry row exists for that session_id and
    its pid is confirmed not running). A dead-but-unreaped record is this mode's steady state,
    not an anomaly: refusing every one forever would fire on essentially every invocation after
    the first, turning the guard into a prompt-to-continue. The replacement is reported LOUDLY,
    in its own `replaced_holder` field, never folded into `superseded_incumbent` -- that name
    means "refused, a human decides," and reusing it here would recreate the exact ambiguity the
    two-field split exists to remove.

  - Liveness is NEVER read off a pid recorded in the nomination record itself. See
    `state/lessons/2026-08-29-a-claim-records-pid-is-not-a-liveness-signal.yaml`: a pid captured
    by a past writer is only ever confirmed via the harness's own session registry, joined on
    `session_id` -- here that join is `coordinator_core.session.liveness.session_live`, which
    already performs the registry-first, `stable_pid_alive`-confirmed check this module would
    otherwise have to reimplement. `harness_registry.lookup` is consulted ONLY to distinguish the
    two not-live reasons (`no_registry_record` vs `pid_not_running`) for the report -- never as
    an input to the liveness verdict itself.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.session import harness_registry
from coordinator_core.session.liveness import session_live

SCHEMA_VERSION = 1


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


# Back-compat alias for any internal caller still spelling the private name.
_repo_key = repo_key


def _record_path(repo_root: str, directory: Optional[Path] = None) -> Path:
    """Deterministic record path -- no directory scan needed to find it."""
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / f"{repo_key(repo_root)}.json"


def _write_json_atomic(target: Path, record: dict) -> None:
    """Write-temp then `os.replace` -- a concurrent reader sees the old record or the new one,
    never a partial one. A raised exception propagates; this is the nomination record of record,
    never best-effort telemetry, so a caller that believes a write succeeded when it did not
    would be reading exactly the "silent lie" this module exists to prevent.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
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
    """The nomination record for `repo_root`, or None if absent, unreadable, or key-collided.

    A `repo_root` mismatch inside the record (a hash-stem collision, or a stale record surviving
    a repo move) is treated as "no nomination" rather than trusted.
    """
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
    """A liveness verdict plus the distinguishing reason -- never a bare bool.

    `live_reason` is one of `"live"`, `"no_registry_record"`, `"pid_not_running"` -- the same
    three-way distinction the reference's `who()` surfaces, so a receiver debugging a
    not-live-looking incumbent can tell a renamed/reaped registry entry apart from a genuinely
    dead process.
    """

    live: bool
    live_reason: str


def is_live(record: dict) -> LivenessResult:
    """Liveness is a join against the harness registry, NEVER a stored pid.

    The verdict itself comes from `coordinator_core.session.liveness.session_live`, which
    performs the registry-first, `stable_pid_alive`-confirmed check on `session_id` alone --
    the record's own `session_id` field is the only thing read here, never a `pid`.
    `harness_registry.lookup` is consulted ONLY to attach the not-live reason for the report.
    """
    session_id = str(record.get("session_id") or "")
    if not session_id:
        return LivenessResult(False, "no_registry_record")
    live = session_live(session_id)
    if live:
        return LivenessResult(True, "live")
    row = harness_registry.lookup(session_id)
    if row is None:
        return LivenessResult(False, "no_registry_record")
    return LivenessResult(False, "pid_not_running")


def _build_record(
    repo_root: str,
    session_id: str,
    peer_name: Optional[str],
    nominated_by: Optional[str],
) -> dict:
    """The on-disk record shape -- deliberately NEVER carries a `pid`.

    See the module docstring's negative spec: a pid captured by this writer would be dead the
    moment this call returns, and reading liveness off it would call every nomination stale
    within seconds -- the most dangerous failure direction for a mutual-exclusion check.
    """
    return {
        "version": SCHEMA_VERSION,
        "repo_root": repo_root,
        "session_id": session_id,
        "peer_name": peer_name,
        "nominated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nominated_by": nominated_by,
    }


def claim(
    repo_root_str: str,
    session_id: str,
    *,
    peer_name: Optional[str] = None,
    nominated_by: Optional[str] = None,
    directory: Optional[Path] = None,
) -> dict:
    """Read the nomination record for `repo_root_str` and return a verdict -- never a unilateral
    supersede of a LIVE holder, or one merely UNACCOUNTED FOR.

    Five cases, all returning a dict shaped
    ``{claimed, holder, already_held, superseded_incumbent, replaced_holder}``:

      - no record on file                       -> claim it; ``{claimed: True, holder: session_id,
                                                       already_held: False,
                                                       superseded_incumbent: None,
                                                       replaced_holder: None}``
      - record already names `session_id`       -> ``{claimed: True, holder: session_id,
                                                       already_held: True, superseded_incumbent:
                                                       None, replaced_holder: None}`` (record is
                                                       refreshed, not left untouched, so
                                                       `nominated_at` stays current -- same
                                                       idempotent-refresh shape as the reference's
                                                       `self_refresh`)
      - record names another session, LIVE      -> DO NOT CLAIM. Direction-class, a human's call.
                                                       ``{claimed: False, holder: <them>,
                                                       already_held: False, superseded_incumbent:
                                                       {..., live: True, live_reason: "live"},
                                                       replaced_holder: None}``
      - record names another session, PID-DEAD  -> AUTO-REPLACE. ``live_reason ==
                                                       "pid_not_running"`` is POSITIVE evidence of
                                                       death -- the harness registry has a row for
                                                       that session_id and its pid is not running,
                                                       so nobody is on the other side. Claim it,
                                                       loudly: ``{claimed: True, holder: session_id,
                                                       already_held: False, superseded_incumbent:
                                                       None, replaced_holder: {..., live: False,
                                                       live_reason: "pid_not_running"}}``. Never
                                                       reuse `superseded_incumbent` for this case --
                                                       that name means "refused, a human decides",
                                                       and overloading it re-creates the exact
                                                       ambiguity the dedicated field exists to
                                                       remove.
      - record names another session, NO RECORD -> DO NOT CLAIM. ``live_reason ==
                                                       "no_registry_record"`` is only ABSENCE of
                                                       evidence -- this fleet is multi-machine, and
                                                       a session on another machine, or one whose
                                                       messaging gate is off, is indistinguishable
                                                       from an exited one from here. Same refusal
                                                       shape as the LIVE case: ``{claimed: False,
                                                       holder: <them>, already_held: False,
                                                       superseded_incumbent: {..., live: False,
                                                       live_reason: "no_registry_record"},
                                                       replaced_holder: None}``.

    The asymmetry is who is harmed: superseding a LIVE peer takes the role from someone holding
    it, so that stays a human's call (`superseded_incumbent`). Replacing a record whose session is
    PROVABLY gone (`pid_not_running`) takes nothing from anyone -- refusing it forever would fire
    on essentially every invocation after the first, since a dead-but-unreaped record is this
    mode's steady state, not an anomaly. A record with NO registry trace at all stays a refusal:
    absence of evidence is not evidence of absence on a multi-machine fleet.

    `repo_root_str` is normalised (resolved to an absolute path) before use, matching the
    reference's own `_normalised_repo_root`.
    """
    repo_root = str(Path(repo_root_str).resolve())
    existing = read_record(repo_root, directory)

    if existing is None:
        record = _build_record(repo_root, session_id, peer_name, nominated_by)
        _write_json_atomic(_record_path(repo_root, directory), record)
        return {
            "claimed": True,
            "holder": session_id,
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": None,
        }

    incumbent_sid = str(existing.get("session_id") or "")
    if incumbent_sid == session_id:
        record = _build_record(repo_root, session_id, peer_name, nominated_by)
        _write_json_atomic(_record_path(repo_root, directory), record)
        return {
            "claimed": True,
            "holder": session_id,
            "already_held": True,
            "superseded_incumbent": None,
            "replaced_holder": None,
        }

    liveness = is_live(existing)

    if liveness.live_reason == "pid_not_running":
        # AUTO-REPLACE: positive evidence of death (a registry row exists for the
        # incumbent's session_id and its pid is not running). Claim it -- but loudly:
        # the replaced holder is named in its own field, never folded into
        # `superseded_incumbent`, so a caller cannot mistake this for the silent
        # clean-pass-under-a-dead-crown failure this whole guard exists to prevent.
        replaced_holder = {
            "session_id": incumbent_sid,
            "peer_name": existing.get("peer_name"),
            "nominated_at": existing.get("nominated_at"),
            "nominated_by": existing.get("nominated_by"),
            "live": liveness.live,
            "live_reason": liveness.live_reason,
        }
        record = _build_record(repo_root, session_id, peer_name, nominated_by)
        _write_json_atomic(_record_path(repo_root, directory), record)
        return {
            "claimed": True,
            "holder": session_id,
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": replaced_holder,
        }

    return {
        "claimed": False,
        "holder": incumbent_sid,
        "already_held": False,
        "superseded_incumbent": {
            "session_id": incumbent_sid,
            "peer_name": existing.get("peer_name"),
            "nominated_at": existing.get("nominated_at"),
            "nominated_by": existing.get("nominated_by"),
            "live": liveness.live,
            "live_reason": liveness.live_reason,
        },
        "replaced_holder": None,
    }
