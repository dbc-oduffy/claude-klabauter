"""
coordinator_core.ops.rea_ledger_write -- JSON-RPC "rea_ledger.write".

Purpose: the one writer of the reverse-engineer-anything ledger under
``<main worktree>/state/rea-ledger/``: claims, evidence records and unknown
revisions. A batch is validated whole, then written; nothing is written when any
record is refused.

Invariants:
    - A record is accepted only when its submitted bytes equal the canonical
      re-serialisation of what they parse to (``rea_ledger.canonical``).
    - An unknown revision chains: revision 1 carries ``previous_revision_digest``
      null and no stored tip; revision N>1 carries the digest of stored revision N-1.
    - Claims are content-addressed (``claim/<sha256>.json``): claim ``id`` is unique
      only within its own claims file, so it cannot name a ledger file.
    - Evidence files are create-once; unknown-revision lines are append-only and
      prior bytes are never rewritten.
    - ``unknown_id`` / ``id`` become filenames: only a single safe path component passes.
    - The unknown-revision shape and digest rule are example-retrieval-repo's (DR-476), vendored
      under ``contract/vendored/unknown-revision/v1``; this module redefines neither.

Registered in ``ops/__init__.py``'s list, ``_registry_map.py``, ``op_scopes.py``
(common_dir), ``authz/classification.py`` (MUTATING) and ``.github/op-inventory.json``.

Negative-spec:
    - Never raises: a bad param or record is ``{"error": ...}``.
    - Writes only the three ledger path shapes named in ``ipc.py``'s rea_ledger.write entry.
    - No git spawn, no commit, no env or subprocess access.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.rea_ledger.canonical import RECORD_KINDS, canonical_bytes, ledger_root, revision_digest

_CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"
_EVIDENCE_SCHEMA = _CONTRACT_DIR / "evidence-record.schema.json"
_UNKNOWN_SCHEMA = _CONTRACT_DIR / "vendored" / "unknown-revision" / "v1" / "schema.json"
_CLAIM_SCHEMA = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "research-claim.schema.json"

#: The nine UnknownRevision fields vectors.json digests; the digest equals example-retrieval-repo's only
#: when every one is present explicitly (nulls included).
_UNKNOWN_FIELDS = (
    "unknown_id", "revision", "previous_revision_digest", "question", "coverage",
    "coverage_scope", "epistemic_mode", "source_authority", "observed_at",
)

_validators: dict = {}


def _fail(message: str) -> dict:
    return {"error": f"rea_ledger.write: {message}"}


def _safe_component(value) -> bool:
    """True when ``value`` is a single filename component that cannot leave its directory."""
    return (
        isinstance(value, str)
        and value not in ("", ".", "..")
        and not any(ch in value for ch in "/\\:\x00")
        and ".." not in value
        and value == value.rstrip(". ")
    )


def _schema_errors(kind: str, record: dict) -> list:
    """Messages for each way ``record`` violates the schema of ``kind``; empty when valid."""
    if kind == "claim":
        from coordinator_core.frontmatter.schema_validate import validate_frontmatter

        return [f"{e.get('field')}: {e.get('error')}" for e in validate_frontmatter(record, _CLAIM_SCHEMA)]
    validator = _validators.get(kind)
    if validator is None:
        import jsonschema

        path = _EVIDENCE_SCHEMA if kind == "evidence_record" else _UNKNOWN_SCHEMA
        validator = jsonschema.Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))
        _validators[kind] = validator
    return [f"{'/'.join(map(str, e.absolute_path)) or '<record>'}: {e.message}" for e in validator.iter_errors(record)]


def _read_tip(path: Path) -> Optional[dict]:
    """The last stored revision of one unknown, or None when no ledger file exists.

    Raises ValueError when the file is torn or its last line is not a record.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if not data:
        return None
    if not data.endswith(b"\n"):
        raise ValueError(f"{path.name} does not end in a newline (torn write)")
    tip = json.loads(data.rstrip(b"\n").rsplit(b"\n", 1)[-1])
    if not isinstance(tip, dict):
        raise ValueError(f"{path.name} last line is not a record")
    return tip


