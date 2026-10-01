"""completion_receipts section: one receipt emits one row valid against the live
completion-receipt schema; no receipts emits []; an invalid receipt is quarantined."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from coordinator_core.completion_receipts.model import receipt_rel_path, render
from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.completion_receipts import collect

_ID = "rcp-fixture-a1b2c3"
_CONCLUDED = "2026-10-01T10:05:00Z"


def _ctx(tmp_path: Path) -> EmitContext:
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path / "state",
        git_branch="main",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at="2026-10-01T12:00:00Z",
        hostname="test-host",
        repo_name="fixture-owner/fixture-repo",
    )


def _fm(receipt_id: str = _ID) -> dict:
    return {
        "schema": "completion-receipt",
        "receipt_id": receipt_id,
        "baton_id": "2026-10-01_100000_fixture",
        "deliverable_id": None,
        "workstream_id": None,
        "repo": "authored-repo-is-not-trusted",
        "plan_path": "docs/plans/fixture.md",
        "branch": "main",
        "run": {"kind": "execute-plan", "id": "run-1"},
        "verdict": "agent-delivered",
        "judge": {
            "identity": "judge",
            "observation_summary": "observed",
            "judged_at": "2026-10-01T10:00:00Z",
        },
        "commit_range": {"base": "abc", "head": "def"},
        "started_at": None,
        "concluded_at": _CONCLUDED,
        "loe": {"estimated": "M", "actual": None},
        "prose_ref": receipt_rel_path(receipt_id, _CONCLUDED),
        "supersedes": None,
        "approved_by": None,
    }


def _write(root: Path, fm: dict) -> None:
    target = root / receipt_rel_path(fm["receipt_id"], _CONCLUDED)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render(fm, "prose").encode("utf-8"))


def test_no_receipts_emits_empty_array(tmp_path):
    assert collect(_ctx(tmp_path)) == ([], [])


def test_one_receipt_emits_one_row_valid_against_completion_receipt_schema(tmp_path):
    _write(tmp_path, _fm())
    records, malformed = collect(_ctx(tmp_path))

    assert malformed == []
    assert len(records) == 1
    row = records[0]
    assert row["repo"] == "fixture-owner/fixture-repo"
    assert row["coordinator_root_path"] == "."
    assert row["provenance"]["path"] == _fm()["prose_ref"]
    assert "_path" not in row

    emit_schemas({"completion-receipt": ENTITY_SCHEMAS["completion-receipt"]}, out_dir=tmp_path / "s")
    schema = json.loads((tmp_path / "s" / "completion-receipt.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(row, schema)


def test_invalid_receipt_is_quarantined_not_emitted(tmp_path):
    _write(tmp_path, {**_fm(), "baton_id": None})
    records, malformed = collect(_ctx(tmp_path))

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == receipt_rel_path(_ID, _CONCLUDED)
