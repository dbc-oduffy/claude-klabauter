"""Tests for the ``handoffs`` emit section's producer-axis pass-through (C6a).

Model + emit pass-through only — no resolver populates ``producer`` yet (a
separate chunk supplies it), so this exercises ``collect()``'s straight
frontmatter-to-wire-key passthrough via ``_jq_or(fm.get("producer"), None)``,
matching the same idiom as ``suggested_priority``/``picked_up_by``/etc.

Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section
from coordinator_core.ops.emit.tests.conftest import _write_node  # noqa: F401


def _make_ctx(repo_root: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = repo_root / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-08-12T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-08-12",
        "status": "open",
        "deployment_state": "ready_to_fire",
        "predecessor": "none",
    }
    fm.update(overrides)
    return fm


def _collect(
    mock_load_ledger,
    mock_qr,
    tmp_path: Path,
    records: list[dict],
    ledger_entries: dict,
    repo_name: str = "test-org/test-repo",
):
    ctx = _make_ctx(tmp_path, repo_name=repo_name)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return records
        return []

    mock_qr.side_effect = query_records
    mock_load_ledger.return_value = ledger_entries
    return handoffs_section.collect(ctx)


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_producer_absent_from_frontmatter_emits_null(mock_qr, mock_ll, tmp_path: Path) -> None:
    """This chunk is model + pass-through only — no resolver has populated
    the field on disk, so every emitted record carries `producer: None`."""
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "solo.md", handoff_id="hnd-solo-000000", predecessor=None)

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{"path": "state/handoffs/solo.md", "frontmatter": _base_handoff_fm(handoff_id="hnd-solo-000000")}],
        ledger_entries={},
    )

    assert malformed == []
    assert len(records) == 1
    assert records[0]["producer"] is None


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_producer_present_in_frontmatter_passes_through_verbatim(mock_qr, mock_ll, tmp_path: Path) -> None:
    """Straight passthrough, `_jq_or(fm.get("producer"), None)` idiom — the
    emit section does not interpret or reshape the value."""
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)

    producer_value = {"op_identity": "machine-minted", "typed_command": "queue_scaffold_baton"}
    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{
            "path": "state/handoffs/a.md",
            "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa", producer=producer_value),
        }],
        ledger_entries={},
    )

    assert malformed == []
    assert records[0]["producer"] == producer_value


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_producer_key_present_but_explicit_null_stays_null(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{
            "path": "state/handoffs/a.md",
            "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa", producer=None),
        }],
        ledger_entries={},
    )

    assert malformed == []
    assert records[0]["producer"] is None
