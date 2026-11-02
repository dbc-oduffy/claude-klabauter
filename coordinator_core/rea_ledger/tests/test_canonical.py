"""Tests for rea_ledger.canonical against the example-retrieval-repo unknown-revision v1 vectors (1fd7164e7c)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.rea_ledger.canonical import (
    RECORD_KINDS,
    canonical_bytes,
    ledger_root,
    revision_digest,
)

REV1 = {
    "unknown_id": "u-1",
    "revision": 1,
    "previous_revision_digest": None,
    "question": "Wie löst der Café-Parser „naïve“ Zeichen auf? 日本語",
    "coverage": "partial",
    "coverage_scope": "claim",
    "epistemic_mode": "observed",
    "source_authority": "shipped-artifact",
    "observed_at": "2026-11-01T12:00:00+00:00",
}
REV1_JSON = (
    '{"coverage":"partial","coverage_scope":"claim","epistemic_mode":"observed",'
    '"observed_at":"2026-11-01T12:00:00+00:00","previous_revision_digest":null,'
    '"question":"Wie löst der Café-Parser „naïve“ Zeichen auf? 日本語","revision":1,'
    '"source_authority":"shipped-artifact","unknown_id":"u-1"}'
)
REV1_DIGEST = "sha256:1d75df68bd497cb48e8a804c42f6452eae66000d98f7f7ca1c9b288b500047e8"


def test_record_kinds():
    assert RECORD_KINDS == ("claim", "evidence_record", "unknown_revision")


def test_vector_canonical_bytes_and_digest():
    assert canonical_bytes(REV1) == REV1_JSON.encode("utf-8")
    assert revision_digest(REV1) == REV1_DIGEST


def test_key_order_does_not_change_bytes():
    shuffled = dict(reversed(list(REV1.items())))
    assert canonical_bytes(shuffled) == canonical_bytes(REV1)


def test_no_unicode_normalisation():
    nfc = {"q": "é"}
    nfd = {"q": "é"}
    assert canonical_bytes(nfc) != canonical_bytes(nfd)
    assert revision_digest(nfc) != revision_digest(nfd)


def test_observed_at_spelling_is_not_normalised():
    z = dict(REV1, observed_at="2026-11-01T12:00:00Z")
    assert revision_digest(z) != revision_digest(REV1)


def test_no_ascii_escapes_and_no_trailing_newline():
    out = canonical_bytes({"k": "日本"})
    assert out == '{"k":"日本"}'.encode("utf-8")


def test_nan_refused():
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})


def test_digest_shape():
    d = revision_digest({"a": 1})
    assert d.startswith("sha256:") and len(d) == 7 + 64
    assert d == d.lower()


def test_ledger_root(tmp_path: Path):
    assert ledger_root(tmp_path) == tmp_path / "state" / "rea-ledger"


def test_canonical_bytes_roundtrips_through_json():
    assert json.loads(canonical_bytes(REV1)) == REV1
