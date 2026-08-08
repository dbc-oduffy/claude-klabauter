"""
coordinator_core.ops.tests.test_memo_fate_partition

Tests for the memo.fate_partition MUTATING op (mechanical distill_fate/in_repo_capture
prefilter, DR-228 § D6 scratch-tier).

Coverage:
  (a) partition-sums-to-input invariant (AC6) — a fixture corpus spanning every
      reachable classification path (absent fate, unknown fate, commitment,
      ephemeral, ratification+well-formed+present, ratification+well-formed+absent,
      ratification+malformed) asserts prefiled+residue+capture_missing == total
      == len(records), and that every memo lands in EXACTLY one bucket.
  (b) capture-missing-never-silent fixture (AC6) — a ratification-stamped memo
      whose in_repo_capture target does not exist on disk lands in
      capture_missing, never prefiled, with a non-empty reason.
  (c) malformed in_repo_capture (schema_validate rule reuse, F1) also lands in
      capture_missing, not prefiled.
  (d) prefiled rows carry a pre-rendered log_row aligned to the fate->disposition
      map (ratification->DISTILLED, commitment->PRESERVE, ephemeral->EPHEMERAL).
  (e) handler smoke: writes the shard to
      state/scratch/artifact-distillation/<run_id>/fate-partition.json, schema_version
      pinned, shard_path returned rel-posix.
  (f) run_id validation: missing/unsafe run_id raises (write-confinement guard,
      DR-228 § D6(i)) before any I/O is attempted.

Spec backlink: coordinator_core/ops/memo_fate_partition.py
Plan: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C16 / AC6
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.memo_fate_partition import (
    _capture_target_exists,
    _handler,
    collect_memo_records,
    partition_memos,
)


def _memo(
    *,
    title: str,
    distill_fate=None,
    in_repo_capture=None,
) -> str:
    lines = ["---", f'title: "{title}"', "from: \"sibling-em\"", "to: \"claude-klabauter-em\""]
    if distill_fate is not None:
        lines.append(f"distill_fate: {distill_fate}")
    if in_repo_capture is not None:
        lines.append(f'in_repo_capture: "{in_repo_capture}"')
    lines.append("---")
    lines.append("")
    lines.append(f"## {title}")
    lines.append("")
    lines.append("Body text.")
    return "\n".join(lines) + "\n"


def _write_memo(archive_dir: Path, memo_id: str, **kwargs) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{memo_id}.md").write_text(_memo(**kwargs), encoding="utf-8")




def _build_fixture_corpus(worktree_root: Path) -> Path:
    """A corpus spanning every reachable classification path."""
    archive_dir = worktree_root / "cross-repo" / "archive"

    # docs/decisions/ home that actually exists on disk.
    decisions_dir = worktree_root / "docs" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    (decisions_dir / "DR-900-fixture.md").write_text("# DR-900\n", encoding="utf-8")

    _write_memo(archive_dir, "no-fate", title="No fate memo")
    _write_memo(archive_dir, "unknown-fate", title="Unknown fate memo", distill_fate="bogus")
    _write_memo(archive_dir, "commitment-memo", title="Commitment memo", distill_fate="commitment")
    _write_memo(archive_dir, "ephemeral-memo", title="Ephemeral memo", distill_fate="ephemeral")
    _write_memo(
        archive_dir,
        "ratified-present",
        title="Ratified present",
        distill_fate="ratification",
        in_repo_capture="docs/decisions/DR-900-fixture.md",
    )
    _write_memo(
        archive_dir,
        "ratified-absent",
        title="Ratified absent",
        distill_fate="ratification",
        in_repo_capture="docs/decisions/DR-999-does-not-exist.md",
    )
    _write_memo(
        archive_dir,
        "ratified-malformed",
        title="Ratified malformed",
        distill_fate="ratification",
        in_repo_capture="~/.claude/projects/some-memory-pointer.md",
    )
    return archive_dir


# ---------------------------------------------------------------------------
# _capture_target_exists containment (2026-07-23 code review F2)
# ---------------------------------------------------------------------------


def test_capture_target_exists_true_for_in_tree_path(tmp_path: Path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "DR-900-fixture.md").write_text("# DR-900\n")

    assert _capture_target_exists(tmp_path, "docs/decisions/DR-900-fixture.md") is True


def test_capture_target_exists_false_for_traversal_escape_outside_worktree(tmp_path: Path):
    # _memo_cf_distill_fate's own well-formed check is a literal string-prefix
    # test, not a normalized-path containment check — a capture value that
    # literally starts with an allowed prefix but then walks "../.." out of
    # the tree passes that format check. This is the containment gap this op
    # closes on top of it: a target outside worktree_root must never read as
    # "exists", even if it is real on the actual filesystem.
    outside_target = tmp_path.parent / "outside-secret.md"
    outside_target.write_text("should never be reachable via in_repo_capture\n")

    worktree_root = tmp_path / "repo"
    (worktree_root / "docs" / "decisions").mkdir(parents=True)

    escaping_capture = "docs/decisions/../../../outside-secret.md"
    assert _capture_target_exists(worktree_root, escaping_capture) is False


def test_capture_target_exists_false_for_absolute_path_escape(tmp_path: Path):
    outside_target = tmp_path / "outside-secret.md"
    outside_target.write_text("should never be reachable via in_repo_capture\n")

    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()

    assert _capture_target_exists(worktree_root, str(outside_target)) is False


# ---------------------------------------------------------------------------
# (a) partition-sums-to-input invariant
# ---------------------------------------------------------------------------


def test_partition_sums_to_input(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)

    records, degraded = collect_memo_records(archive_dir, worktree_root)
    assert degraded is False
    assert len(records) == 7

    outcome = partition_memos(records, worktree_root=worktree_root, run_id="run-a")

    counts = outcome["counts"]
    assert counts["total"] == len(records)
    assert counts["prefiled"] + counts["residue"] + counts["capture_missing"] == counts["total"]

    all_ids = (
        {r["memo_id"] for r in outcome["prefiled"]}
        | {r["memo_id"] for r in outcome["residue"]}
        | {r["memo_id"] for r in outcome["capture_missing"]}
    )
    assert len(all_ids) == len(records), "every memo must land in exactly one bucket"


def test_partition_buckets_match_expected_classification(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)
    records, _ = collect_memo_records(archive_dir, worktree_root)
    outcome = partition_memos(records, worktree_root=worktree_root, run_id="run-a")

    prefiled_ids = {r["memo_id"] for r in outcome["prefiled"]}
    residue_ids = {r["memo_id"] for r in outcome["residue"]}
    capture_missing_ids = {r["memo_id"] for r in outcome["capture_missing"]}

    assert residue_ids == {"no-fate", "unknown-fate"}
    assert prefiled_ids == {"commitment-memo", "ephemeral-memo", "ratified-present"}
    assert capture_missing_ids == {"ratified-absent", "ratified-malformed"}


# ---------------------------------------------------------------------------
# (b) capture-missing-never-silent
# ---------------------------------------------------------------------------


def test_ratification_with_absent_target_is_capture_missing_not_prefiled(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)
    records, _ = collect_memo_records(archive_dir, worktree_root)
    outcome = partition_memos(records, worktree_root=worktree_root, run_id="run-a")

    row = next(r for r in outcome["capture_missing"] if r["memo_id"] == "ratified-absent")
    assert row["reason"]
    assert "ratified-absent" not in {r["memo_id"] for r in outcome["prefiled"]}


# ---------------------------------------------------------------------------
# (c) malformed in_repo_capture also lands in capture_missing
# ---------------------------------------------------------------------------


def test_ratification_with_malformed_capture_is_capture_missing(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)
    records, _ = collect_memo_records(archive_dir, worktree_root)
    outcome = partition_memos(records, worktree_root=worktree_root, run_id="run-a")

    row = next(r for r in outcome["capture_missing"] if r["memo_id"] == "ratified-malformed")
    assert row["reason"]


# ---------------------------------------------------------------------------
# (d) prefiled rows carry a pre-rendered log_row
# ---------------------------------------------------------------------------


def test_prefiled_rows_carry_log_row_aligned_to_disposition(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)
    records, _ = collect_memo_records(archive_dir, worktree_root)
    outcome = partition_memos(records, worktree_root=worktree_root, run_id="run-a")

    by_id = {r["memo_id"]: r for r in outcome["prefiled"]}
    assert "-> DISTILLED, ratification (run: run-a)" in by_id["ratified-present"]["log_row"]
    assert "-> PRESERVE, commitment (run: run-a)" in by_id["commitment-memo"]["log_row"]
    assert "-> EPHEMERAL, ephemeral (run: run-a)" in by_id["ephemeral-memo"]["log_row"]


# ---------------------------------------------------------------------------
# (e) handler smoke — writes the shard, schema_version pinned
# ---------------------------------------------------------------------------


def test_handler_writes_shard_to_run_scoped_path(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)

    result = _handler(
        {"run_id": "run-xyz", "archive_dir": str(archive_dir)},
        repo_root=worktree_root / ".git",
    )

    assert result["schema_version"] >= 1
    assert result["run_id"] == "run-xyz"
    assert result["shard_path"] == "state/scratch/artifact-distillation/run-xyz/fate-partition.json"

    shard_file = worktree_root / "state" / "scratch" / "artifact-distillation" / "run-xyz" / "fate-partition.json"
    assert shard_file.is_file()
    on_disk = json.loads(shard_file.read_text(encoding="utf-8"))
    assert on_disk["run_id"] == "run-xyz"
    assert on_disk["counts"]["total"] == 7


def test_handler_is_idempotent_on_rerun_same_run_id(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    archive_dir = _build_fixture_corpus(worktree_root)

    first = _handler({"run_id": "run-rerun", "archive_dir": str(archive_dir)}, repo_root=worktree_root / ".git")
    second = _handler({"run_id": "run-rerun", "archive_dir": str(archive_dir)}, repo_root=worktree_root / ".git")
    assert first["counts"] == second["counts"]


# ---------------------------------------------------------------------------
# (f) run_id validation — write-confinement guard
# ---------------------------------------------------------------------------


def test_handler_rejects_missing_run_id(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    with pytest.raises(ValueError, match="run_id"):
        _handler({}, repo_root=worktree_root / ".git")


def test_handler_rejects_unsafe_run_id(tmp_path: Path):
    worktree_root = tmp_path / "repo"
    with pytest.raises(ValueError, match="safe path segment"):
        _handler({"run_id": "../escape"}, repo_root=worktree_root / ".git")


def test_handler_rejects_none_repo_root():
    with pytest.raises(ValueError, match="repo_root"):
        _handler({"run_id": "run-a"}, repo_root=None)
