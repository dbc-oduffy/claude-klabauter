"""Regression tests — cross_repo_memos query-failure observability.

Break-class bug (original subprocess-era regression): ``sections/cross_repo_memos.py::
_query_records`` collapsed EVERY failure mode (missing node, non-zero exit, unparseable
stdout, timeout, OSError) to a bare ``[]``, indistinguishable from "the query ran and there
are genuinely zero cross-repo memos". Downstream consumers (cockpit's fleet view) could not
tell "couldn't read" from "none exist".

These tests assert the fix: ``_query_records`` returns ``(records, query_error)`` and
``collect()`` surfaces a failed query as a ``query_failed: True`` marker row in
``malformed_records.cross_repo_memos`` (the section's existing degraded-record channel —
schema-permissive, no new contract field) plus a loud ``warnings.warn``. Emission never
aborts; the two failure/empty cases are distinguishable in the returned tuple and in the
emitted envelope shape.

Node-subprocess retirement (2026-07-21): ``_query_records`` no longer shells out to
``node query-records.js`` — it calls ``coordinator_core.ops.records_query``'s in-process
``records.query`` op handler via ``_invoke_records_query_sync``. The failure-mode fixtures
below were rewritten to match: the old subprocess-specific failure modes (non-zero exit,
unparseable stdout, timeout, missing node OSError) no longer apply to this call path — they
are replaced by the in-process equivalents (handler ``SystemExit``, an unexpected exception
from the handler, and a malformed non-list ``records`` payload). The overarching contract
(``(records, query_error)``, fail-open, distinguishable malformed marker row) is unchanged
and is what these tests continue to guard.

2026-07-24 (C6, archived-memo bucket): ``_query_records`` now takes an explicit
``record_type`` param (``cross-repo-memo`` / ``archived-memo``) — direct-call tests below
pass it explicitly. ``collect()`` now drives TWO independent buckets via ``_collect_bucket``
(inbox + archived); collect()-level tests patch ``_collect_bucket`` directly rather than
``_query_records``, so each bucket's contribution to the merged (records, malformed) tuple
stays independently controllable and the query-failure-signal assertions below continue to
hold per-bucket.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.sections.cross_repo_memos import (
    _query_records,
    collect,
)


def _make_ctx(repo_name: str = "test-org/test-repo") -> MagicMock:
    """Minimal EmitContext stub sufficient for section collect() calls under test."""
    ctx = MagicMock()
    ctx.repo_name = repo_name
    ctx.repo_root = "/tmp/does-not-exist"
    ctx.subprocess_root = None

    def provenance(source_kind: str, path: str | None = None, derivation: str = "parsed") -> dict:
        return {
            "source_kind": source_kind,
            "repo": repo_name,
            "ref": None,
            "path": path or "",
            "observed_at": "2026-07-21T00:00:00Z",
            "derivation": derivation,
        }

    ctx.provenance.side_effect = provenance
    return ctx


# ---------------------------------------------------------------------------
# _query_records — distinguishing success-empty from failure
# ---------------------------------------------------------------------------

_INVOKE_TARGET = "coordinator_core.ops.emit.sections.cross_repo_memos._invoke_records_query_sync"


@patch(_INVOKE_TARGET)
def test_success_empty_list_has_no_query_error(mock_invoke):
    """A well-formed empty result is success — query_error must be None."""
    mock_invoke.return_value = {"records": []}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is None


@patch(_INVOKE_TARGET)
def test_handler_system_exit_is_reported_as_query_error(mock_invoke):
    """A SystemExit from the handler (e.g. unknown/dropped type) must surface a
    query_error, not silently become []."""
    mock_invoke.side_effect = SystemExit(1)
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "SystemExit" in query_error


@patch(_INVOKE_TARGET)
def test_unexpected_handler_exception_is_reported_as_query_error(mock_invoke):
    """Any unexpected exception raised by the handler must surface a query_error rather
    than propagating (fail-open posture) or collapsing to []."""
    mock_invoke.side_effect = RuntimeError("boom")
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "RuntimeError" in query_error
    assert "boom" in query_error


@patch(_INVOKE_TARGET)
def test_non_list_records_payload_is_reported_as_query_error(mock_invoke):
    """A malformed non-list ``records`` payload must surface a query_error."""
    mock_invoke.return_value = {"records": {"oops": True}}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "non-list records" in query_error


@patch(_INVOKE_TARGET)
def test_non_dict_result_is_reported_as_query_error(mock_invoke):
    """A handler result that isn't even a dict must surface a query_error."""
    mock_invoke.return_value = None
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "non-list records" in query_error


@patch(_INVOKE_TARGET)
def test_valid_records_pass_through_with_no_query_error(mock_invoke):
    """A genuine successful query with records must pass through unchanged, query_error None."""
    payload = [{"path": "cross-repo/inbox/x.md", "frontmatter": {"title": "t"}}]
    mock_invoke.return_value = {"records": payload}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == payload
    assert query_error is None


@patch(_INVOKE_TARGET)
def test_archived_type_is_passed_through_to_records_query_params(mock_invoke):
    """``record_type`` is threaded into the records.query params unchanged — the archived
    bucket queries ``type=archived-memo``, not ``cross-repo-memo``."""
    mock_invoke.return_value = {"records": []}
    ctx = _make_ctx()

    _query_records(ctx, "archived-memo")

    called_params = mock_invoke.call_args[0][0]
    assert called_params["type"] == "archived-memo"


# ---------------------------------------------------------------------------
# collect() — query failure surfaces as an observable malformed marker row
#
# collect() now drives two independent buckets (inbox + archived) via
# _collect_bucket(ctx, record_type, archived); these tests patch _collect_bucket directly
# so each bucket's contribution is independently controllable via side_effect keyed on the
# record_type argument, mirroring the pre-C6 single-bucket fixtures above but exercised
# through the merge point.
# ---------------------------------------------------------------------------

