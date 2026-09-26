
from __future__ import annotations

import asyncio

import pytest

from coordinator_core.distill.manifest_schema import make_scope_manifest
from coordinator_core.ops.distill_workflow_input import (
    CONSUMER_BATCH_FIELDS,
    CONSUMER_TOP_LEVEL_FIELDS,
    _handler,
    translate_to_workflow_input,
    validate_workflow_input,
)


def _run(coro):
    return asyncio.run(coro)


def _sample_manifest() -> dict:
    return make_scope_manifest(
        run_id="2026-08-06-15h20",
        batches=[["a.md", "b.md"], ["c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "foo", "path": "docs/wiki/foo.md"}],
        cohorts={"harvest": ["a.md", "b.md", "c.md"]},
    )


def test_translate_top_level_field_renames():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["runId"] == "2026-08-06-15h20"
    assert payload["repoRoot"] == "/repo"
    assert set(payload) == set(CONSUMER_TOP_LEVEL_FIELDS)


def test_translate_batches_shape_and_ids():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    batches = payload["batches"]
    assert [b["batchId"] for b in batches] == ["batch-1", "batch-2"]
    assert batches[0]["files"] == ["a.md", "b.md"]
    assert batches[1]["files"] == ["c.md"]
    for b in batches:
        assert set(b) == set(CONSUMER_BATCH_FIELDS)
        assert isinstance(b["description"], str) and b["description"]
        assert b["formatHints"] == {}


def test_translate_wiki_slugs_is_flat_object_map():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["wikiSlugs"] == {"foo": "docs/wiki/foo.md"}
    assert isinstance(payload["wikiSlugs"], dict)


def test_translate_batch_count_and_total_file_count():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["batch_count"] == 2
    assert payload["total_file_count"] == 3


def test_translate_format_hints_applied_uniformly():
    payload = translate_to_workflow_input(
        _sample_manifest(), repo_root="/repo", format_hints={"style": "verbose"}
    )
    assert all(b["formatHints"] == {"style": "verbose"} for b in payload["batches"])


def test_validate_well_formed_payload_clean():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert validate_workflow_input(payload) == []


def test_validate_catches_renamed_top_level_field():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["run_id"] = payload.pop("runId")
    errors = validate_workflow_input(payload)
    assert any("runId" in e for e in errors)


def test_validate_catches_missing_per_batch_field():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    del payload["batches"][0]["batchId"]
    errors = validate_workflow_input(payload)
    assert any("batchId" in e for e in errors)


def test_validate_catches_batch_count_integrity_mismatch():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["batch_count"] = 999
    errors = validate_workflow_input(payload)
    assert any("integrity mismatch" in e for e in errors)


def test_validate_catches_total_file_count_integrity_mismatch():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["total_file_count"] = 1
    errors = validate_workflow_input(payload)
    assert any("integrity mismatch" in e for e in errors)


def test_validate_catches_wiki_slugs_wrong_type():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["wikiSlugs"] = [{"slug": "foo", "path": "docs/wiki/foo.md"}]
    errors = validate_workflow_input(payload)
    assert any("wikiSlugs" in e for e in errors)


def test_handler_raises_on_missing_manifest():
    with pytest.raises(ValueError, match="manifest"):
        _handler({"repo_root": "/repo"}, repo_root=None)


def test_handler_raises_on_missing_repo_root():
    with pytest.raises(ValueError, match="repo_root"):
        _handler({"manifest": _sample_manifest()}, repo_root=None)


def test_dispatch_message_smoke():
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.workflow_input",
        "params": {"manifest": _sample_manifest(), "repo_root": "/repo"},
    }
    d = _run(ipc.dispatch_message(msg))
    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    assert d["result"]["runId"] == "2026-08-06-15h20"
    assert d["result"]["batch_count"] == 2
