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

Spec backlink: pln-chain-end-review-scale-wire-de-23a81a
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

# Generator-provenance declaration (generator_provenance.py). write_verdict_record
# writes one JSON record per closing session id under
# `state/ceremony/wsc-chain-partition-verdict/<sha256-hash>.json` -- a data-dependent,
# session-keyed file SET under a tracked directory, not a single fixed artifact.
MUTATES = ["state/ceremony/wsc-chain-partition-verdict/*.json"]

SCHEMA_VERSION: int = 2

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
    chain_slices: Optional[list[dict[str, Any]]] = None,
) -> Path:
    """Persist the producer's already-computed brightline verdict, verbatim.

    Raises on any I/O failure (mkdir / mkstemp / os.replace / json encoding)
    — the CALLER (the gate runner's `cmd_brightline_gate`) is responsible
    for catching this and reporting it loudly on stderr WITHOUT changing its
    own exit code, per that gate's advisory-not-halting contract (module
    docstring, "Fail-closed contract").

    `verdict` is stored verbatim, never re-derived or normalized — this
    store is a pure carrier, not a second oracle.

    `chain_slices` (C7's slate, decorated by C2): carried on the record
    exactly as given, never inspected, validated, or re-ordered — this
    store is a carrier for this payload too. `None` (the default) OMITS
    the `chain_slices` key from the record entirely, matching `commit_
    slices`'s own None-vs-`[]` precedent on the `brief()` side: absent
    means "the gate has not run for this close / this caller did not
    compute a slate", an empty list means "the gate ran and the owed set
    is resolved-and-empty". Never pass `[]` to mean "not computed yet".
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
    if chain_slices is not None:
        record["chain_slices"] = chain_slices
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


def _load_verified_record(
    repo_root: Path,
    *,
    session_id: str,
    expected_from_handoff: Optional[str],
) -> Optional[dict[str, Any]]:
    """Shared fail-closed load-and-verify for `read_verdict_record` and
    `read_chain_slices` — ONE provenance check, so the two accessors can
    never disagree about which record belongs to this close. Returns the
    raw record dict only after every check below passes; `None` otherwise.
    Never raises — see `read_verdict_record`'s docstring for the full list
    of degradation cases, reproduced here verbatim since both readers share
    them.
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

    return record


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

    Return type is pinned — this function returns ONLY the verdict string
    (or `None`) and must NOT be widened to also carry `chain_slices`; see
    `read_chain_slices` for that.
    """
    record = _load_verified_record(
        repo_root, session_id=session_id, expected_from_handoff=expected_from_handoff
    )
    if record is None:
        return None
    return record["verdict"]


def read_chain_slices(
    repo_root: Path,
    *,
    session_id: str,
    expected_from_handoff: Optional[str] = None,
) -> Optional[list[dict[str, Any]]]:
    """Return the persisted `chain_slices` slate for `session_id`, or `None`.

    Same fail-closed posture and the same session-id + `from_handoff`
    provenance cross-check as `read_verdict_record` (shared via
    `_load_verified_record` — the two accessors read the same record and
    can never disagree about whose close it belongs to).

    `None` on every case `read_verdict_record` returns `None` for, PLUS:
    the record verified fine but carries no `chain_slices` key at all (the
    gate has not run for this close, or ran before this key existed —
    `write_verdict_record`'s own None-vs-absent-key contract), or the key
    is present but not a list.

    A resolved-but-empty slate is returned as `[]`, distinct from `None` —
    "the gate ran and the owed set is empty" is an honest answer, not a
    failure (mirrors `commit_slices`'s own None-vs-`[]` precedent). The
    returned value is passed straight through, opaquely — this function
    does not inspect, validate, or re-order entries; that would make the
    carrier a second oracle over C7's shape.
    """
    record = _load_verified_record(
        repo_root, session_id=session_id, expected_from_handoff=expected_from_handoff
    )
    if record is None:
        return None
    chain_slices = record.get("chain_slices")
    if not isinstance(chain_slices, list):
        return None
    return chain_slices


#: `verdict_record_presence`'s three outcomes. NOT a verdict and never a
#: substitute for one — this is the DIAGNOSTIC axis (did the producer run?),
#: orthogonal to `read_verdict_record`'s value axis (what did it decide?).
PRESENCE_ABSENT: str = "absent"
PRESENCE_UNREADABLE: str = "unreadable"
PRESENCE_PRESENT: str = "present"


def verdict_record_presence(
    repo_root: Path, *, session_id: str, expected_from_handoff: Optional[str] = None
) -> str:
    """Report WHY `read_verdict_record` would return `None`, without ever
    supplying a verdict of its own.

    `read_verdict_record` deliberately collapses every failure mode to
    `None` (its fail-closed contract, module docstring). That collapse is
    correct for the value axis and useless for diagnosis: "the producer has
    not run yet" and "the producer ran and its verdict did not survive the
    seam" are the same `None`, and they call for OPPOSITE responses — wait
    and run the gate, versus investigate a broken persistence seam. This
    function is the discriminator, added because `build_review_scale_
    judgment_point` was ASSERTING the second cause ("a verdict that was not
    carried forward") in EM-facing prose on every unresolved chain-terminal
    close, having checked neither (example-retrieval-repo-em memo, cross-repo/inbox/
    2026-08-10-example-retrieval-repo-em-jp-review-scale-null-is-blocked-computation.md;
    PM ruling 2026-08-10 that the "as designed" defence of that null did not
    survive contact with `brief()` emitting `d-run-chain-plan-brightline-
    gate` on the very same envelope).

      - `PRESENCE_ABSENT` — no record file at this session's path. The
        producer has not run for this close. Normal and expected before
        `d-run-chain-plan-brightline-gate` executes; NOT a defect.
      - `PRESENCE_UNREADABLE` — a file exists at the path but
        `read_verdict_record` rejects it (corrupt JSON, schema-unexpected,
        session-id or from_handoff mismatch, verdict outside
        `KNOWN_VERDICTS`). The producer ran and the value did not survive.
        Break-class: report it, do not paper over it.
      - `PRESENCE_PRESENT` — a record exists and reads clean.

    Negative-spec: callers MUST still take the VALUE from
    `read_verdict_record`. A `PRESENCE_PRESENT` result is not permission to
    read the file a second way, and no caller may infer a verdict from
    presence alone — that would be the fabricated-permissive outcome this
    module exists to make impossible.

    `expected_from_handoff`, when supplied, is threaded straight into the
    `read_verdict_record` call below so this diagnostic axis rejects exactly
    what the value axis rejects (a record computed against a different
    `from_handoff` reports `PRESENCE_UNREADABLE`, not `PRESENCE_PRESENT`) —
    see `read_verdict_record`'s own docstring for that provenance check.

    Never raises; an unstattable path degrades to `PRESENCE_ABSENT`, the
    conservative reading (it keeps the EM pointed at "run the gate" rather
    than at a phantom seam bug).

    TOCTOU note: the `is_file()` check below and the subsequent
    `read_verdict_record` open are two separate filesystem operations with
    no lock between them. If the record is deleted or atomically replaced in
    that window, `is_file()` can observe True and the following read still
    fail, collapsing to `PRESENCE_UNREADABLE` rather than the more accurate
    `PRESENCE_ABSENT` — a diagnostic label only, never a verdict value, so
    this degrades safely.
    """
    path = verdict_store_path(repo_root, session_id)
    try:
        if not path.is_file():
            return PRESENCE_ABSENT
    except OSError:
        return PRESENCE_ABSENT

    if read_verdict_record(
        repo_root, session_id=session_id, expected_from_handoff=expected_from_handoff
    ) is None:
        return PRESENCE_UNREADABLE
    return PRESENCE_PRESENT


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
