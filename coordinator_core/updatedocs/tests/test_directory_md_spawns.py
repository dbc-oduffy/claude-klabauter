
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.updatedocs.directory_md import compute_directory_md_drift

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REAL_REFRESHED_LINE = (
    "Last refreshed: 2026-08-06 (spot-check only — see coverage note above)."
)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _git(root, *args) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    )


def test_count_claim_counts_tracked_files_not_untracked_or_node_modules(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("", encoding="utf-8")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "x.py").write_text("", encoding="utf-8")
    _git(tmp_path, "add", "a.py", "b.py")

    tracked_doc = _write(tmp_path, "DIRECTORY.md", "2 `*.py` files\n")
    claim = compute_directory_md_drift(tracked_doc).count_claims[0]
    assert (claim.asserted, claim.actual, claim.matches) == (2, 2, True)

    disk_doc = _write(tmp_path, "DIRECTORY.md", "4 `*.py` files\n")
    claim = compute_directory_md_drift(disk_doc).count_claims[0]
    assert (claim.asserted, claim.actual, claim.matches) == (4, 2, False)
