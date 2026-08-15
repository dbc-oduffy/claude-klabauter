"""
coordinator_core.ops.tests.test_distill_disposal_manifest

Unit tests for coordinator_core.ops.distill_disposal_manifest — the
"distill.assemble_disposal_manifest" op (C12).

Coverage:
  evaluate_candidate_receipts:
    (a) fully-eligible handoff candidate -> eligible True, guards_run non-empty,
        every receipt has guard/verdict/evidence, no "block" verdicts
    (b) blocked candidate (missing shipped_in, no realized_by, absent
        commitments surface) -> eligible False, blocked_by lists the failing
        guards, guards_run STILL carries every applicable guard's receipt
        (AC3 — receipts present on both eligible and retained rows)
    (c) unclassifiable candidate (neither memo nor handoff shape/path) ->
        eligible False, "artifact-class-unresolved" in blocked_by
  compute_disposal_manifest (fixture-repo golden):
    (d) eligible row carries a non-empty log_row (EPHEMERAL disposition,
        render_row-validated); retained row carries retention_reason and an
        empty log_row
    (e) receipt-completeness structural assert (AC3): no row has
        eligible=True with an empty guards_run, no row has eligible=False
        without a retention_reason
    (f) scan_stats totals match input candidate count
    (g) mass-throttle flag set when eligible/total ratio exceeds
        MASS_THROTTLE_RATIO; unset at/below threshold (flag set/unset boundary)
    (h) mass-throttle flag NOT set when total_scanned == 0 (empty candidate
        list is a clean no-op — § 1a fix, 2026-07-23 architecture review);
        mass-throttle flag set when eligible_count exceeds
        MASS_THROTTLE_ABSOLUTE regardless of ratio (absolute-floor trip)
    (i) a candidate path absent on disk at assemble time -> retained with the
        named reason, no guard evaluated
    (j) deletion_groups additive key present only when eligible-row count
        exceeds GROUP_THRESHOLD, absent otherwise
  write_disposal_manifest:
    (k) writes state/scratch/artifact-distillation/<run-id>/disposal-manifest.json,
        valid JSON, schema_version first key
    (l) write-confined: a second run-id's write never touches the first's file
  handler:
    (m) repo_root=None raises ValueError
    (n) missing run_id raises ValueError
    (o) missing/non-list candidates raises ValueError
    (p) end-to-end dispatch_message smoke via the real registered wiring

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C12
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.distill_disposal_manifest import (
    GROUP_THRESHOLD,
    MASS_THROTTLE_ABSOLUTE,
    MASS_THROTTLE_RATIO,
    _handler,
    compute_disposal_manifest,
    evaluate_candidate_receipts,
    write_disposal_manifest,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")


def _run(coro):
    return asyncio.run(coro)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _full_handoff(tmp_path: Path, name: str = "candidate.md") -> Path:
    """A handoff-shaped file that clears every handoff-applicable guard (empty
    commitments ledger included, so commitment-closure has a real pass)."""
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "archive" / "handoffs" / name
    _write(
        handoff,
        "---\n"
        "shipped_in: 68b27420\n"
        "status: consumed\n"
        "deployment_state: shipped\n"
        "realized_by: inline\n"
        "distill_fate: ephemeral\n"
        "---\n"
        "body\n",
    )
    return handoff


def _blocked_handoff(tmp_path: Path, name: str = "bad-candidate.md") -> Path:
    """A handoff-shaped file that fails shipped_in + realized_by + the
    absent-surface commitment-closure branch (no cross-repo-commitments dir)."""
    handoff = tmp_path / "archive" / "handoffs" / name
    _write(
        handoff,
        "---\n"
        "status: consumed\n"
        "deployment_state: shipped\n"
        "---\n"
        "body\n",
    )
    return handoff


def _unclassifiable(tmp_path: Path, name: str = "spec.md") -> Path:
    spec = tmp_path / "archive" / "specs" / name
    _write(spec, "---\nstatus: implemented\n---\nbody\n")
    return spec


# ---------------------------------------------------------------------------
# evaluate_candidate_receipts
# ---------------------------------------------------------------------------


@_requires_rg
def test_evaluate_candidate_receipts_fully_eligible(tmp_path: Path):
    handoff = _full_handoff(tmp_path)
    receipt = evaluate_candidate_receipts(handoff, tmp_path)
    assert receipt["eligible"] is True
    assert receipt["blocked_by"] == []
    assert receipt["artifact_class"] == "handoff"
    assert len(receipt["guards_run"]) > 0
    for r in receipt["guards_run"]:
        assert set(r.keys()) == {"guard", "verdict", "evidence"}
        assert r["verdict"] == "pass"


@_requires_rg
def test_evaluate_candidate_receipts_blocked_but_receipts_present(tmp_path: Path):
    handoff = _blocked_handoff(tmp_path)
    receipt = evaluate_candidate_receipts(handoff, tmp_path)
    assert receipt["eligible"] is False
    assert "shipped_in" in receipt["blocked_by"]
    assert "realized_by" in receipt["blocked_by"]
    assert "commitment-closure" in receipt["blocked_by"]
    # AC3: guards_run is non-empty even on a fully-blocked candidate.
    assert len(receipt["guards_run"]) >= 3
    verdicts = {r["guard"]: r["verdict"] for r in receipt["guards_run"]}
    assert verdicts["shipped_in"] == "block"


@_requires_rg
def test_evaluate_candidate_receipts_unclassifiable(tmp_path: Path):
    spec = _unclassifiable(tmp_path)
    receipt = evaluate_candidate_receipts(spec, tmp_path)
    assert receipt["eligible"] is False
    assert "artifact-class-unresolved" in receipt["blocked_by"]
    assert receipt["artifact_class"] == "unresolved"


# ---------------------------------------------------------------------------
# compute_disposal_manifest — fixture-repo golden
# ---------------------------------------------------------------------------


@_requires_rg
def test_compute_disposal_manifest_eligible_and_retained_rows(tmp_path: Path):
    _full_handoff(tmp_path, "eligible.md")
    _blocked_handoff(tmp_path, "blocked.md")
    result = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=["archive/handoffs/eligible.md", "archive/handoffs/blocked.md"],
    )
    rows_by_path = {r["path"]: r for r in result.manifest["rows"]}
    eligible_row = rows_by_path["archive/handoffs/eligible.md"]
    blocked_row = rows_by_path["archive/handoffs/blocked.md"]

    assert eligible_row["eligible"] is True
    assert eligible_row["log_row"].startswith("- archive/handoffs/eligible.md -> EPHEMERAL")
    assert "retention_reason" not in eligible_row

    assert blocked_row["eligible"] is False
    assert blocked_row["log_row"] == ""
    assert blocked_row["retention_reason"]


@_requires_rg
def test_compute_disposal_manifest_receipt_completeness(tmp_path: Path):
    _full_handoff(tmp_path, "eligible.md")
    _blocked_handoff(tmp_path, "blocked.md")
    result = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=["archive/handoffs/eligible.md", "archive/handoffs/blocked.md"],
    )
    for row in result.manifest["rows"]:
        assert len(row["guards_run"]) > 0, f"row {row['path']} has empty guards_run"
        if row["eligible"] is False:
            assert row.get("retention_reason"), f"retained row {row['path']} missing retention_reason"


@_requires_rg
def test_compute_disposal_manifest_scan_stats_totals(tmp_path: Path):
    _full_handoff(tmp_path, "a.md")
    _full_handoff(tmp_path, "b.md")
    _blocked_handoff(tmp_path, "c.md")
    result = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=[
            "archive/handoffs/a.md",
            "archive/handoffs/b.md",
            "archive/handoffs/c.md",
        ],
    )
    stats = result.manifest["scan_stats"]
    assert stats["total_scanned"] == 3
    assert stats["eligible_count"] == 2
    assert stats["retained_count"] == 1
    assert stats["eligible_count"] + stats["retained_count"] == stats["total_scanned"]


@_requires_rg
def test_compute_disposal_manifest_mass_throttle_boundary(tmp_path: Path):
    # Exactly at the ratio (1/2 == 0.5, NOT > 0.5) -> flag unset.
    _full_handoff(tmp_path, "eligible.md")
    _blocked_handoff(tmp_path, "blocked.md")
    at_threshold = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=["archive/handoffs/eligible.md", "archive/handoffs/blocked.md"],
        mass_throttle_ratio=0.5,
    )
    assert at_threshold.manifest["mass_throttle"] is False

    # A second eligible candidate tips the ratio to 2/3 > 0.5 -> flag set.
    _full_handoff(tmp_path, "eligible2.md")
    over_threshold = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-02h00",
        candidates=[
            "archive/handoffs/eligible.md",
            "archive/handoffs/eligible2.md",
            "archive/handoffs/blocked.md",
        ],
        mass_throttle_ratio=0.5,
    )
    assert over_threshold.manifest["mass_throttle"] is True


def test_compute_disposal_manifest_mass_throttle_zero_scanned_does_not_trip(tmp_path: Path):
    # § 1a fix: an empty manifest (zero blast radius, the single safest
    # possible input) must be a clean no-op, NOT a flag that trains an
    # operator to type the ack reflexively on the harmless run.
    result = compute_disposal_manifest(tmp_path, run_id="2026-07-23-01h00", candidates=[])
    assert result.manifest["mass_throttle"] is False
    assert result.manifest["scan_stats"]["total_scanned"] == 0


@_requires_rg
def test_compute_disposal_manifest_mass_throttle_absolute_floor(tmp_path: Path):
    # § 1a fix: the plan's own motivating F2 scenario ("400 eligible out of
    # 1000 scanned") is a 0.4 ratio and would never trip a ratio-only test at
    # a corpus size where the ratio stays under 0.5. Use a low ratio
    # threshold here so the absolute floor, not the ratio, is what's under
    # test: MASS_THROTTLE_ABSOLUTE eligible candidates plus one retained
    # candidate keeps the ratio comfortably below any reasonable
    # mass_throttle_ratio, isolating the absolute-floor trip.
    candidates = []
    for i in range(MASS_THROTTLE_ABSOLUTE):
        name = f"eligible-{i:03d}.md"
        _full_handoff(tmp_path, name)
        candidates.append(f"archive/handoffs/{name}")
    _blocked_handoff(tmp_path, "blocked.md")
    candidates.append("archive/handoffs/blocked.md")

    at_floor = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=candidates,
        mass_throttle_ratio=0.99,
    )
    # eligible_count == MASS_THROTTLE_ABSOLUTE exactly -> NOT > floor -> unset.
    assert at_floor.manifest["scan_stats"]["eligible_count"] == MASS_THROTTLE_ABSOLUTE
    assert at_floor.manifest["mass_throttle"] is False

    one_more = tmp_path.parent / "over-floor-repo"
    one_more.mkdir()
    over_floor_candidates = []
    for i in range(MASS_THROTTLE_ABSOLUTE + 1):
        name = f"eligible-{i:03d}.md"
        _full_handoff(one_more, name)
        over_floor_candidates.append(f"archive/handoffs/{name}")
    over_floor = compute_disposal_manifest(
        one_more,
        run_id="2026-07-23-02h00",
        candidates=over_floor_candidates,
        # ratio can never exceed 1.0, so a >1.0 threshold makes the ratio
        # test a structural no-op here — isolating the absolute floor as the
        # ONLY thing that can trip the flag in this fixture.
        mass_throttle_ratio=2.0,
    )
    # eligible_count == MASS_THROTTLE_ABSOLUTE + 1 -> > floor -> set, on the
    # absolute floor alone.
    assert over_floor.manifest["mass_throttle"] is True


def test_compute_disposal_manifest_absent_candidate_path_retained(tmp_path: Path):
    result = compute_disposal_manifest(
        tmp_path,
        run_id="2026-07-23-01h00",
        candidates=["archive/handoffs/does-not-exist.md"],
    )
    row = result.manifest["rows"][0]
    assert row["eligible"] is False
    assert row["retention_reason"] == "candidate path absent on disk at assemble time"
    assert row["guards_run"] == [
        {
            "guard": "path-exists",
            "verdict": "block",
            "evidence": "candidate path absent on disk",
        }
    ]


@_requires_rg
def test_compute_disposal_manifest_deletion_groups_additive_key(tmp_path: Path):
    candidates = []
    for i in range(GROUP_THRESHOLD + 5):
        name = f"eligible-{i:03d}.md"
        _full_handoff(tmp_path, name)
        candidates.append(f"archive/handoffs/{name}")

    result = compute_disposal_manifest(tmp_path, run_id="2026-07-23-01h00", candidates=candidates)
    assert "deletion_groups" in result.manifest
    groups = result.manifest["deletion_groups"]
    flat = [p for g in groups for p in g]
    assert len(flat) == len(candidates)
    assert all(len(g) <= GROUP_THRESHOLD for g in groups)


@_requires_rg
def test_compute_disposal_manifest_no_deletion_groups_below_threshold(tmp_path: Path):
    _full_handoff(tmp_path, "eligible.md")
    result = compute_disposal_manifest(
        tmp_path, run_id="2026-07-23-01h00", candidates=["archive/handoffs/eligible.md"]
    )
    assert "deletion_groups" not in result.manifest


# ---------------------------------------------------------------------------
# write_disposal_manifest
# ---------------------------------------------------------------------------


@_requires_rg
def test_write_disposal_manifest_writes_valid_json(tmp_path: Path):
    _full_handoff(tmp_path, "eligible.md")
    result = compute_disposal_manifest(
        tmp_path, run_id="2026-07-23-01h00", candidates=["archive/handoffs/eligible.md"]
    )
    written = write_disposal_manifest(tmp_path, result.manifest)
    assert written == (
        tmp_path
        / "state"
        / "scratch"
        / "artifact-distillation"
        / "2026-07-23-01h00"
        / "disposal-manifest.json"
    )
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert next(iter(loaded)) == "schema_version"
    assert loaded["run_id"] == "2026-07-23-01h00"


@_requires_rg
def test_write_disposal_manifest_write_confined_to_own_run_id(tmp_path: Path):
    _full_handoff(tmp_path, "eligible.md")
    result_a = compute_disposal_manifest(
        tmp_path, run_id="2026-07-23-01h00", candidates=["archive/handoffs/eligible.md"]
    )
    result_b = compute_disposal_manifest(
        tmp_path, run_id="2026-07-23-02h00", candidates=["archive/handoffs/eligible.md"]
    )
    written_a = write_disposal_manifest(tmp_path, result_a.manifest)
    written_b = write_disposal_manifest(tmp_path, result_b.manifest)
    assert written_a != written_b
    assert written_a.exists()
    assert written_b.exists()
    assert json.loads(written_a.read_text())["run_id"] == "2026-07-23-01h00"
    assert json.loads(written_b.read_text())["run_id"] == "2026-07-23-02h00"


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_raises_on_repo_root_none():
    with pytest.raises(ValueError, match="_origin_worktree"):
        _handler({"run_id": "2026-07-23-01h00", "candidates": []}, repo_root=None)


def test_handler_raises_on_missing_run_id(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="run_id"):
        _handler({"candidates": []}, repo_root=tmp_path / ".git")


def test_handler_raises_on_missing_candidates(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="candidates"):
        _handler({"run_id": "2026-07-23-01h00"}, repo_root=tmp_path / ".git")


@_requires_rg
def test_dispatch_message_smoke(tmp_path: Path):
    """End-to-end command-type dispatch via the REAL registered wiring
    (ops/__init__.py eager import + _registry_map.py + op_scopes.py + the
    @register_op decorator)."""
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    _full_handoff(tmp_path, "eligible.md")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.assemble_disposal_manifest",
        "params": {
            "run_id": "2026-07-23-01h00",
            "candidates": ["archive/handoffs/eligible.md"],
        },
        "_origin_worktree": str(tmp_path),
    }
    d = _run(ipc.dispatch_message(msg))
    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    assert d["result"]["run_id"] == "2026-07-23-01h00"
    assert d["result"]["counts"]["total_scanned"] == 1
    # § 1b — the op result carries a human-legible render, not just counts.
    assert "archive/handoffs/eligible.md" in d["result"]["rendered_manifest"]
    assert "ELIGIBLE" in d["result"]["rendered_manifest"]
