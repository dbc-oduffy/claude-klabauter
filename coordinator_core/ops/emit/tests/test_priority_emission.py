"""Tests for the ``handoffs`` emit section's priority-ledger wiring (C6a).

Exercises the ``collect()``-level integration: ``pm_priority`` /
``pm_priority_origin`` / ``pm_priority_source_id`` / ``suggested_priority``
are populated on every emitted ``HandoffSummary`` record by importing and
calling ``priority_resolve.resolve_priority`` — the SOLE resolution
implementation (``priority_resolve.py`` module docstring) — never a second,
section-local walk. Also covers the dangling-ledger-target diagnostic and the
``CONTRACT_VERSION`` bump.

Real on-disk ``.md`` fixture files back every case that needs ancestor-chain
resolution: ``dag.walk_forward`` (which ``resolve_priority`` calls) reads
real files, so a record's mocked ``_query_records`` frontmatter and its
on-disk file content must agree — same discipline as
``test_priority_resolve.py``'s ``_write_node`` fixtures.

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C6a.
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
        observed_at="2026-07-27T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-07-27",
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


# CONTRACT_VERSION — PART 3.


def test_contract_version_carries_the_priority_ledger_bump() -> None:
    from coordinator_core.contract.cockpit_schema.emit_schema import CONTRACT_VERSION

    version = tuple(int(part) for part in CONTRACT_VERSION.split("."))
    assert version >= (3, 4, 0), (
        f"priority-ledger fields require contract >= 3.4.0, got {CONTRACT_VERSION}"
    )


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_fields_present_and_null_with_no_ledger_no_ancestry(mock_qr, mock_ll, tmp_path: Path) -> None:
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
    r = records[0]
    assert r["pm_priority"] is None
    assert r["pm_priority_origin"] == "none"
    assert r["pm_priority_source_id"] is None
    assert r["suggested_priority"] is None


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_explicit_entry_resolves_and_source_id_is_null(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa")}],
        ledger_entries={"hnd-a-aaaaaa": {"priority": "urgent", "target_kind": "handoff"}},
    )

    assert malformed == []
    r = records[0]
    assert r["pm_priority"] == "urgent"
    assert r["pm_priority_origin"] == "explicit"
    assert r["pm_priority_source_id"] is None


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_inherited_from_ancestor_source_id_is_ancestor_handoff_id(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)
    _write_node(handoff_dir, "b.md", handoff_id="hnd-b-bbbbbb", predecessor="a.md")

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [
            {"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa")},
            {
                "path": "state/handoffs/b.md",
                "frontmatter": _base_handoff_fm(handoff_id="hnd-b-bbbbbb", predecessor="a.md"),
            },
        ],
        ledger_entries={"hnd-a-aaaaaa": {"priority": "low", "target_kind": "handoff"}},
    )

    assert malformed == []
    by_id = {r["handoff_id"]: r for r in records}
    b = by_id["hnd-b-bbbbbb"]
    assert b["pm_priority"] == "low"
    assert b["pm_priority_origin"] == "inherited"
    assert b["pm_priority_source_id"] == "hnd-a-aaaaaa"


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_suggested_priority_passthrough_and_resolver_fallback(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(
        handoff_dir, "solo.md", handoff_id="hnd-solo-000000", predecessor=None,
        suggested_priority="medium",
    )

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{
            "path": "state/handoffs/solo.md",
            "frontmatter": _base_handoff_fm(handoff_id="hnd-solo-000000", suggested_priority="medium"),
        }],
        ledger_entries={},
    )

    assert malformed == []
    r = records[0]
    assert r["suggested_priority"] == "medium"
    assert r["pm_priority"] == "medium"
    assert r["pm_priority_origin"] == "suggested"
    assert r["pm_priority_source_id"] is None


# handoff is REPORTED via the malformed bucket, never silently carried and


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_dangling_handoff_target_reported_in_malformed(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa")}],
        ledger_entries={
            "hnd-a-aaaaaa": {"priority": "high", "target_kind": "handoff"},
            "hnd-ghost-999999": {"priority": "urgent", "target_kind": "handoff"},
        },
    )

    assert len(records) == 1
    dangling_reasons = [m["reason"] for m in malformed if "hnd-ghost-999999" in m.get("reason", "")]
    assert len(dangling_reasons) == 1
    assert "dangling" in dangling_reasons[0]


@patch("coordinator_core.ops.emit.sections.handoffs.load_priority_ledger")
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_dangling_check_ignores_non_handoff_target_kinds(mock_qr, mock_ll, tmp_path: Path) -> None:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    _write_node(handoff_dir, "a.md", handoff_id="hnd-a-aaaaaa", predecessor=None)

    records, malformed = _collect(
        mock_ll, mock_qr, tmp_path,
        [{"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id="hnd-a-aaaaaa")}],
        ledger_entries={
            "hnd-a-aaaaaa": {"priority": "high", "target_kind": "handoff"},
            "pln-some-plan-000000": {"priority": "urgent", "target_kind": "plan"},
        },
    )

    assert len(records) == 1
    assert malformed == []
