"""Canonical serialisation and revision digest for rea-ledger records.

The unknown-revision rule is owned by example-retrieval-repo (DR-476); claude-klabauter does not
redefine it. Binding source: example-retrieval-repo ``contracts/unknown-revision/v1/
vectors.json`` at 1fd7164e7c. Where that file differs from example-retrieval-repo's
``unknown_revision.revision_digest`` snippet, vectors.json wins.

Rule: keys sorted at every level, "," and ":" separators, no trailing
newline, raw UTF-8 (``ensure_ascii=False``), NO Unicode normalisation.
Strings such as ``observed_at`` are hashed as emitted ("Z" and "+00:00"
differ); never normalise them. ``revision_digest`` is not a record field and
is never digested, so callers pass the record without it; ``previous_revision_digest``
is a field and is digested.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECORD_KINDS = ("claim", "evidence_record", "unknown_revision")


def canonical_bytes(record: dict) -> bytes:
    """Return the canonical UTF-8 JSON bytes of ``record`` (NaN/Infinity are refused)."""
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def revision_digest(record: dict) -> str:
    """Return ``"sha256:"`` plus the hex SHA-256 of ``canonical_bytes(record)``."""
    return "sha256:" + hashlib.sha256(canonical_bytes(record)).hexdigest()


def ledger_root(repo_root: Path) -> Path:
    """Return the ledger directory ``<repo_root>/state/rea-ledger``."""
    return Path(repo_root) / "state" / "rea-ledger"
