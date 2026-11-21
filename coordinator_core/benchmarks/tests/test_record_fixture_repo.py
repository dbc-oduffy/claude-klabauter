"""`record_fixture_repo` builds a record-only repo in the OS temp dir: no clone, no alternates."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.isolated_clone import record_fixture_repo


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "d.md").write_text("doc\n", encoding="utf-8")
    (src / "other.py").write_text("X = 1\n", encoding="utf-8")
    return src


def test_fixture_is_removed_on_error(tmp_path):
    kept = None
    with pytest.raises(RuntimeError):
        with record_fixture_repo(_source(tmp_path), [], prefix="rfr-test", label="t") as repo:
            kept = repo
            raise RuntimeError("boom")
    assert kept is not None and not kept.exists()