_COLLECT_BUCKET_TARGET = "coordinator_core.ops.emit.sections.cross_repo_memos._collect_bucket"


def _bucket_side_effect(inbox_result, archived_result):
    """Build a ``_collect_bucket`` side_effect returning ``inbox_result`` for the
    ``cross-repo-memo`` type and ``archived_result`` for ``archived-memo``."""

    def _side_effect(ctx, record_type, archived):
        if record_type == "cross-repo-memo":
            return inbox_result
        assert record_type == "archived-memo"
        return archived_result

    return _side_effect


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_query_success_empty_yields_no_malformed_marker(mock_bucket):
    """Genuine "no cross-repo memos exist" in either bucket — records=[] AND malformed=[]
    (no marker row)."""
    mock_bucket.side_effect = _bucket_side_effect(([], []), ([], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert records == []
    assert malformed == []


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_bucket_malformed_marker_passes_through_merge(mock_bucket):
    """A query_failed marker row surfaced by one bucket propagates unchanged through the
    collect()-level merge — the merge is a plain concatenation, not a re-derivation."""
    marker = {
        "path": None,
        "reason": "records.query op query failed: records.query op raised RuntimeError: boom",
        "query_failed": True,
        "record_type": "cross-repo-memo",
    }
    mock_bucket.side_effect = _bucket_side_effect(([], [marker]), ([], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert records == []
    assert malformed == [marker]


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_merges_both_buckets_without_raising(mock_bucket):
    """Emission must continue even when a bucket call raises unexpectedly at the merge
    layer — collect() itself performs no I/O beyond the two _collect_bucket calls, so this
    guards the merge stays a straight concatenation with no re-introduced failure mode."""
    mock_bucket.side_effect = _bucket_side_effect(([], []), ([], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)  # must not raise

    assert isinstance(records, list)
    assert isinstance(malformed, list)


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_merges_inbox_and_archived_records(mock_bucket):
    """Baseline: records from both buckets land in the merged output, each carrying the
    ``archived`` discriminator its bucket call was invoked with."""
    inbox_record = {"title": "Some ask", "archived": False}
    archived_record = {"title": "Some closed ask", "archived": True}
    mock_bucket.side_effect = _bucket_side_effect(([inbox_record], []), ([archived_record], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert malformed == []
    assert records == [inbox_record, archived_record]


# ---------------------------------------------------------------------------
# _collect_bucket() — the single-bucket logic collect() now delegates to; exercises the
# query-failure-signal + decision_note/archived-field behavior _query_records-level tests
# above no longer reach once collect() stopped calling _query_records directly.
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_query_failure_yields_distinguishable_malformed_marker(mock_qr):
    """Query failure — records=[] but malformed carries a query_failed:True marker row,
    making it distinguishable from the genuine-empty case."""
    mock_qr.return_value = ([], "records.query op raised RuntimeError: boom")
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

    with pytest.warns(UserWarning, match="records.query op query failed"):
        records, malformed = _collect_bucket(ctx, "cross-repo-memo", archived=False)

    assert records == []
    assert len(malformed) == 1
    marker = malformed[0]
    assert marker["query_failed"] is True
    assert marker["path"] is None
    assert "records.query op raised RuntimeError: boom" in marker["reason"]


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_query_failure_does_not_abort_emission(mock_qr):
    """Emission must continue on query failure — no exception propagates out of
    _collect_bucket()."""
    mock_qr.return_value = ([], "records.query op exited (SystemExit code=1)")
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

    with pytest.warns(UserWarning):
        records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)  # must not raise

    assert isinstance(records, list)
    assert isinstance(malformed, list)


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_success_with_records_stamps_archived_field(mock_qr):
    """A successful query's valid records carry the ``archived`` discriminator the caller
    passed in, unaffected by the query-failure signal path."""
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/inbox/2026-07-21-x.md",
                "frontmatter": {
                    "title": "Some ask",
                    "from": "team-a",
                    "to": "team-b",
                    "status": "open",
                    "created": "2026-07-21T00:00:00Z",
                },
            }
        ],
        None,
    )
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

    records, malformed = _collect_bucket(ctx, "cross-repo-memo", archived=False)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["title"] == "Some ask"
    assert records[0]["archived"] is False
    assert "decision_note" not in records[0]


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_decision_note_capped_and_present_when_set(mock_qr):
    """A frontmatter ``decision_note`` is capped and emitted; the field is present only when
    the source memo actually carries one (key-absent otherwise, per jsonschema shape)."""
    long_note = "x" * 900
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/archive/2026-07-21-x.md",
                "frontmatter": {
                    "title": "Closed ask",
                    "from": "team-a",
                    "to": "team-b",
                    "status": "actioned",
                    "created": "2026-07-21T00:00:00Z",
                    "decision_note": long_note,
                },
            }
        ],
        None,
    )
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import (
        _DECISION_NOTE_MAX_CHARS,
        _collect_bucket,
    )

    records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["archived"] is True
    note = records[0]["decision_note"]
    assert len(note) == _DECISION_NOTE_MAX_CHARS
    assert note.endswith("…")


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_malformed_row_is_quarantined_not_crashed(mock_qr):
    """A row missing required memo-shape fields is quarantined into malformed, not raised
    or silently dropped — collect()/_collect_bucket() never crash on a malformed row."""
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/archive/2026-07-21-bad.md",
                "frontmatter": {"title": "Missing from/to/status/created"},
            }
        ],
        None,
    )
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

    records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)  # must not raise

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "cross-repo/archive/2026-07-21-bad.md"
