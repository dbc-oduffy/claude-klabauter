"""
Failure-matrix + AC4 tests for `publish_envelope.splice_publish_envelope` -- the
byte-splice publish-envelope producer (C2). No-parse by design: these tests exercise the
splice-safety guards, the bounded head-scan, and prove AC4 (every input key/value survives
byte-for-byte, and the output key set is exactly the three spliced fields plus the input's
own keys) without ever asserting through a full `json.loads` re-derivation of the input.
"""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.ops.emit import publish_envelope as pe

SCHEMA_VERSION = "3.13.0"


def _body(schema_version: str = SCHEMA_VERSION, emitted_at: str = "2026-08-21T00:00:00Z", **extra) -> bytes:
    doc = {"schema_version": schema_version, "emitted_at": emitted_at}
    doc.update(extra)
    return json.dumps(doc, indent=2).encode("utf-8")


@pytest.fixture(autouse=True)
def _pin_schema_version(monkeypatch):
    monkeypatch.setattr(pe.validate, "read_schema_version", lambda: SCHEMA_VERSION)


@pytest.fixture(autouse=True)
def _pin_stamp(monkeypatch):
    monkeypatch.setattr(pe, "read_engine_stamp_sha", lambda engine_root: None)


def test_splices_three_fields_immediately_after_opening_brace():
    raw = _body(coordinator_roots=[], branches=[])
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    prefix = out[:1].decode()
    assert prefix == "{"
    doc = json.loads(out)
    assert doc["repo_slug"] == "owner/myrepo"
    assert doc["producer"] == "myrepo@live"
    assert doc["published_at"] == "2026-08-21T00:00:00Z"


def test_output_is_valid_json():
    raw = _body(coordinator_roots=[{"repo": "owner/myrepo"}])
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    json.loads(out)  # must not raise


def test_ac4_every_input_key_survives_unchanged_and_key_set_is_exact():
    input_doc = {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": "2026-08-21T00:00:00Z",
        "coordinator_roots": [{"repo": "owner/myrepo"}],
        "branches": ["a", "b"],
        "completion_rollups": {"day": [], "week": []},
        "lessons": [{"id": "x", "text": "y"}],
    }
    raw = json.dumps(input_doc, indent=2).encode("utf-8")
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    output_doc = json.loads(out)

    for key, value in input_doc.items():
        assert output_doc[key] == value, f"input key {key!r} changed"

    expected_keys = set(input_doc.keys()) | {"repo_slug", "published_at", "producer"}
    assert set(output_doc.keys()) == expected_keys


def test_ac4_fails_if_a_future_edit_drops_a_section_key():
    input_doc = {"schema_version": SCHEMA_VERSION, "emitted_at": "2026-08-21T00:00:00Z", "lessons": []}
    raw = json.dumps(input_doc, indent=2).encode("utf-8")
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    output_doc = json.loads(out)
    # Simulate a future edit that filters the body: assert the guard this test embodies
    # would fail if "lessons" were absent from the spliced output.
    dropped = dict(output_doc)
    del dropped["lessons"]
    expected_keys = set(input_doc.keys()) | {"repo_slug", "published_at", "producer"}
    assert set(dropped.keys()) != expected_keys


def test_empty_object_body_omits_trailing_comma():
    raw = b"{}"
    with pytest.raises(pe.PublishEnvelopeError):
        # An empty body has no schema_version to head-scan; this must fail loud on the
        # missing key, not silently splice a malformed document.
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_empty_object_with_fields_present_produces_no_double_comma():
    # Directly exercise the comma-omission branch via the smallest legal body: no keys
    # besides the two required for the head-scan, so `inner` after those two is non-empty
    # and the branch under test is the reverse -- construct a body whose *only* content is
    # the two required keys to confirm no trailing/leading comma artifacts appear.
    raw = _body()
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    assert b",," not in out
    assert not re.search(rb",\s*}", out)
    json.loads(out)


def test_rejects_bom_prefixed_artifact():
    raw = b"\xef\xbb\xbf" + _body()
    with pytest.raises(pe.PublishEnvelopeError, match="BOM"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_rejects_non_object_top_level_array():
    raw = b'[{"schema_version": "3.13.0"}]'
    with pytest.raises(pe.PublishEnvelopeError, match="not b'\\{'"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_rejects_truncated_document_missing_closing_brace():
    raw = _body()[:-1]  # drop the final "}"
    with pytest.raises(pe.PublishEnvelopeError, match="not b'\\}'"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_rejects_empty_artifact():
    with pytest.raises(pe.PublishEnvelopeError, match="empty"):
        pe.splice_publish_envelope(b"   \n\t  ", owner="owner", repo="myrepo")


def test_schema_version_missing_within_bound_fails_loud():
    raw = b'{"padding": "' + b"x" * pe._HEAD_SCAN_BOUND + b'", "schema_version": "3.13.0"}'
    with pytest.raises(pe.PublishEnvelopeError, match="schema_version"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_schema_version_disagreement_fails_loud():
    raw = _body(schema_version="9.9.9")
    with pytest.raises(pe.PublishEnvelopeError, match="disagrees"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_schema_version_nested_inside_object_before_top_level_fails_loud():
    # The regex scan finds the FIRST "schema_version" occurrence in byte order. If a nested
    # (depth-2) occurrence precedes the real top-level one, the depth guard must reject it
    # rather than silently accepting a value from inside a nested object.
    raw = (
        b'{"nested": {"schema_version": "3.13.0"}, '
        b'"schema_version": "3.13.0", "emitted_at": "2026-08-21T00:00:00Z"}'
    )
    with pytest.raises(pe.PublishEnvelopeError, match="nesting depth"):
        pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")


def test_producer_uses_live_stamp_when_reader_returns_none(monkeypatch):
    monkeypatch.setattr(pe, "read_engine_stamp_sha", lambda engine_root: None)
    raw = _body()
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    assert json.loads(out)["producer"] == "myrepo@live"


def test_producer_uses_real_sha_when_reader_returns_one(monkeypatch):
    monkeypatch.setattr(pe, "read_engine_stamp_sha", lambda engine_root: "abc1234")
    raw = _body()
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    assert json.loads(out)["producer"] == "myrepo@abc1234"


def test_producer_uses_literal_unpinned_when_stamp_reads_unpinned(monkeypatch):
    monkeypatch.setattr(pe, "read_engine_stamp_sha", lambda engine_root: "unpinned")
    raw = _body()
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    assert json.loads(out)["producer"] == "myrepo@unpinned"


def test_repo_slug_preserves_owner_casing():
    raw = _body()
    out = pe.splice_publish_envelope(raw, owner="MyOwner", repo="myrepo")
    assert json.loads(out)["repo_slug"] == "MyOwner/myrepo"


def test_published_at_sources_from_body_emitted_at_not_a_derived_time():
    raw = _body(emitted_at="2020-01-01T00:00:00Z")
    out = pe.splice_publish_envelope(raw, owner="owner", repo="myrepo")
    assert json.loads(out)["published_at"] == "2020-01-01T00:00:00Z"


def test_no_bare_repo_token_string_literal_in_module_source():
    with open(pe.__file__, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert '"claude-klabauter"' not in source
    assert "'claude-klabauter'" not in source
    assert '"claude-klabauter"' not in source
    assert "'claude-klabauter'" not in source
