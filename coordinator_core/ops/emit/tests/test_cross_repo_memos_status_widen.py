"""CrossRepoMemoSummary.status is open: named statuses plus any non-empty string emit and validate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import jsonschema
import pytest

from coordinator_core.contract.cockpit_schema.emit_schema import (
    CONTRACT_VERSION,
    build_entity_schema,
)
from coordinator_core.contract.cockpit_schema.entities.cross_repo_memo_summary import (
    CrossRepoMemoSummary,
)
from coordinator_core.ops.emit.sections.cross_repo_memos import _collect_bucket

_QUERY_TARGET = "coordinator_core.ops.emit.sections.cross_repo_memos._query_records"
_NAMED = ("open", "in_progress", "actioned")
_OBSERVED = (
    "superseded",
    "draft",
    "closed",
    "consumed",
    "sent",
    "resolved",
    "authored-not-yet-delivered",
)


def _ctx(tmp_path) -> MagicMock:
    ctx = MagicMock()
    ctx.repo_name = "acme/widget"
    ctx.repo_root = tmp_path
    ctx.subprocess_root = None
    ctx.provenance.side_effect = lambda source_kind, path=None, derivation="parsed": {
        "source_kind": source_kind,
        "repo": "acme/widget",
        "ref": None,
        "path": path or "",
        "observed_at": "2026-10-16T00:00:00Z",
        "derivation": derivation,
        "entity_anchor": None,
    }
    return ctx


def _memo(status) -> dict:
    return {
        "path": "cross-repo/inbox/m.md",
        "frontmatter": {
            "title": "t",
            "from": "a",
            "to": "b",
            "status": status,
            "created": "2026-10-16",
        },
    }


def _emit(tmp_path, statuses):
    with patch(_QUERY_TARGET, return_value=([_memo(s) for s in statuses], None)):
        return _collect_bucket(_ctx(tmp_path), "cross-repo-memo", archived=False)


@pytest.mark.parametrize("status", _NAMED + _OBSERVED)
def test_every_status_emits_and_validates(tmp_path, status):
    records, malformed = _emit(tmp_path, [status])
    assert malformed == []
    assert [r["status"] for r in records] == [status]
    jsonschema.validate(records[0], build_entity_schema(CrossRepoMemoSummary))


@pytest.mark.parametrize("status", ["", 7, ["open"], None])
def test_non_string_or_empty_status_is_still_quarantined(tmp_path, status):
    records, malformed = _emit(tmp_path, [status])
    assert records == [] and len(malformed) == 1


def test_schema_names_the_enum_and_falls_back_to_string():
    branches = build_entity_schema(CrossRepoMemoSummary)["properties"]["status"]["anyOf"]
    assert set(branches[0]["enum"]) == set(_NAMED)
    assert branches[1]["type"] == "string"


@pytest.mark.parametrize("status", ["", 7, ["open"]])
def test_generated_schema_rejects_empty_or_non_string_status(tmp_path, status):
    records, _ = _emit(tmp_path, ["open"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**records[0], "status": status}, build_entity_schema(CrossRepoMemoSummary))


def test_contract_version_is_bumped_above_4_12_0():
    parts = tuple(int(p) for p in CONTRACT_VERSION.split("."))
    assert parts > (4, 12, 0)
