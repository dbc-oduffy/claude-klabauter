"""
coordinator_core.workstream_complete.chain_partition_verdict_store — single-
source persistence seam for the chain-terminal brightline verdict, so it
survives the trip from producer to consumer without an EM re-typing it.

Problem this closes (root cause, cross-repo/inbox/2026-08-04-example-retrieval-repo-em-
brightline-partition-mandatory-does-not-halt.md, "mechanism 2"): the producer
(`coordinator/bin/wsc-coverage-gate-runner.py::cmd_brightline_gate`) computes
`verdict` ("PARTITION-MANDATORY" | "single-reviewer-ok") and used to only
print an `ACTION:` line asking the EM to hand-carry
`decisions["chain_partition_verdict"]` into the next `wsc.brief()`/
`wsc.apply()` call. When nobody retyped it, `decide_review_scale`'s
chain-terminal rows 5/6 stayed unreachable and a real `PARTITION-MANDATORY`
close resolved unresolved. This module is the durable seam that makes the
retyping optional rather than load-bearing: the producer WRITES a record,
the consumer READS it as a fallback when `decisions` doesn't supply the
value explicitly.

Location choice: `state/ceremony/wsc-chain-partition-verdict/`, SIBLING to
the existing per-session ceremony receipt shard convention at
`state/ceremony/wsc/` (`coordinator_core/ops/ceremony/receipt_emit.py`) —
investigated first, not reused directly. That receipt schema is frozen and
explicitly GENERAL (`receipt_schema.py`'s own negative-spec: "do NOT add
wsc-specific sub-keys ... the generality is the invariant") and belongs to a
different producer/consumer pair (the `PipelineContext` D/J/F/B/X node
ledger, written by `ceremony.wsc_tail`). Bolting a brightline-verdict field
onto that schema would violate its own stated invariant and couple two
unrelated lifecycles. Sitting beside it under the same `state/ceremony/`
root keeps the "durable per-session ceremony artifact" convention (tracked,
not `.git/coordinator-sessions/<sid>/`, not a temp dir) without touching the
frozen schema.

Keying: ONE file per closing session id, named by a stable hash of the
session id rather than a truncated/sanitized literal slug (avoids both the
receipt shard's short-sid-prefix collision hazard and Windows path-length /
reserved-character concerns from an arbitrary session-id string embedded
directly in a filename). The record ALSO carries `session_id` verbatim
inside the body — the filename hash and the body field are independent
checks; a caller reading this record verifies both the path it resolved to
AND the body's own `session_id`/`from_handoff` fields belong to the close it
is running (see `read_verdict_record`'s docstring) before trusting a value
computed on a different run.

Windows-first: no `/tmp`, no POSIX-only path assumption, no shelling out —
pure `pathlib` + `json`, mirrors `coordinator_core.session.autonomous_
sentinel`'s single-source-resolver shape (see that module's docstring for
the incident — three call sites once reimplemented a path three ways and
two disagreed on Windows — this module exists so the verdict-store path can
never drift the same way: every writer and reader MUST call
`verdict_store_path()` below, never construct the path locally).

Fail-closed contract (HARD): `read_verdict_record` returns `None` — never a
fabricated verdict — on ANY of: file absent, unreadable, corrupt JSON,
schema-unexpected, session-id mismatch, from_handoff mismatch (when the
caller supplies one to check), or a verdict string outside the two known
literals. It is a correctness violation for this module to ever manufacture
`"single-reviewer-ok"` (or any other value) for a record it could not
positively verify belongs to the current close — an unresolved outcome is
the safe degradation, a fabricated permissive verdict is the one outcome
that must be impossible. `write_verdict_record` raises on I/O failure (mkdir/
mkstemp/replace) — it is the CALLER's job (the producer, `wsc-coverage-gate-
runner.py::cmd_brightline_gate`) to catch that and report it loudly on
stderr without changing its own exit code, per that gate's advisory-not-
halting contract.

Spec backlink: docs/plans/2026-08-03-chain-end-review-scale-wiring.md
(the Staff Engineer finding 2, "producer/consumer seam") — this module is the
persistence half that plan's C5 flagged as still hand-carried.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from coordinator_core.session import scope as session_scope

SCHEMA_VERSION: int = 1

#: Repo-relative directory this store's records live under — SIBLING to
#: `state/ceremony/wsc/` (the unrelated ceremony-receipt shard convention),
#: never inside it. See module docstring "Location choice".
VERDICT_STORE_RELDIR: str = "state/ceremony/wsc-chain-partition-verdict"

#: The two literal `verdict=` values this store will ever persist or accept
#: on read-back — the same cross-repo wire-contract strings
#: `review_brightline_gate.py` emits and `directives_review.decide_review_
#: scale` consumes verbatim. Never re-derive or rename these here.
KNOWN_VERDICTS: frozenset[str] = frozenset({"PARTITION-MANDATORY", "single-reviewer-ok"})

_HASH_LEN = 20
"""Hex-digest truncation length for the filename-safe session-id hash.
Collision-resistant at this length for the fleet's session-id volume — a
resolution failure here degrades to a wrong-session-shaped miss (fail-closed,
never a fabricated verdict), not silent data corruption."""


def _session_id_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_HASH_LEN]


def verdict_store_path(repo_root: Path, session_id: str) -> Path:
    """Return the single deterministic record path for `session_id`.

    Every writer and every reader MUST call this function rather than
    constructing the path locally — see module docstring "Windows-first".
    """
    return repo_root / VERDICT_STORE_RELDIR / f"{_session_id_hash(session_id)}.json"


def write_verdict_record(
    repo_root: Path,
    *,
    session_id: str,
    verdict: str,
    from_handoff: str,
    git_range: Optional[str],
    basis: str,
    tier: str,
) -> Path:
    """Persist the producer's already-computed brightline verdict, verbatim.

    Raises on any I/O failure (mkdir / mkstemp / os.replace / json encoding)
    — the CALLER (the gate runner's `cmd_brightline_gate`) is responsible
    for catching this and reporting it loudly on stderr WITHOUT changing its
    own exit code, per that gate's advisory-not-halting contract (module
    docstring, "Fail-closed contract").

    `verdict` is stored verbatim, never re-derived or normalized — this
    store is a pure carrier, not a second oracle.
    """
    path = verdict_store_path(repo_root, session_id)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "verdict": verdict,
        "from_handoff": from_handoff,
        "git_range": git_range,
        "basis": basis,
        "tier": tier,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, record)
    # Claim recorded only AFTER the atomic rename above completes -- a claim
    # for a write that failed (raised before os.replace) would be a false
    # declaration (module docstring, "Fail-closed contract"; plan § Key
    # mechanism facts). rel_path is repo-relative, matching every other
    # session.scope.touch() call site.
    rel_path = path.relative_to(repo_root).as_posix()
    session_scope.touch_written_path(session_id, rel_path, str(repo_root))
    return path


def read_verdict_record(
    repo_root: Path,
    *,
    session_id: str,
    expected_from_handoff: Optional[str] = None,
) -> Optional[str]:
    """Return the persisted verdict string for `session_id`, or `None`.

    `None` on ANY of: no record at this session's path, corrupt/unreadable
    JSON, missing/malformed required fields, a body-level `session_id` that
    does not match the `session_id` this call was asked for (defense-in-
    depth on top of the path already being keyed by that same id — see
    module docstring "Keying"), a `from_handoff` mismatch when
    `expected_from_handoff` is supplied (the caller's positive "this record
    was computed over the close I am running" check — see module docstring
    "Location choice" / brief()'s own provenance cross-check), or a
    `verdict` value outside `KNOWN_VERDICTS`.

    NEVER raises — every failure mode above degrades to `None`, matching
    this module's fail-closed contract: a record this function cannot
    positively verify is treated exactly as an absent one, never coerced
    into a value.
    """
    path = verdict_store_path(repo_root, session_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(record, dict):
        return None

    verdict = record.get("verdict")
    if not isinstance(verdict, str) or verdict not in KNOWN_VERDICTS:
        return None

    record_session_id = record.get("session_id")
    if not isinstance(record_session_id, str) or record_session_id != session_id:
        return None

    if expected_from_handoff is not None:
        record_from_handoff = record.get("from_handoff")
        if not isinstance(record_from_handoff, str) or record_from_handoff != expected_from_handoff:
            return None

    return verdict


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as JSON to *path* atomically via mkstemp + os.replace,
    same shape as `coordinator_core.ops.ceremony.receipt_emit._atomic_write_
    json` — mkstemp in the same directory so `os.replace` is a same-
    filesystem rename. The temp file is cleaned up on any write failure and
    the exception propagates to the caller."""
    dir_path = path.parent
    fd, tmp_str = tempfile.mkstemp(
        dir=str(dir_path),
        prefix=f".{path.stem}.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_str, str(path))
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise
