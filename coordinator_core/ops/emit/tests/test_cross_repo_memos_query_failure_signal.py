
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.sections.cross_repo_memos import (
    _query_records,
    collect,
)


def _make_ctx(repo_name: str = "test-org/test-repo") -> MagicMock:
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


_INVOKE_TARGET = "coordinator_core.ops.emit.sections.cross_repo_memos._invoke_records_query_sync"


@patch(_INVOKE_TARGET)
def test_success_empty_list_has_no_query_error(mock_invoke):
    mock_invoke.return_value = {"records": []}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is None


@patch(_INVOKE_TARGET)
def test_handler_system_exit_is_reported_as_query_error(mock_invoke):
    mock_invoke.side_effect = SystemExit(1)
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "SystemExit" in query_error


@patch(_INVOKE_TARGET)
def test_unexpected_handler_exception_is_reported_as_query_error(mock_invoke):
    mock_invoke.side_effect = RuntimeError("boom")
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "RuntimeError" in query_error
    assert "boom" in query_error


@patch(_INVOKE_TARGET)
def test_non_list_records_payload_is_reported_as_query_error(mock_invoke):
    mock_invoke.return_value = {"records": {"oops": True}}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "non-list records" in query_error


@patch(_INVOKE_TARGET)
def test_non_dict_result_is_reported_as_query_error(mock_invoke):
    mock_invoke.return_value = None
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == []
    assert query_error is not None
    assert "non-list records" in query_error


@patch(_INVOKE_TARGET)
def test_valid_records_pass_through_with_no_query_error(mock_invoke):
    payload = [{"path": "cross-repo/inbox/x.md", "frontmatter": {"title": "t"}}]
    mock_invoke.return_value = {"records": payload}
    ctx = _make_ctx()

    records, query_error = _query_records(ctx, "cross-repo-memo")

    assert records == payload
    assert query_error is None


@patch(_INVOKE_TARGET)
def test_archived_type_is_passed_through_to_records_query_params(mock_invoke):
    mock_invoke.return_value = {"records": []}
    ctx = _make_ctx()

    _query_records(ctx, "archived-memo")

    called_params = mock_invoke.call_args[0][0]
    assert called_params["type"] == "archived-memo"


_COLLECT_BUCKET_TARGET = "coordinator_core.ops.emit.sections.cross_repo_memos._collect_bucket"


def _bucket_side_effect(inbox_result, archived_result):

    def _side_effect(ctx, record_type, archived):
        if record_type == "cross-repo-memo":
            return inbox_result
        assert record_type == "archived-memo"
        return archived_result

    return _side_effect


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_query_success_empty_yields_no_malformed_marker(mock_bucket):
    mock_bucket.side_effect = _bucket_side_effect(([], []), ([], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert records == []
    assert malformed == []


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_bucket_malformed_marker_passes_through_merge(mock_bucket):
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
    mock_bucket.side_effect = _bucket_side_effect(([], []), ([], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert isinstance(records, list)
    assert isinstance(malformed, list)


@patch(_COLLECT_BUCKET_TARGET)
def test_collect_merges_inbox_and_archived_records(mock_bucket):
    inbox_record = {"title": "Some ask", "archived": False}
    archived_record = {"title": "Some closed ask", "archived": True}
    mock_bucket.side_effect = _bucket_side_effect(([inbox_record], []), ([archived_record], []))
    ctx = _make_ctx()

    records, malformed = collect(ctx)

    assert malformed == []
    assert records == [inbox_record, archived_record]


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_query_failure_yields_distinguishable_malformed_marker(mock_qr):
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
    mock_qr.return_value = ([], "records.query op exited (SystemExit code=1)")
    ctx = _make_ctx()

    from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

    with pytest.warns(UserWarning):
        records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)

    assert isinstance(records, list)
    assert isinstance(malformed, list)


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_success_with_records_stamps_archived_field(mock_qr):
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

    records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "cross-repo/archive/2026-07-21-bad.md"
