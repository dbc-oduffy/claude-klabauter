"""scripts/normalize_handoff_layout.py: dry-run moves nothing, --apply canonicalizes, rerun is a no-op."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "normalize_handoff_layout.py"


def _baton(root: Path, rel: str, state: str = "ready_to_fire", claimed: str | None = None) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ndeployment_state: {state}\n"
    if claimed is not None:
        fm += f"claimed_by: {claimed}\n"
    p.write_text(fm + "---\nbody\n", encoding="utf-8")


def _fixture(root: Path) -> None:
    _baton(root, "state/handoffs/2026-10-01-flat-live.md")
    _baton(root, "state/handoffs/2026-10-02-flat-done.md", "shipped")
    _baton(root, "state/handoffs/2026-10-04-flat-claimed.md", claimed="sess-1")
    _baton(root, "state/handoffs/2026-10/2026-10-06-month-live.md")
    _baton(root, "state/handoffs/2026-10/2026-10-07-month-done.md", "closed")
    _baton(root, "state/handoffs/2026-10-09-perdir/2026-10-09-perdir.md")
    (root / "state/handoffs/mixed").mkdir()
    (root / "state/handoffs/mixed/a.md").write_text("x", encoding="utf-8")
    (root / "state/handoffs/mixed/b.md").write_text("y", encoding="utf-8")
    _baton(root, "archive/handoffs/2026-09-01-archived-flat.md", "shipped")


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_dry_run_moves_nothing_and_reports_counts(tmp_path):
    _fixture(tmp_path)
    before = _tree(tmp_path)
    proc = _run("--repo", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert _tree(tmp_path) == before
    assert report["dry_run"] is True
    assert report["flat"] == 3
    assert report["month"] == 2
    assert report["per_handoff_dir"] == 2
    assert report["terminal"] == 3
    assert report["skipped_live_or_claimed"] == 1
    assert report["unclassified_dirs"] == ["state/handoffs/mixed"]
    assert report["moves"] > 0


def test_apply_canonicalizes_and_rerun_is_noop(tmp_path):
    _fixture(tmp_path)
    first = json.loads(_run("--repo", str(tmp_path), "--apply").stdout)
    assert first["dry_run"] is False and first["moved"] == first["moves"] > 0
    names = _tree(tmp_path)
    assert "state/handoffs/2026-10/2026-10-01-flat-live.md" in names
    assert "archive/handoffs/2026-10/2026-10-02-flat-done.md" in names
    assert "archive/handoffs/2026-10/2026-10-07-month-done.md" in names
    assert "archive/handoffs/2026-09/2026-09-01-archived-flat.md" in names
    assert "state/handoffs/2026-10/2026-10-09-perdir.md" in names
    assert "state/handoffs/2026-10-04-flat-claimed.md" in names  # claimed live baton untouched
    assert "state/handoffs/mixed/a.md" in names  # unclassified dir untouched
    assert not (tmp_path / "state/handoffs/2026-10-09-perdir").exists()
    second = json.loads(_run("--repo", str(tmp_path), "--apply").stdout)
    assert second["moves"] == 0 and second["moved"] == 0
    assert _tree(tmp_path) == names


def test_missing_handoff_tree_is_reported_not_failed(tmp_path):
    proc = _run("--repo", str(tmp_path))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["missing"] is True


def test_repo_not_a_directory_exits_2(tmp_path):
    proc = _run("--repo", str(tmp_path / "nope"))
    assert proc.returncode == 2
