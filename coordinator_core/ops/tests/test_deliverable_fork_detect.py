"""
coordinator_core.ops.tests.test_deliverable_fork_detect

Unit tests for coordinator_core/ops/deliverable_fork_detect.py (C7).

Coverage:
  (i)   incident_triple_groups_as_one_family — the 40/42/45-shaped fixture (one
        shared slug, three truncation lengths) clusters as a single family.
  (ii)  no_family_when_no_collision — unrelated seeded ids report no families.
  (iii) report_shape_has_no_winner_field — the STRUCTURAL BOUNDARY: no winner /
        superseded_by / status / closed_at / adjudicator key anywhere in the
        report.
  (iv)  equivalence_artifact_byte_unchanged — a detector run never mutates
        state/deliverable-equivalence.yaml.
  (v)   handler_repo_root_required — the op handler's required-param contract.
  (vi)  handler_reports_family — the registered op end-to-end.

Spec backlink: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md § C7 (AC12)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.deliverable_equivalence import (
    _reset_deliverable_ledger_cache,
    _reset_equivalence_map_cache,
)
from coordinator_core.ops.deliverable_fork_detect import (
    _handler,
    detect_slug_prefix_fork_families,
)


@pytest.fixture(autouse=True)
def _reset_memo():
    _reset_equivalence_map_cache()
    _reset_deliverable_ledger_cache()
    yield
    _reset_equivalence_map_cache()
    _reset_deliverable_ledger_cache()


def _write_handoff(path: Path, deliverable_id: str) -> None:
    path.write_text(
        "\n".join(["---", f"deliverable_id: {deliverable_id}", "kind: handoff", "---", "body"])
        + "\n",
        encoding="utf-8",
    )


def test_incident_triple_groups_as_one_family(tmp_path):
    """The Problem section's own 40/42/45 triple -- three ids that are prefixes
    of one shared `coordinator-ops-buildout-from-fence-inventory` slug -- must
    cluster as ONE family."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(
        handoffs_dir / "a.md",
        "dlv-coordinator-ops-buildout-from-fence-inve-fc3678",
    )
    _write_handoff(
        handoffs_dir / "b.md",
        "dlv-coordinator-ops-buildout-from-fence-invent-903224",
    )
    _write_handoff(
        handoffs_dir / "c.md",
        "dlv-coordinator-ops-buildout-from-fence-inventory-df74c5",
    )

    families = detect_slug_prefix_fork_families(tmp_path)

    assert len(families) == 1
    assert set(families[0]["family"]) == {
        "dlv-coordinator-ops-buildout-from-fence-inve-fc3678",
        "dlv-coordinator-ops-buildout-from-fence-invent-903224",
        "dlv-coordinator-ops-buildout-from-fence-inventory-df74c5",
    }
    assert families[0]["evidence_paths"]


def test_no_family_when_no_collision(tmp_path):
    """Unrelated seeded ids with no shared slug prefix report no families."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "a.md", "dlv-completely-unrelated-one-aaaaaa")
    _write_handoff(handoffs_dir / "b.md", "dlv-totally-different-workstream-bbbbbb")

    families = detect_slug_prefix_fork_families(tmp_path)

    assert families == []


def test_report_shape_has_no_winner_field(tmp_path):
    """STRUCTURAL BOUNDARY: the report shape carries only `family` and
    `evidence_paths` -- no winner/superseded_by/status/closed_at/adjudicator
    slot anywhere, so it cannot silently grow one."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "a.md", "dlv-coordinator-ops-buildout-from-fence-inve-fc3678")
    _write_handoff(handoffs_dir / "b.md", "dlv-coordinator-ops-buildout-from-fence-invent-903224")

    families = detect_slug_prefix_fork_families(tmp_path)

    assert len(families) == 1
    entry = families[0]
    assert set(entry.keys()) == {"family", "evidence_paths"}
    for forbidden in ("winner", "superseded_by", "status", "closed_at", "adjudicator"):
        assert forbidden not in entry


def test_equivalence_artifact_byte_unchanged(tmp_path):
    """A detector run against a fixture carrying a real
    state/deliverable-equivalence.yaml never mutates it -- report-only, no
    adjudication."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_text(
        "entries:\n"
        "- loser: dlv-loser-example\n"
        "  winner: dlv-winner-example\n"
        "  evidence: test fixture\n",
        encoding="utf-8",
    )
    before = artifact_path.read_bytes()

    handoffs_dir = state_dir / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "a.md", "dlv-coordinator-ops-buildout-from-fence-inve-fc3678")
    _write_handoff(handoffs_dir / "b.md", "dlv-coordinator-ops-buildout-from-fence-invent-903224")

    detect_slug_prefix_fork_families(tmp_path)

    after = artifact_path.read_bytes()
    assert before == after


def test_handler_repo_root_required():
    """The registered op refuses with no founding root, matching
    cascade_backstop_sweep's own contract."""
    result = asyncio.run(_handler({}, repo_root=None))
    assert result["exit_code"] == 1
    assert "repo_root is required" in result["error"]


def test_handler_reports_family(tmp_path, monkeypatch):
    """End-to-end: the registered op handler reports the incident-shaped family
    with schema_version and a non-negative families_checked count."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir / "a.md", "dlv-coordinator-ops-buildout-from-fence-inve-fc3678")
    _write_handoff(handoffs_dir / "b.md", "dlv-coordinator-ops-buildout-from-fence-invent-903224")
    (tmp_path / ".git").mkdir()

    result = asyncio.run(_handler({}, repo_root=tmp_path))

    assert result["exit_code"] == 0
    assert result["schema_version"] == 1
    assert result["families_checked"] >= 2
    assert len(result["families"]) == 1
    assert set(result["families"][0]["family"]) == {
        "dlv-coordinator-ops-buildout-from-fence-inve-fc3678",
        "dlv-coordinator-ops-buildout-from-fence-invent-903224",
    }
