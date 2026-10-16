"""CrossRepoMemoSummary.kind is open: named kinds plus any non-empty string emit and validate."""

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
_NAMED = ("ask", "consult", "fyi", "friction", "bug", "proposal", "notice", "reply")
_OPEN = ("defect", "request", "answer", "some-future-kind")


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


def _memo(kind, **extra) -> dict:
    fm = {
        "title": "t",
        "from": "a",
        "to": "b",
        "status": "open",
        "created": "2026-10-16",
        **extra,
    }
    if kind is not None:
        fm["kind"] = kind
    return {"path": "cross-repo/inbox/m.md", "frontmatter": fm}


def _emit(tmp_path, kinds):
    with patch(_QUERY_TARGET, return_value=([_memo(k) for k in kinds], None)):
        return _collect_bucket(_ctx(tmp_path), "cross-repo-memo", archived=False)


@pytest.mark.parametrize("kind", _NAMED + _OPEN)
def test_every_kind_emits_and_validates(tmp_path, kind):
    records, malformed = _emit(tmp_path, [kind])
    assert malformed == []
    assert [r["kind"] for r in records] == [kind]
    jsonschema.validate(records[0], build_entity_schema(CrossRepoMemoSummary))


def test_absent_kind_defaults_to_ask(tmp_path):
    records, malformed = _emit(tmp_path, [None])
    assert malformed == [] and records[0]["kind"] == "ask"


@pytest.mark.parametrize("kind", ["", 7, ["bug"]])
def test_non_string_or_empty_kind_is_still_quarantined(tmp_path, kind):
    records, malformed = _emit(tmp_path, [kind])
    assert records == [] and len(malformed) == 1


def test_schema_names_the_enum_and_falls_back_to_string():
    branches = build_entity_schema(CrossRepoMemoSummary)["properties"]["kind"]["anyOf"]
    assert set(branches[0]["enum"]) == set(_NAMED)
    assert branches[1]["type"] == "string"


@pytest.mark.parametrize("kind", ["", 7, ["bug"]])
def test_generated_schema_rejects_empty_or_non_string_kind(tmp_path, kind):
    records, _ = _emit(tmp_path, ["ask"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**records[0], "kind": kind}, build_entity_schema(CrossRepoMemoSummary))


def test_contract_version_is_bumped_above_4_11_0():
    parts = tuple(int(p) for p in CONTRACT_VERSION.split("."))
    assert parts > (4, 11, 0)