def _unknown_chain_problem(rec: dict, tip: Optional[dict]) -> Optional[str]:
    """Why ``rec`` cannot follow ``tip`` in its unknown's chain, or None."""
    rev = rec["revision"]
    if rev == 1:
        if tip is not None:
            return "revision 1 refused: the unknown already has a stored chain"
        if rec["previous_revision_digest"] is not None:
            return "revision 1 must carry previous_revision_digest null"
        return None
    if tip is None:
        return f"revision {rev} refused: no stored revision {rev - 1}"
    if tip.get("revision") != rev - 1:
        return f"revision {rev} refused: stored tip is revision {tip.get('revision')}"
    if rec["previous_revision_digest"] != revision_digest(tip):
        return f"revision {rev} refused: previous_revision_digest differs from the stored revision_digest"
    return None


@register_op("rea_ledger.write")
def _rea_ledger_write(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "rea_ledger.write" handler.

    Params:
        kind (str) -- one of RECORD_KINDS: "claim", "evidence_record", "unknown_revision".
        records (list[str]) -- non-empty; each the submitted JSON text of one record, which must
            equal its canonical re-serialisation byte for byte.

    Returns:
        {"written": int, "kind": str} (``written`` counts files created or lines appended;
        an identical existing claim or evidence file is a no-op) or {"error": str}, in which
        case nothing was written.
    """
    p = params if isinstance(params, dict) else {}
    kind, records = p.get("kind"), p.get("records")
    if kind not in RECORD_KINDS:
        return _fail(f"'kind' must be one of {list(RECORD_KINDS)}")
    if not isinstance(records, list) or not records or not all(isinstance(r, str) for r in records):
        return _fail("'records' must be a non-empty list of JSON strings")
    if repo_root is None:
        return _fail("repo_root is required (no founding root available)")
    root = ledger_root(main_worktree_root(repo_root))
    try:
        with held_lock(root):
            return _write_batch(kind, records, root)
    except LockTimeout as exc:
        return _fail(f"ledger lock not acquired: {exc}")


def _write_batch(kind: str, records: list, root: Path) -> dict:
    """Validate then write one batch; the caller holds the ledger lock so tip-read and append are one step."""
    creates: dict = {}
    appends: dict = {}
    tips: dict = {}
    for i, text in enumerate(records):
        try:
            rec = json.loads(text)
            if not isinstance(rec, dict):
                return _fail(f"record {i}: not a JSON object")
            canon = canonical_bytes(rec)
        except (ValueError, RecursionError) as exc:
            return _fail(f"record {i}: unparseable or non-canonicalisable: {exc}")
        if text.encode("utf-8") != canon:
            return _fail(f"record {i}: submitted bytes differ from the canonical re-serialisation")
        if kind == "unknown_revision":
            missing = [f for f in _UNKNOWN_FIELDS if f not in rec]
            if missing:
                return _fail(f"record {i}: missing explicit field(s) {missing}")
        problems = _schema_errors(kind, rec)
        if problems:
            return _fail(f"record {i}: schema: {'; '.join(problems[:3])}")

        if kind == "claim":
            target = root / "claim" / f"{hashlib.sha256(canon).hexdigest()}.json"
        elif kind == "evidence_record":
            if not _safe_component(rec["id"]):
                return _fail(f"record {i}: id is not a single safe path component")
            target = root / "evidence_record" / f"{rec['id']}.json"
        else:
            uid = rec["unknown_id"]
            if not _safe_component(uid):
                return _fail(f"record {i}: unknown_id is not a single safe path component")
            target = root / "unknown_revision" / f"{uid}.jsonl"
            if uid not in tips:
                try:
                    tips[uid] = _read_tip(target)
                except (OSError, ValueError) as exc:
                    return _fail(f"record {i}: stored chain for {uid!r} unreadable: {exc}")
            problem = _unknown_chain_problem(rec, tips[uid])
            if problem:
                return _fail(f"record {i}: {problem}")
            tips[uid] = rec
            appends.setdefault(target, []).append(canon + b"\n")
            continue

        if target in creates and creates[target] != canon:
            return _fail(f"record {i}: a different record in this batch already targets {target.name}")
        try:
            existing = target.read_bytes() if target.exists() else None
        except OSError as exc:
            return _fail(f"record {i}: {target.name} unreadable: {exc}")
        if existing is not None and existing != canon:
            return _fail(f"record {i}: {target.name} already exists with different bytes")
        if existing is None:
            creates[target] = canon

    written = 0
    try:
        for target, data in creates.items():
            atomic_write_bytes(target, data)
            written += 1
        for target, lines in appends.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "ab") as fh:
                fh.write(b"".join(lines))
                fh.flush()
                os.fsync(fh.fileno())
            written += len(lines)
    except OSError as exc:
        return _fail(f"write failed after {written} record(s): {exc}")
    return {"written": written, "kind": kind}
