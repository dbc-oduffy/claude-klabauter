"""
coordinator_core.ops.session.record_pickup — session.record_pickup op (Class B).

Purpose: port the `pickup` field write out of DoE bash's generic
`cs_session_shape_set` merge-writer into a dedicated, performant Python op,
and make the write **append-only/versioned** — a `pickup_history[]` ledger
sits alongside the existing flat `pickup` object — so a mid-session pivot
("pick up handoff A, then silently re-point to handoff B") is *detectable*
instead of silently overwriting the prior pickup, which was the root cause
of the 2026-07-14 wsc chain-terminal ship-drift incident (bash
`cs_consume_handoff` writes `pickup.handoff` in place via a top-level-key
REPLACE merge — see `coordinator-session.sh` `cs_session_shape_set`).

Scope: DR-059 defense-in-depth port (folded into
`state/improvement-queue/2026-07-14-port-session-shape-pickup-write-to-makim-bab041a7e79c.yaml`).
The observable ship-drift symptom is already fixed read-side by
`docs/plans/2026-07-14-wsc-chain-terminal-ship-drift-fix.md`; this op closes
the write-side TOCTOU root so a repoint is recorded, not merely worked around.

Lock protocol (LOAD-BEARING — see Negative-spec): this op acquires the SAME
mkdir-based lock convention bash's `cs_session_shape_set` uses
(`<sdir>/session-shape.lock/`, `coordinator-session.sh`, DoE e34f2484), NOT
`coordinator_core.locked_write.locked_rmw`'s fcntl sidecar. Two of
`cs_session_shape_set`'s three call sites (plan-claim, actioned-memos) remain
on bash and target the SAME `session-shape.json` file; fcntl and a bash
mkdir-lock do not mutually exclude one another, so using `locked_rmw` here
would silently reopen the exact write-race class this op exists to close.

Schema note: `pickup_history[]` is an ADDITIVE field not yet declared in the
DoE-owned schema (`DoE-claude/coordinator/schemas/session-shape.schema.json`,
`additionalProperties:false` on the document's top level). This op does not
edit that schema — see the dispatch report's "schema-ownership finding" for
the DoE-side follow-up. Old readers that only know `pickup` are unaffected;
strict-schema validation against the *current* (unpatched) schema would
reject the new top-level key until DoE lands the additive change.

Same posture applies to the optional `pickup.deliverable_id` /
`pickup_history[].deliverable_id` sibling field added alongside
`happened`/`handoff` (DoE handoff.schema.json's own `deliverable_id`,
threaded through by `coordinator_core.archive_stamp._record_pickup_best_effort`
at claim time) — additive, `additionalProperties:false`-incompatible with the
CURRENT unpatched `session-shape.schema.json`'s `pickup` sub-object until DoE
lands the matching schema update.

Self-registration: importing this module calls
register_op("session.record_pickup", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration.

Spec backlinks:
  - Proposal: session-shape-pickup-port-proposal.md (scout doc, this dispatch)
  - DR-059 (folded, defense-in-depth): state/improvement-queue/2026-07-14-port-session-shape-pickup-write-to-makim-bab041a7e79c.yaml
  - Ship-drift RCA / read-side fix: docs/plans/2026-07-14-wsc-chain-terminal-ship-drift-fix.md
  - Read-side consumer (MUST stay wire-compatible): coordinator_core/ops/ceremony/branch_resolution.py
    `_read_session_shape`, `pickup_field = session_shape.get("pickup")`
  - Bash write source (port origin): coordinator-session.sh (DoE e34f2484, 2026-07-22)
    `cs_session_shape_set`, `_cs_shape_lock_live` — mkdir-lock + 30s staleness
    convention replicated below.

Negative-spec:
  - Does NOT use coordinator_core.locked_write.locked_rmw — that primitive's fcntl sidecar
    lock does NOT mutually exclude against the bash mkdir-lock still used by
    cs_session_shape_set's other two call sites (plan-claim, actioned_memos) on the SAME
    session-shape.json. Using it here would silently reopen a write race, not close one.
  - Does NOT mutate the flat `pickup` object's own field names/shape — `happened`/`handoff`
    stay exactly as branch_resolution's reader expects; only a new SIBLING key
    (`pickup_history`) is added. Never rename or remove `pickup` for backward-compat.
  - Does NOT edit session-shape.schema.json — that file is DoE-owned
    (DoE-claude/coordinator/schemas/session-shape.schema.json); the additive
    `pickup_history` schema change rides the veneer memo, not this op.
  - Does NOT implement the generic `cs_session_shape_set` field-level deep-merge writer
    (actioned_memos append / plan replace / base-wins identity fields) — this op is
    SCOPED to the pickup field only, per the proposal's "self-contained field write"
    finding. Plan-claim and actioned-memos writes stay on bash until (if) ported.
  - Does NOT use raw pid/kill -0 for lock liveness — mirrors bash: liveness is inferred
    from the lock's own `claimed_at` recency (30s staleness bound), not process liveness.
"""

