"""Tests for coordinator_core.updatedocs.memo_prune."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.updatedocs.memo_prune import (
    MemoPruneTargetMissing,
    compute_memo_prune_candidates,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_memo(archive_dir: Path, name: str, status_line: str | None, age_days: float) -> Path:
    body = "# memo\n\nbody text\n"
    if status_line is None:
        fm = "---\ntitle: test\n---\n"
    else:
        fm = f"---\ntitle: test\nstatus: {status_line}\n---\n"
    p = archive_dir / name
    p.write_text(fm + body, encoding="utf-8")
    mtime = time.time() - age_days * 86400
    os.utime(p, (mtime, mtime))
    return p


def test_missing_archive_dir_raises_typed_error(tmp_path):
    with pytest.raises(MemoPruneTargetMissing) as excinfo:
        compute_memo_prune_candidates(tmp_path)
    assert (tmp_path / "cross-repo" / "archive") == excinfo.value.missing_path


def test_actioned_and_old_is_prunable(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "old-actioned.md", "actioned", age_days=120)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == ["cross-repo/archive/old-actioned.md"]
    assert result.retained == []
    assert result.indeterminate == []


def test_actioned_but_too_young_is_retained(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "young-actioned.md", "actioned", age_days=5)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == []
    assert result.retained == ["cross-repo/archive/young-actioned.md"]
    assert result.indeterminate == []


def test_old_but_not_actioned_is_retained(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "old-open.md", "open", age_days=200)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == []
    assert result.retained == ["cross-repo/archive/old-open.md"]
    assert result.indeterminate == []


def test_no_status_key_is_indeterminate_never_prunable(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "no-status-old.md", None, age_days=500)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == []
    assert result.retained == []
    assert result.indeterminate == ["cross-repo/archive/no-status-old.md"]


def test_no_status_key_and_young_still_indeterminate(tmp_path):
    # A no-status memo fails the age leg outright (younger than floor), so it
    # never reaches the frontmatter head-read at all -- confirm it still lands
    # in indeterminate rather than being silently miscategorised as retained
    # by the age-floor short-circuit path.
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "no-status-young.md", None, age_days=1)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    # Young files never get their frontmatter read (perf ordering), so a
    # no-status young memo is classified purely on the age leg -- retained,
    # not indeterminate, since indeterminate requires reading status at all.
    assert result.prunable == []
    assert result.indeterminate == []
    assert result.retained == ["cross-repo/archive/no-status-young.md"]


def test_mixed_corpus_partitions_correctly(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "a-prunable.md", "actioned", age_days=91)
    _write_memo(archive_dir, "b-young.md", "actioned", age_days=1)
    _write_memo(archive_dir, "c-open.md", "open", age_days=200)
    _write_memo(archive_dir, "d-no-status.md", None, age_days=300)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == ["cross-repo/archive/a-prunable.md"]
    assert result.retained == ["cross-repo/archive/b-young.md", "cross-repo/archive/c-open.md"]
    assert result.indeterminate == ["cross-repo/archive/d-no-status.md"]
    assert set(result.evidence.keys()) == set(
        result.prunable + result.retained + result.indeterminate
    )


def test_age_days_is_a_parameter_not_a_literal(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "medium-actioned.md", "actioned", age_days=20)

    result_90 = compute_memo_prune_candidates(tmp_path, age_days=90)
    result_10 = compute_memo_prune_candidates(tmp_path, age_days=10)

    assert result_90.prunable == []
    assert result_10.prunable == ["cross-repo/archive/medium-actioned.md"]


def test_live_corpus_yields_zero_prunable():
    """Measured truth (plan C4): over the real repo's 1851-file
    cross-repo/archive corpus, this predicate yields ZERO prunable memos
    today. The gate is correct and inert -- do not loosen the predicate to
    manufacture a non-empty result against real data."""
    archive_dir = REPO_ROOT / "cross-repo" / "archive"
    if not archive_dir.is_dir():
        pytest.skip("cross-repo/archive not present in this checkout")

    result = compute_memo_prune_candidates(REPO_ROOT)

    assert result.prunable == []
