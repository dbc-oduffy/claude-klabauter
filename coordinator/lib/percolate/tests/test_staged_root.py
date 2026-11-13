"""staged_root builds the candidate tree beside the live root and swaps it in per top-level unit."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate import staged_root  # noqa: E402
from percolate.diff_commit import DestWrite  # noqa: E402


def _w(rel, text="x\n", mode=0o100644):
    return DestWrite(rel, text.encode(), mode)


def _live(tmp_path):
    root = tmp_path / "live"
    for rel, text in {
        "pkg/a.py": "A0\n",
        "pkg/sub/b.py": "B0\n",
        "other/c.txt": "C0\n",
        "bin/run.sh": "R0\n",
        "top.txt": "T0\n",
        "gone.txt": "G0\n",
        ".git/HEAD": "ref\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    return root


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _siblings(root):
    return [p.name for p in root.parent.iterdir() if p.name.startswith(f".{root.name}.")]


def test_stage_tree_builds_beside_the_root_and_writes_nothing_under_it(tmp_path, monkeypatch):
    root = _live(tmp_path)
    before = _snapshot(root)
    written = []
    real = staged_root.write_entry
    monkeypatch.setattr(staged_root, "write_entry", lambda p, d, m: (written.append(Path(p)), real(p, d, m)))
    staged = staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], [])
    assert _snapshot(root) == before
    assert staged.parent == root.parent and root not in staged.parents
    assert written and all(staged in p.parents for p in written)
    assert (staged / "pkg" / "a.py").read_bytes() == b"A1\n"
    assert (staged / "pkg" / "sub" / "b.py").read_bytes() == b"B0\n"
    assert not (staged / "other").exists() and not (staged / ".git").exists()


def test_unchanged_files_enter_the_unit_by_hardlink(tmp_path):
    root = _live(tmp_path)
    staged = staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], [])
    assert os.path.samefile(staged / "pkg" / "sub" / "b.py", root / "pkg" / "sub" / "b.py")
    assert not os.path.samefile(staged / "pkg" / "a.py", root / "pkg" / "a.py")
    assert (root / "pkg" / "a.py").read_bytes() == b"A0\n"


def test_unchanged_files_are_copied_when_hardlinks_are_refused(tmp_path, monkeypatch):
    root = _live(tmp_path)

    def refuse(src, dst, **k):
        raise OSError("no links")

    monkeypatch.setattr(os, "link", refuse)
    staged = staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], [])
    monkeypatch.undo()
    assert (staged / "pkg" / "sub" / "b.py").read_bytes() == b"B0\n"
    assert not os.path.samefile(staged / "pkg" / "sub" / "b.py", root / "pkg" / "sub" / "b.py")


def test_swap_makes_units_live_and_returns_the_old_ones(tmp_path):
    root = _live(tmp_path)
    staged = staged_root.stage_tree(
        root,
        [_w("pkg/a.py", "A1\n"), _w("pkg/new/n.py", "N\n"), _w("top.txt", "T1\n"), _w("fresh/f.txt", "F\n")],
        ["pkg/sub/b.py", "gone.txt"],
    )
    grave = staged_root.swap_root(staged, root)
    assert _snapshot(root) == {
        "pkg/a.py": b"A1\n",
        "pkg/new/n.py": b"N\n",
        "other/c.txt": b"C0\n",
        "bin/run.sh": b"R0\n",
        "top.txt": b"T1\n",
        "fresh/f.txt": b"F\n",
        ".git/HEAD": b"ref\n",
    }
    assert (grave / "pkg" / "a.py").read_bytes() == b"A0\n" and (grave / "top.txt").read_bytes() == b"T0\n"
    assert (grave / "gone.txt").read_bytes() == b"G0\n"
    staged_root.discard(staged)
    staged_root.discard(grave)
    assert _siblings(root) == []


def test_a_unit_emptied_by_deletions_is_removed(tmp_path):
    root = _live(tmp_path)
    grave = staged_root.swap_root(staged_root.stage_tree(root, [], ["bin/run.sh"]), root)
    assert not (root / "bin").exists() and (grave / "bin" / "run.sh").exists()
    staged_root.discard(grave)


def test_the_last_write_unit_swaps_last(tmp_path, monkeypatch):
    root = _live(tmp_path)
    order = []
    real_rename, real_replace = os.rename, os.replace
    monkeypatch.setattr(os, "rename", lambda s, d, *a, **k: (order.append(Path(d).name) if Path(d).parent == root else None, real_rename(s, d))[1])
    monkeypatch.setattr(os, "replace", lambda s, d: (order.append(Path(d).name) if Path(d).parent == root else None, real_replace(s, d))[1])
    writes = [_w("pkg/a.py", "A1\n"), _w("bin/run.sh", "R1\n"), _w("top.txt", "T1\n"), _w("other/c.txt", "C1\n")]
    staged_root.swap_root(staged_root.stage_tree(root, writes, [], last="pkg/a.py"), root)
    assert order[-1] == "pkg" and set(order) == {"pkg", "bin", "top.txt", "other"}


def test_a_failed_rename_rolls_everything_back_and_is_not_retried(tmp_path, monkeypatch):
    root = _live(tmp_path)
    before = _snapshot(root)
    staged = staged_root.stage_tree(
        root, [_w("bin/run.sh", "R1\n"), _w("other/c.txt", "C1\n"), _w("pkg/a.py", "A1\n"), _w("top.txt", "T1\n")], ["gone.txt"]
    )
    real = os.rename
    refused = []

    def rename(src, dst, *a, **k):
        if Path(dst) == root / "other" and Path(src).parent.name.startswith(f".{root.name}.stage-"):
            refused.append(src)
            raise PermissionError(32, "in use")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(staged_root.SwapError, match="other"):
        staged_root.swap_root(staged, root)
    monkeypatch.undo()
    assert _snapshot(root) == before
    assert len(refused) == 1
    assert [n for n in _siblings(root) if ".old-" in n] == []
    staged_root.discard(staged)


@pytest.mark.parametrize("rel", [".git/config", "../escape.txt", "/abs.txt", "pkg/../../x"])
def test_paths_that_leave_the_root_or_name_git_are_refused(tmp_path, rel):
    root = _live(tmp_path)
    with pytest.raises(staged_root.StageError):
        staged_root.stage_tree(root, [_w(rel)], [])
    assert _siblings(root) == []


def test_a_failed_stage_leaves_no_container(tmp_path, monkeypatch):
    root = _live(tmp_path)

    def boom(path, data, mode):
        raise OSError("disk full")

    monkeypatch.setattr(staged_root, "write_entry", boom)
    with pytest.raises(OSError):
        staged_root.stage_tree(root, [_w("pkg/a.py")], [])
    assert _siblings(root) == []


def test_exec_mode_and_symlink_entries_are_staged(tmp_path):
    root = _live(tmp_path)
    staged = staged_root.stage_tree(root, [_w("bin/tool", "#!/bin/sh\n", 0o100755)], [])
    if os.name != "nt":
        assert (staged / "bin" / "tool").stat().st_mode & 0o111
    link = _w("bin/lnk", "run.sh", 0o120000)
    try:
        staged = staged_root.stage_tree(root, [link], [])
    except OSError:
        pytest.skip("symlink creation not permitted here")
    assert (staged / "bin" / "lnk").is_symlink() or os.name == "nt"


def test_swap_refuses_when_a_unit_changed_after_staging(tmp_path):
    root = _live(tmp_path)
    staged = staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], [])
    (root / "pkg" / "late.py").write_text("late\n")
    with pytest.raises(staged_root.SwapError, match="pkg"):
        staged_root.swap_root(staged, root)
    assert (root / "pkg" / "a.py").read_text() == "A0\n" and (root / "pkg" / "late.py").exists()
    staged_root.discard(staged)


def test_swap_ignores_pycache_written_after_staging(tmp_path):
    root = _live(tmp_path)
    staged = staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], [])
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"x")
    grave = staged_root.swap_root(staged, root)
    assert (root / "pkg" / "a.py").read_text() == "A1\n"
    staged_root.discard(grave)


def test_stage_tree_sweeps_stale_siblings_but_not_fresh_ones(tmp_path):
    root = _live(tmp_path)
    stale = root.parent / ".live.old-1"
    fresh = root.parent / ".live.stage-2"
    for d in (stale, fresh):
        (d / "x").mkdir(parents=True)
    old = os.stat(stale).st_mtime - 7200
    os.utime(stale, (old, old))
    staged_root.discard(staged_root.stage_tree(root, [_w("pkg/a.py", "A1\n")], []))
    assert not stale.exists() and fresh.exists()

