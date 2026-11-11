"""Tests for coordinator_core.ops.repo_scratch_sweep (tmp_path only)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import repo_scratch_sweep as rss

DAY = 86400.0


def _age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def _file(path: Path, days: float, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _age(path, days)
    return path


def _tree(root: Path, days: float) -> Path:
    _file(root / "a.txt", days)
    _file(root / "sub" / "b.txt", days)
    _age(root / "sub", days)
    _age(root, days)
    return root


def _make_dir_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    return False


def test_missing_scratch_returns_empty(tmp_path):
    r = rss.sweep_repo_scratch(tmp_path)
    assert r["deleted"] == 0 and r["remaining"] == 0 and not r["batch_exhausted"]
    assert r["skipped"] == {"young": 0, "link": 0, "held": 0, "run_named": 0}


def test_default_deletes_aged_keeps_young(tmp_path):
    s = tmp_path / "scratch"
    _tree(s / "old", 10)
    _file(s / "old.txt", 10)
    _file(s / "young.txt", 1)
    _tree(s / "mixed", 10)
    _file(s / "mixed" / "fresh.txt", 0)
    r = rss.sweep_repo_scratch(tmp_path)
    assert not (s / "old").exists() and not (s / "old.txt").exists()
    assert (s / "young.txt").exists()
    assert (s / "mixed" / "a.txt").exists() and (s / "mixed" / "fresh.txt").exists()
    assert r["skipped"]["young"] == 2
    assert r["deleted"] == 5  # old: a.txt, b.txt, sub, old; plus old.txt


def test_default_counts_match_removals(tmp_path):
    s = tmp_path / "scratch"
    _tree(s / "old", 10)
    r = rss.sweep_repo_scratch(tmp_path)
    assert r["deleted"] == 4
    assert not (s / "old").exists()


def test_dry_run_touches_nothing(tmp_path):
    s = tmp_path / "scratch"
    _tree(s / "old", 10)
    r = rss.sweep_repo_scratch(tmp_path, dry_run=True)
    assert r["deleted"] == 4 and r["dry_run"] is True
    assert (s / "old" / "sub" / "b.txt").exists()


def test_batch_cap_stops_and_resumes(tmp_path):
    s = tmp_path / "scratch"
    for i in range(10):
        _file(s / f"f{i:02d}.txt", 10)
    r = rss.sweep_repo_scratch(tmp_path, batch=4)
    assert r["deleted"] == 4 and r["batch_exhausted"] is True and r["remaining"] == 6
    assert len(list(s.iterdir())) == 6
    for _ in range(2):
        r = rss.sweep_repo_scratch(tmp_path, batch=4)
    assert list(s.iterdir()) == []


def test_partly_deleted_tree_still_ages_out(tmp_path):
    s = tmp_path / "scratch"
    for i in range(6):
        _file(s / "big" / f"f{i}.txt", 10)
    _age(s / "big", 10)
    r = rss.sweep_repo_scratch(tmp_path, batch=3)
    assert r["batch_exhausted"] is True
    # Deleting children bumped the directory mtime; the leftovers must not read young.
    r2 = rss.sweep_repo_scratch(tmp_path, batch=50)
    assert not (s / "big").exists()
    assert r2["skipped"]["young"] == 0


def test_nested_link_never_descended_target_untouched(tmp_path):
    outside = tmp_path / "outside"
    keep = _file(outside / "keep.txt", 30)
    s = tmp_path / "scratch"
    _tree(s / "tree", 10)
    link = s / "tree" / "lnk"
    if not _make_dir_link(link, outside):
        pytest.skip("cannot create a directory link here")
    # A link's own mtime cannot be backdated portably; a negative threshold ages everything.
    rss.sweep_repo_scratch(tmp_path, max_age_days=-1)
    assert keep.exists()
    assert not (s / "tree").exists()


def test_top_level_aged_link_removed_target_kept(tmp_path):
    outside = tmp_path / "outside"
    keep = _file(outside / "keep.txt", 30)
    s = tmp_path / "scratch"
    s.mkdir()
    link = s / "lnk"
    if not _make_dir_link(link, outside):
        pytest.skip("cannot create a directory link here")
    r = rss.sweep_repo_scratch(tmp_path, max_age_days=-1)
    assert keep.exists() and not os.path.lexists(link)
    assert r["deleted"] == 1


def test_young_link_counted_and_kept(tmp_path):
    outside = tmp_path / "outside"
    _file(outside / "keep.txt", 30)
    s = tmp_path / "scratch"
    s.mkdir()
    link = s / "lnk"
    if not _make_dir_link(link, outside):
        pytest.skip("cannot create a directory link here")
    r = rss.sweep_repo_scratch(tmp_path)
    assert os.path.lexists(link) and r["skipped"]["link"] == 1
    assert (outside / "keep.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file symlink")
def test_posix_file_symlink_target_untouched(tmp_path):
    outside = _file(tmp_path / "outside.txt", 30)
    s = tmp_path / "scratch"
    s.mkdir()
    link = s / "lnk"
    os.symlink(outside, link)
    rss.sweep_repo_scratch(tmp_path, max_age_days=-1)
    assert outside.exists() and not os.path.lexists(link)


def _state(repo: Path, run: str, stage: str | None) -> None:
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True)
    if stage is not None:
        (d / "run-stage.json").write_text(json.dumps({"stage": stage}))


def test_run_named_live_skipped_terminal_swept(tmp_path):
    s = tmp_path / "scratch" / "warp"
    for name in ("live", "halted", "landed", "nostate", "garbled"):
        _file(s / name / "f.txt", 10)
        _age(s / name, 10)
    _state(tmp_path, "live", "staged")
    _state(tmp_path, "halted", "halted")
    _state(tmp_path, "landed", "landed")
    _state(tmp_path, "garbled", None)
    (tmp_path / "state" / "warp" / "garbled" / "run-stage.json").write_text("{not json")
    r = rss.sweep_repo_scratch(tmp_path)
    assert (s / "live" / "f.txt").exists()
    assert (s / "halted" / "f.txt").exists()
    assert (s / "nostate" / "f.txt").exists()
    assert (s / "garbled" / "f.txt").exists()
    assert not (s / "landed").exists()
    assert r["skipped"]["run_named"] == 4
    assert s.exists()


def test_held_probe_windows_path_skips_and_reports_tombstone(tmp_path, monkeypatch):
    s = tmp_path / "scratch"
    _tree(s / "a", 10)
    _tree(s / "b", 10)
    monkeypatch.setattr(rss, "_IS_WINDOWS", True)
    real = os.rename

    def fake(src, dst):
        if str(src).endswith(os.sep + "a"):
            raise PermissionError("held")
        if os.path.basename(str(src)).startswith("b.sweep-tomb-"):
            raise PermissionError("rename back")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", fake)
    r = rss.sweep_repo_scratch(tmp_path)
    assert (s / "a" / "a.txt").exists()
    assert r["skipped"]["held"] == 2
    assert len(r["tombstones"]) == 1 and os.path.exists(r["tombstones"][0])
    assert (tmp_path / "scratch" / "b").exists() is False


@pytest.mark.skipif(os.name != "nt", reason="real held-file probe is Windows only")
def test_windows_open_file_is_held(tmp_path):
    s = tmp_path / "scratch"
    f = _file(s / "d" / "held.txt", 10)
    _age(s / "d", 10)
    with open(f, "rb"):
        r = rss.sweep_repo_scratch(tmp_path)
    assert f.exists() and r["skipped"]["held"] == 1
    r = rss.sweep_repo_scratch(tmp_path)
    assert not (s / "d").exists()


def test_posix_probe_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(rss, "_IS_WINDOWS", False)
    monkeypatch.setattr(os, "rename", lambda *a: (_ for _ in ()).throw(AssertionError("rename")))
    _file(tmp_path / "scratch" / "x.txt", 10)
    assert rss.sweep_repo_scratch(tmp_path)["deleted"] == 1


def test_five_thousand_entries_under_500ms_process_time(tmp_path):
    s = tmp_path / "scratch"
    s.mkdir()
    t = time.time() - 10 * DAY
    for i in range(5000):
        p = s / f"e{i:05d}"
        p.write_text("x")
        os.utime(p, (t, t))
    start = time.process_time()
    r = rss.sweep_repo_scratch(tmp_path)
    cost = time.process_time() - start
    assert r["deleted"] == rss.DEFAULT_BATCH and r["batch_exhausted"] is True
    assert r["remaining"] == 5000 - rss.DEFAULT_BATCH
    assert cost < 0.5
