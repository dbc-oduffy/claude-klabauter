"""Tests for coordinator_core.updatedocs.memo_prune."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.memo_prune import compute_memo_prune_candidates

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
    """Neither `state/cross-repo/` nor `cross-repo/` exists under tmp_path,
    so `memo_corpus_root`'s write-when-neither-exists rule returns the NEW
    root (`state/cross-repo/`) -- see its own docstring. The missing path
    this raises must match that resolution, not the legacy literal."""
    with pytest.raises(UpdatedocsTargetMissing) as excinfo:
        compute_memo_prune_candidates(tmp_path)
    assert Path(memo_corpus_root(str(tmp_path))) / "archive" == excinfo.value.missing_path


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
    """Indeterminate is AGE-INDEPENDENT, which is the whole three-state contract.

    Regression guard. An earlier revision stat-ed first and skipped the
    frontmatter head-read for files younger than the floor, so a young
    no-status memo was folded into `retained` — indistinguishable from a memo
    whose status was actually read and found non-actioned. This test asserted
    that as correct while its own name promised the opposite.

    The perf ordering it protected was real (17.0ms vs 98.4ms over 1851 memos)
    and is not worth a bucket that lies: 98ms sits well under the 500ms
    brightline.
    """
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "no-status-young.md", None, age_days=1)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.prunable == []
    assert result.retained == []
    assert result.indeterminate == ["cross-repo/archive/no-status-young.md"]


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


def test_age_days_is_a_parameter_not_a_literal(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "medium-actioned.md", "actioned", age_days=20)

    result_90 = compute_memo_prune_candidates(tmp_path, age_days=90)
    result_10 = compute_memo_prune_candidates(tmp_path, age_days=10)

    assert result_90.prunable == []
    assert result_10.prunable == ["cross-repo/archive/medium-actioned.md"]


def test_live_corpus_yields_zero_prunable():
    """Measured truth (plan C4, re-verified against the memo_corpus_root-
    resolved corpus by 2026-09-03's C2): over the real repo's memo-archive
    corpus, this predicate yields ZERO prunable memos today. The gate is
    correct and inert -- do not loosen the predicate to manufacture a
    non-empty result against real data."""
    archive_dir = Path(memo_corpus_root(str(REPO_ROOT))) / "archive"
    if not archive_dir.is_dir():
        pytest.skip("memo-corpus archive not present in this checkout")

    result = compute_memo_prune_candidates(REPO_ROOT)

    assert result.prunable == []


def test_unreadable_memo_is_indeterminate_not_retained(tmp_path, monkeypatch):
    """"Could not read the status" and "read it, not actioned" are different states.

    Both used to land in `retained`, which is the same collapse this module
    exists to prevent, one layer down from the no-status case.
    """
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "unreadable.md", "actioned", age_days=200)

    import pathlib

    real_open = pathlib.Path.open

    def _boom(self, *a, **kw):
        if self.name == "unreadable.md":
            raise OSError("simulated permissions failure")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "open", _boom)

    result = compute_memo_prune_candidates(tmp_path, age_days=90)

    assert result.indeterminate == ["cross-repo/archive/unreadable.md"]
    assert result.retained == []
    assert result.prunable == []
