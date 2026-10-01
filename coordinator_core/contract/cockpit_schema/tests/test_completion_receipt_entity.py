from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import (
    CONTRACT_VERSION,
    emit_schemas,
)
from coordinator_core.contract.cockpit_schema.entities.completion_receipt import (
    CompletionReceipt,
)

PROV = {
    "source_kind": "local_fs",
    "repo": "fixture-owner/fixture-repo",
    "ref": None,
    "path": "state/completion-receipts/2026-10/rcp-fixture-a1b2c3.md",
    "observed_at": "2026-10-01T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

VALID = {
    "schema": "completion-receipt",
    "receipt_id": "rcp-fixture-a1b2c3",
    "baton_id": "2026-10-01_100000_fixture",
    "deliverable_id": None,
    "workstream_id": None,
    "repo": "fixture-repo",
    "coordinator_root_path": ".",
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
    "concluded_at": "2026-10-01T10:05:00Z",
    "loe": {"estimated": "M", "actual": None},
    "prose_ref": "state/completion-receipts/2026-10/rcp-fixture-a1b2c3.md",
    "supersedes": None,
    "approved_by": None,
    "provenance": PROV,
}

REQUIRED = {
    "schema", "receipt_id", "baton_id", "deliverable_id", "workstream_id", "repo",
    "plan_path", "branch", "run", "verdict", "judge", "commit_range", "started_at",
    "concluded_at", "loe", "prose_ref", "supersedes", "approved_by", "provenance",
    "coordinator_root_path",
}


def test_contract_version_is_4_11_0():
    assert CONTRACT_VERSION == "4.11.0"


def test_envelope_declares_optional_completion_receipts(tmp_path):
    from coordinator_core.contract.cockpit_schema.entities.snapshot_envelope import (
        SnapshotEnvelope,
    )

    emit_schemas({"snapshot-envelope": SnapshotEnvelope}, out_dir=tmp_path)
    doc = json.loads((tmp_path / "snapshot-envelope.schema.json").read_text(encoding="utf-8"))
    prop = doc["properties"]["completion_receipts"]
    assert prop["type"] == "array"
    assert prop["items"]["properties"]["schema"]["const"] == "completion-receipt"
    assert "completion_receipts" not in doc["required"]
    assert "completion_rollups" in doc["required"]


def test_registered_before_snapshot_envelope():
    keys = list(ENTITY_SCHEMAS)
    assert keys.index("completion-receipt") == keys.index("snapshot-envelope") - 1


def test_emitted_schema_shape(tmp_path):
    emit_schemas({"completion-receipt": CompletionReceipt}, out_dir=tmp_path)
    doc = json.loads((tmp_path / "completion-receipt.schema.json").read_text(encoding="utf-8"))
    assert doc["additionalProperties"] is False
    assert REQUIRED <= set(doc["required"])
    assert "schema" in doc["properties"]
    verdict = json.dumps(doc["properties"]["verdict"])
    for v in ("agent-delivered", "human-approved", "rejected"):
        assert v in verdict
    assert "superseded" not in verdict


def test_valid_parses():
    CompletionReceipt.model_validate(VALID)


def test_extra_key_refused():
    with pytest.raises(ValidationError):
        CompletionReceipt.model_validate({**VALID, "inherited_from": "x"})


@pytest.mark.parametrize(
    "patch",
    [
        {"baton_id": None, "deliverable_id": None},
        {"judge": None},
        {"verdict": "human-approved"},
        {"verdict": "superseded"},
        {"receipt_id": "bad"},
    ],
)
def test_invalid_refused(patch):
    with pytest.raises(ValidationError):
        CompletionReceipt.model_validate({**VALID, **patch})


def test_human_approved_with_supersedes_parses():
    CompletionReceipt.model_validate(
        {**VALID, "verdict": "human-approved", "supersedes": "rcp-fixture-ffffff", "approved_by": "pm"}
    )
