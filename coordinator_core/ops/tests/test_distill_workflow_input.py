"""
coordinator_core.ops.tests.test_distill_workflow_input

Unit + contract tests for coordinator_core.ops.distill_workflow_input — the
"distill.workflow_input" op that translates a distill.scope scope-manifest
(producer shape) into the distill-harvest Workflow script's consumer shape.

Coverage:
  translate_to_workflow_input:
    (a) run_id -> runId
    (b) flat-list batches -> [{batchId, files, description, formatHints}]
    (c) wiki_slugs list-of-dicts -> wikiSlugs flat object map
    (d) repoRoot is caller-supplied, never inferred
    (e) batch_count/total_file_count are first-class and correct
    (f) format_hints applied uniformly per batch, defaults to {}
  validate_workflow_input (the contract-drift detector):
    (g) a well-formed payload validates clean
    (h) missing/renamed top-level field caught
    (i) missing/renamed per-batch field caught
    (j) batch_count/total_file_count integrity-mismatch caught
  handler:
    (k) missing manifest param raises ValueError
    (l) missing repo_root param raises ValueError
    (m) end-to-end dispatch_message smoke via the real registered wiring

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C10
"""

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


# ---------------------------------------------------------------------------
# translate_to_workflow_input
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# validate_workflow_input — the contract-drift detector
# ---------------------------------------------------------------------------


def test_validate_well_formed_payload_clean():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert validate_workflow_input(payload) == []


def test_validate_catches_renamed_top_level_field():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["run_id"] = payload.pop("runId")  # simulate a producer-side rename regression
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
    payload["wikiSlugs"] = [{"slug": "foo", "path": "docs/wiki/foo.md"}]  # regressed to list
    errors = validate_workflow_input(payload)
    assert any("wikiSlugs" in e for e in errors)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_raises_on_missing_manifest():
    with pytest.raises(ValueError, match="manifest"):
        _handler({"repo_root": "/repo"}, repo_root=None)


def test_handler_raises_on_missing_repo_root():
    with pytest.raises(ValueError, match="repo_root"):
        _handler({"manifest": _sample_manifest()}, repo_root=None)


def test_dispatch_message_smoke():
    """End-to-end command-type dispatch via the REAL registered wiring
    (ops/__init__.py eager import + _registry_map.py + op_scopes.py + the
    @register_op decorator) — proves distill.workflow_input is reachable
    exactly as a caller would invoke it, not just as a directly-imported
    Python function."""
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