from __future__ import annotations

GENERATES = []  # writes session-shape.json under <git_common_dir>/coordinator-sessions/<sid>/, session state living under .git/, never a tracked repo artifact

import sys

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.fleet._common import _sessions_dir

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# mkdir-lock protocol constants — MUST mirror coordinator-session.sh exactly
# (_cs_shape_lock_live default _CS_SHAPE_LOCK_STALE_SEC=30; cs_session_shape_set
# max_attempts=20, sleep 0.1s) so a Python holder and a bash holder agree on
# staleness/retry behaviour when racing on the same lock dir.
# ---------------------------------------------------------------------------

_SHAPE_LOCK_STALE_SECONDS: float = 30.0
_LOCK_MAX_ATTEMPTS: int = 20
_LOCK_RETRY_SLEEP_SECONDS: float = 0.1


class ShapeLockTimeout(Exception):
    """Raised when the session-shape.lock mkdir lock cannot be acquired.

    Fail-closed: no read, no mutate, no write has occurred against
    session-shape.json when this is raised.
    """


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp (seconds resolution) — mirrors bash _cs_now_iso."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_epoch(raw: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp (possibly 'Z'-suffixed) to a UTC epoch float.

    Returns None on any parse failure — callers treat None as "not live"
    (mirrors bash _cs_shape_lock_live's `return 1` on unparseable claimed_at).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        print(f"skip: _parse_iso_epoch: if raw.endswith(\"Z\"): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def _lock_live(lock_dir: Path) -> bool:
    """Return True iff *lock_dir* was claimed recently enough its holder is presumed live.

    Mirrors bash `_cs_shape_lock_live`: reads the lock's own `claimed_at` file
    (written at acquisition time) rather than any process-liveness check — a
    session-shape write is a sub-second critical section, so a 30s staleness
    bound is a generous, purely time-based signal. Missing lock dir, missing
    claimed_at, or unparseable timestamp all return False (stale/reapable).
    """
    claimed_at_file = lock_dir / "claimed_at"
    if not lock_dir.is_dir() or not claimed_at_file.is_file():
        return False
    try:
        claimed_iso = claimed_at_file.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _lock_live: claimed_iso = claimed_at_file.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    claimed_epoch = _parse_iso_epoch(claimed_iso)
    if claimed_epoch is None:
        return False
    elapsed = datetime.now(tz=timezone.utc).timestamp() - claimed_epoch
    if elapsed < 0:
        elapsed = 0.0  # clock-skew clamp
    return elapsed < _SHAPE_LOCK_STALE_SECONDS


def _try_claim_lock_dir(lock_dir: Path, sid: str) -> bool:
    """Attempt a single mkdir-based lock claim; write pid/session_id/claimed_at on success.

    Returns True on successful claim, False if the dir already exists (held by
    another process/thread). Mirrors bash: `mkdir "$lock_dir"` then writes the
    three metadata files used by _lock_live's staleness check.
    """
    try:
        lock_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        print(f"skip: _try_claim_lock_dir: lock_dir.mkdir(parents=False, exist_ok=False) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    # Metadata writes are best-effort after a successful mkdir claim — a failure
    # here still leaves the lock held (the mkdir succeeded); staleness-detection
    # of a metadata-write failure is an acceptable edge (mirrors bash, which
    # does not roll back the mkdir on a subsequent `echo >` failure either).
    try:
        (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8", newline="\n")
        (lock_dir / "session_id").write_text(sid, encoding="utf-8", newline="\n")
        (lock_dir / "claimed_at").write_text(_now_iso(), encoding="utf-8", newline="\n")
    except OSError as exc:
        _LOG.warning(
            "session.record_pickup: lock metadata write failed for %s (lock still held): %s",
            lock_dir, exc,
        )
    return True


def _acquire_shape_lock(sdir: Path, sid: str) -> Path:
    """Acquire the session-shape.lock mkdir lock at <sdir>/session-shape.lock/.

    Bounded retry (20 attempts, 0.1s sleep between), with stale-lock reaping
    via _lock_live — mirrors bash cs_session_shape_set's acquire loop exactly
    so a Python holder and a bash holder converge on the same lock dir and
    the same staleness contract.

    Raises ShapeLockTimeout after _LOCK_MAX_ATTEMPTS failed attempts.
    """
    lock_dir = sdir / "session-shape.lock"
    attempts = 0
    while attempts < _LOCK_MAX_ATTEMPTS:
        if _try_claim_lock_dir(lock_dir, sid):
            return lock_dir
        # Lock held — check if holder is still in its critical section.
        if not _lock_live(lock_dir):
            # Stale/crashed holder — reap and retry immediately (mirrors bash).
            try:
                shutil.rmtree(str(lock_dir))
            except OSError:
                print(f"skip: _acquire_shape_lock: shutil.rmtree(str(lock_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            if _try_claim_lock_dir(lock_dir, sid):
                return lock_dir
        attempts += 1
        time.sleep(_LOCK_RETRY_SLEEP_SECONDS)
    raise ShapeLockTimeout(
        f"session.record_pickup: failed to acquire session-shape.lock for "
        f"session {sid!r} after {_LOCK_MAX_ATTEMPTS} attempts"
    )


def _release_shape_lock(lock_dir: Path) -> None:
    """Release the mkdir lock (rm -rf lock_dir) — best-effort, mirrors bash."""
    try:
        shutil.rmtree(str(lock_dir))
    except OSError as exc:
        _LOG.warning("session.record_pickup: failed to release lock %s: %s", lock_dir, exc)


# ---------------------------------------------------------------------------
# Read-modify-write: pickup field + pickup_history ledger
# ---------------------------------------------------------------------------

def _record_pickup_sync(
    sdir: Path, sid: str, handoff_relpath: str, deliverable_id: Optional[str] = None
) -> dict:
    """Sync: lock, read-modify-write session-shape.json's pickup fields, unlock.

    Write rule (versioned-pickup design, proposal §3):
      - `pickup` is SET (overwritten) to {"happened": true, "handoff": handoff_relpath}
        — mirrors bash's top-level-key REPLACE merge semantics exactly, so
        branch_resolution's unchanged reader keeps seeing "the current pickup".
      - `pickup_history` is APPENDED with a NEW entry carrying the same
        happened/handoff fields plus `recorded_at` — prior entries are never
        mutated. A second (pivot) call therefore produces len(pickup_history) == 2
        with differing `handoff` values — the detectable re-point signal.
      - create-if-absent: initializes {"schema_version": 1, "session_id": sid}
        when session-shape.json does not yet exist (mirrors cs_session_shape_set).
      - `deliverable_id` (additive, optional): when the caller supplies a
        non-empty value (the claimed handoff's `deliverable_id` frontmatter),
        it is set alongside `happened`/`handoff` in BOTH the flat `pickup`
        object and the new `pickup_history` entry. Omitted entirely (not
        written as `None`/`""`) when the caller passes `None` or an empty
        string — a handoff with no `deliverable_id` records nothing for this
        field, mirroring the Session-Id/Deliverable-Id trailer's
        omit-rather-than-guess discipline (coordinator-prepare-commit-msg).
        Additive sibling field — NOT yet declared in the DoE-owned
        session-shape.schema.json (same posture as `pickup_history` itself,
        see module docstring "Schema note").

    Returns the result envelope (see _handler docstring for wire shape).
    """
    sdir.mkdir(parents=True, exist_ok=True)
    shape_file = sdir / "session-shape.json"

    lock_dir = _acquire_shape_lock(sdir, sid)
    try:
        existing: dict = {}
        if shape_file.is_file():
            try:
                text = shape_file.read_text(encoding="utf-8")
                if text.strip():
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        existing = parsed
            except (OSError, json.JSONDecodeError) as exc:
                _LOG.warning(
                    "session.record_pickup: could not read existing session-shape.json "
                    "for %s (treating as absent, create-if-absent semantics): %s",
                    sid, exc,
                )
                existing = {}

        if not existing:
            existing = {"schema_version": 1, "session_id": sid}

        recorded_at = _now_iso()
        pickup_flat = {"happened": True, "handoff": handoff_relpath}
        history_entry = {"happened": True, "handoff": handoff_relpath, "recorded_at": recorded_at}
        if deliverable_id:
            pickup_flat["deliverable_id"] = deliverable_id
            history_entry["deliverable_id"] = deliverable_id

        existing["pickup"] = pickup_flat  # top-level-key REPLACE (mirrors bash merge)

        history = existing.get("pickup_history")
        if not isinstance(history, list):
            history = []
        history = history + [history_entry]  # append-only — never mutate prior entries
        existing["pickup_history"] = history

        new_text = json.dumps(existing, indent=None) + "\n"

        # Atomic write: mktemp in the session dir + os.replace (mirrors bash mktemp+mv).
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(sdir), prefix="session-shape.json.")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
            os.replace(tmp_path, str(shape_file))
            tmp_path = None  # claimed; don't unlink in finally
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    print(f"skip: _record_pickup_sync: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
                    pass

        return {
            "exit_code": 0,
            "sid": sid,
            "shape_path": str(shape_file),
            "pickup": pickup_flat,
            "pickup_history_len": len(history),
            "repoint_detected": len(history) > 1,
        }
    finally:
        _release_shape_lock(lock_dir)


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def _validate_params(
    params: dict,
) -> tuple[Optional[str], Optional[str], Optional[Path], Optional[str], Optional[dict]]:
    """Validate params; return (sid, handoff_relpath, target_root, deliverable_id, error_result).

    error_result is None on success — callers check that field to branch.
    On failure, the first four return values are None/None/None/None and
    error_result is the exit_code:1 wire envelope to return directly.
    """
    sid = params.get("sid")
    if not sid or not isinstance(sid, str) or not sid.strip():
        return None, None, None, None, _error_result("sid must be a non-empty string")

    handoff_relpath = params.get("handoff_relpath")
    if not handoff_relpath or not isinstance(handoff_relpath, str) or not handoff_relpath.strip():
        return None, None, None, None, _error_result("handoff_relpath must be a non-empty string")
    # Defense-in-depth: reject absolute paths and parent-escape — the bash caller
    # (_hp_rec in cs_consume_handoff) already guards foreign-repo path bleed, but
    # this op is the write-side security boundary and must not trust a caller-
    # supplied path blindly (mirrors _resolve_in_repo-style containment used
    # elsewhere for producer-written JSON paths).
    rel_path = Path(handoff_relpath)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None, None, None, None, _error_result(
            f"handoff_relpath must be a repo-relative path with no parent-escape: {handoff_relpath!r}"
        )

    raw_target = params.get("repo_root")
    if not raw_target or not isinstance(raw_target, str) or not raw_target.strip():
        return None, None, None, None, _error_result("repo_root must be a non-empty string")
    try:
        target_root = Path(raw_target).resolve(strict=True)
    except OSError as exc:
        print(f"skip: _validate_params: target_root = Path(raw_target).resolve(strict=True) failed: {exc}", file=sys.stderr)
        return None, None, None, None, _error_result(
            f"repo_root does not resolve to an existing path: {exc}"
        )

    # Optional, additive: a non-string or blank value normalizes to None
    # (omit-rather-than-guess — never a validation error; deliverable_id is
    # purely advisory and its absence is the common/expected case).
    raw_deliverable_id = params.get("deliverable_id")
    deliverable_id = (
        raw_deliverable_id.strip()
        if isinstance(raw_deliverable_id, str) and raw_deliverable_id.strip()
        else None
    )

    return sid, handoff_relpath, target_root, deliverable_id, None


def _error_result(reason: str) -> dict:
    """Build a setup-error result (exit_code:1) — pre-handler failure, no write attempted."""
    _LOG.error("session.record_pickup setup error: %s", reason)
    return {
        "exit_code": 1,
        "error": reason,
        "sid": None,
        "shape_path": None,
        "pickup": None,
        "pickup_history_len": 0,
        "repoint_detected": False,
    }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------

@register_op("session.record_pickup")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.record_pickup — record a `/pickup` consume event, append-only/versioned.

    Op scope "none" (fleet-generic per-repo target-resolution convention —
    mirrors session.reap_claims_for_repos / cartography.churn precedent): the
    `repo_root` handler arg is unused (always None for scope-"none" ops); the
    real target repo is the REQUIRED `repo_root` wire param, since a `/pickup`
    may consume a handoff in a repo different from claude-klabauter's own dispatching
    tree (the exact foreign-repo case the bash `_hp_rec` guard already covers
    on the read side of the payload this op receives).

    params:
      sid              (str, required): the session id whose session-shape.json
                                         is being written.
      handoff_relpath  (str, required): repo-relative path to the consumed
                                         handoff (e.g. "state/handoffs/foo.md").
                                         Must not be absolute or parent-escaping.
      repo_root        (str, required): the target repo root (any git repo,
                                         not necessarily claude-klabauter's own tree).
      deliverable_id   (str, optional): the consumed handoff's `deliverable_id`
                                         frontmatter, if it carries one. A
                                         non-string or blank value is treated
                                         as absent (never a validation error).

    Behavior: resolves <git_common_dir(repo_root)>/coordinator-sessions/<sid>/
    session-shape.json (same path convention branch_resolution reads from —
    coordinator_core.ops.fleet._common._sessions_dir), acquires the SAME
    mkdir-based lock bash's cs_session_shape_set uses (see module docstring
    "Lock protocol"), then:
      (a) SETS the flat `pickup` object (backward-compatible with
          branch_resolution._read_session_shape's `pickup.happened`/
          `pickup.handoff` reads — unchanged shape, unchanged field names).
      (b) APPENDS a `pickup_history` entry (never mutates prior entries) —
          the new, append-only/versioned ledger this op exists to introduce.
      (c) create-if-absent: initializes the schema_version/session_id skeleton
          when session-shape.json does not yet exist.
      (d) when `deliverable_id` is supplied (non-blank), SETS it alongside
          `happened`/`handoff` in both the flat `pickup` object and the new
          `pickup_history` entry — omitted entirely when absent/blank, never
          written as null or "" (see `_record_pickup_sync` docstring).

    Wire output (session.record_pickup-specific — NOT the frozen fleet envelope):
      exit_code          int   0=ok, 1=setup-error (bad params / lock timeout / write failure)
      sid                str|None
      shape_path         str|None   absolute path to the written session-shape.json
      pickup             dict|None  the flat {"happened": true, "handoff": ...} just written
      pickup_history_len int        length of pickup_history AFTER this write
      repoint_detected   bool       true iff pickup_history_len > 1 (a prior pickup existed
                                     for this sid before this call — i.e. this write is itself
                                     a pivot, or a pivot already happened earlier this session)
      error              str        present only on exit_code:1

    Negative-spec: see module docstring — does NOT implement the generic
    cs_session_shape_set deep-merge writer; scoped to pickup only.
    """
    sid, handoff_relpath, target_root, deliverable_id, error_result = _validate_params(params)
    if error_result is not None:
        return error_result

    try:
        common_dir = await asyncio.to_thread(git_common_dir, target_root)
    except RuntimeError as exc:
        return _error_result(f"git_common_dir failed for repo_root={target_root}: {exc}")

    sessions_dir = _sessions_dir(common_dir)
    sdir = sessions_dir / sid

    try:
        result = await asyncio.to_thread(
            _record_pickup_sync, sdir, sid, handoff_relpath, deliverable_id
        )
    except ShapeLockTimeout as exc:
        return _error_result(str(exc))
    except OSError as exc:
        return _error_result(f"session-shape.json write failed for sid={sid}: {exc}")

    return result
