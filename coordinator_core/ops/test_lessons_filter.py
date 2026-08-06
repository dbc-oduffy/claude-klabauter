"""Tests for coordinator_core.ops.lessons_filter (ops
lessons.filter_undated_universal, lessons.reject_orphan_strip_entries).

Covers the pure filter functions, both async register_op handler contracts,
and AC7 double-invocation idempotency proofs (both ops are pure reads with
no side effects).
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from coordinator_core.ops.lessons_filter import (
    _lessons_filter_undated_universal,
    _lessons_reject_orphan_strip_entries,
    filter_undated_universal,
    reject_orphan_strip_entries,
)

# ---------------------------------------------------------------------------
# filter_undated_universal / lessons.filter_undated_universal
# ---------------------------------------------------------------------------


def _extraction_yaml(records):
    return yaml.safe_dump({"records": records}, sort_keys=False)


def test_filter_keeps_only_undated_and_universal():
    records = [
        {"id": "r1", "undated": True, "tag_universal": True},
        {"id": "r2", "undated": True, "tag_universal": False},
        {"id": "r3", "undated": False, "tag_universal": True},
        {"id": "r4", "undated": False, "tag_universal": False},
    ]
    result = filter_undated_universal(_extraction_yaml(records))

    assert result["kept_count"] == 1
    filtered = yaml.safe_load(result["filtered_yaml"])
    assert [r["id"] for r in filtered["records"]] == ["r1"]


def test_filter_preserves_other_top_level_fields():
    payload = yaml.safe_dump(
        {"meta": {"shortname": "foo"}, "records": [{"id": "r1", "undated": True, "tag_universal": True}]},
        sort_keys=False,
    )
    result = filter_undated_universal(payload)
    filtered = yaml.safe_load(result["filtered_yaml"])
    assert filtered["meta"] == {"shortname": "foo"}


def test_filter_empty_records_yields_zero_kept():
    result = filter_undated_universal(_extraction_yaml([]))
    assert result["kept_count"] == 0
    assert yaml.safe_load(result["filtered_yaml"])["records"] == []


def test_filter_handler_returns_expected_shape():
    payload = _extraction_yaml([{"id": "r1", "undated": True, "tag_universal": True}])
    result = asyncio.run(_lessons_filter_undated_universal({"extraction_yaml": payload}))
    assert result["kept_count"] == 1
    assert "filtered_yaml" in result


def test_filter_handler_requires_extraction_yaml_param():
    with pytest.raises(ValueError):
        asyncio.run(_lessons_filter_undated_universal({}))


def test_filter_double_invocation_is_idempotent_no_op():
    payload = _extraction_yaml(
        [
            {"id": "r1", "undated": True, "tag_universal": True},
            {"id": "r2", "undated": False, "tag_universal": True},
        ]
    )
    first = filter_undated_universal(payload)
    second = filter_undated_universal(payload)
    assert first == second


# ---------------------------------------------------------------------------
# reject_orphan_strip_entries / lessons.reject_orphan_strip_entries
# ---------------------------------------------------------------------------


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_reject_orphan_flags_id_with_no_routed_record(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(
        records_path,
        {"records": [{"id": "r1", "change_kind": "wiki-append"}, {"id": "r2", "change_kind": "discard"}]},
    )
    _write_yaml(strip_path, {"strip": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]})

    result = reject_orphan_strip_entries(str(records_path), str(strip_path))

    assert result["orphans"] == ["r2", "r3"]
    assert result["ok"] is False


def test_reject_orphan_ok_when_all_strip_entries_routed(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(records_path, {"records": [{"id": "r1", "change_kind": "wiki-append"}]})
    _write_yaml(strip_path, {"strip": [{"id": "r1"}]})

    result = reject_orphan_strip_entries(str(records_path), str(strip_path))

    assert result == {"orphans": [], "ok": True}


def test_reject_orphan_empty_strip_list_is_ok(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(records_path, {"records": []})
    _write_yaml(strip_path, {"strip": []})

    result = reject_orphan_strip_entries(str(records_path), str(strip_path))

    assert result == {"orphans": [], "ok": True}


def test_reject_orphan_treats_missing_change_kind_as_unrouted(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(records_path, {"records": [{"id": "r1"}]})
    _write_yaml(strip_path, {"strip": [{"id": "r1"}]})

    result = reject_orphan_strip_entries(str(records_path), str(strip_path))

    assert result["orphans"] == ["r1"]
    assert result["ok"] is False


def test_reject_orphan_handler_returns_expected_shape(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(records_path, {"records": [{"id": "r1", "change_kind": "wiki-append"}]})
    _write_yaml(strip_path, {"strip": [{"id": "r1"}]})

    result = asyncio.run(
        _lessons_reject_orphan_strip_entries(
            {"records_path": str(records_path), "strip_list_path": str(strip_path)}
        )
    )
    assert result == {"orphans": [], "ok": True}


def test_reject_orphan_handler_requires_both_params(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_lessons_reject_orphan_strip_entries({}))
    with pytest.raises(ValueError):
        asyncio.run(_lessons_reject_orphan_strip_entries({"records_path": str(tmp_path / "r.yaml")}))


def test_reject_orphan_double_invocation_is_idempotent_no_op(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(
        records_path,
        {"records": [{"id": "r1", "change_kind": "wiki-append"}, {"id": "r2", "change_kind": "discard"}]},
    )
    _write_yaml(strip_path, {"strip": [{"id": "r1"}, {"id": "r2"}]})

    first = reject_orphan_strip_entries(str(records_path), str(strip_path))
    second = reject_orphan_strip_entries(str(records_path), str(strip_path))

    assert first == second
