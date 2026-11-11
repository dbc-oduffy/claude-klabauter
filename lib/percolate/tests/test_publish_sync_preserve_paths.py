"""`preserve_paths` on `sync_mirror` / `sync_flat_mirror`: exact destination file
holds are neither overwritten nor deleted, bypass the copier, and print as held
unverified. Pins both sides: held stays untouched, unheld still syncs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2]
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from percolate import publish_sync as ps  # noqa: E402
from percolate.publish_sync import IgnoreMatcher  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _recording_copier(calls: list):
    def copier(src: Path, dst: Path, dry: bool) -> None:
        calls.append(dst.name)
        if not dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    return copier


def _flat(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "held.txt", "new-held")
    _write(src / "free.txt", "new-free")
    _write(dst / "held.txt", "old-held")
    _write(dst / "free.txt", "old-free")
    _write(dst / "orphan-held.txt", "keep")
    return src, dst


def test_flat_held_file_not_overwritten_and_bypasses_callback(tmp_path, capsys):
    src, dst = _flat(tmp_path)
    calls: list = []
    ps.sync_flat_mirror(
        src, dst, IgnoreMatcher([]), False,
        copy_file=_recording_copier(calls), preserve_paths=["held.txt"],
    )
    assert (dst / "held.txt").read_text() == "old-held"
    assert (dst / "free.txt").read_text() == "new-free"
    assert calls == ["free.txt"]
    assert "HELD UNVERIFIED: held.txt" in capsys.readouterr().out


def test_flat_held_destination_only_file_not_deleted(tmp_path):
    src, dst = _flat(tmp_path)
    ps.sync_flat_mirror(
        src, dst, IgnoreMatcher([]), False, preserve_paths=["orphan-held.txt"]
    )
    assert (dst / "orphan-held.txt").read_text() == "keep"


def test_flat_unheld_orphan_still_deleted(tmp_path):
    src, dst = _flat(tmp_path)
    ps.sync_flat_mirror(
        src, dst, IgnoreMatcher([]), False, preserve_paths=["held.txt"]
    )
    assert not (dst / "orphan-held.txt").exists()


def test_flat_only_paths_skips_held(tmp_path):
    src, dst = _flat(tmp_path)
    ps.sync_flat_mirror(
        src, dst, IgnoreMatcher([]), False,
        only_paths=["held.txt", "free.txt"], preserve_paths=["held.txt"],
    )
    assert (dst / "held.txt").read_text() == "old-held"
    assert (dst / "free.txt").read_text() == "new-free"


def test_mirror_nested_held_file_not_overwritten_or_deleted(tmp_path, capsys):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "plug" / "held.txt", "new")
    _write(src / "plug" / "free.txt", "new-free")
    _write(dst / "plug" / "held.txt", "old")
    _write(dst / "plug" / "free.txt", "old-free")
    _write(dst / "plug" / "dst-only.txt", "keep")
    calls: list = []
    ps.sync_mirror(
        src, dst, IgnoreMatcher([]), False,
        copy_file=_recording_copier(calls),
        preserve_paths=["plug/held.txt", "plug/dst-only.txt"],
    )
    assert (dst / "plug" / "held.txt").read_text() == "old"
    assert (dst / "plug" / "dst-only.txt").read_text() == "keep"
    assert (dst / "plug" / "free.txt").read_text() == "new-free"
    assert calls == ["free.txt"]
    out = capsys.readouterr().out
    assert "HELD UNVERIFIED: plug/held.txt" in out
    assert "HELD UNVERIFIED: plug/dst-only.txt" in out


def test_mirror_unheld_dst_only_file_still_deleted(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "plug" / "a.txt", "a")
    _write(dst / "plug" / "a.txt", "a")
    _write(dst / "plug" / "gone.txt", "x")
    _write(dst / "plug" / "held.txt", "h")
    ps.sync_mirror(
        src, dst, IgnoreMatcher([]), False, preserve_paths=["plug/held.txt"]
    )
    assert not (dst / "plug" / "gone.txt").exists()
    assert (dst / "plug" / "held.txt").exists()


def test_mirror_orphan_dir_with_held_descendant_retained(tmp_path, capsys):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "plug" / "a.txt", "a")
    _write(dst / "plug" / "a.txt", "a")
    _write(dst / "orph" / "held.txt", "h")
    ps.sync_mirror(
        src, dst, IgnoreMatcher([]), False, preserve_paths=["orph/held.txt"]
    )
    assert (dst / "orph" / "held.txt").read_text() == "h"
    assert "HELD UNVERIFIED: orph/" in capsys.readouterr().out


def test_mirror_top_level_sweep_spares_held_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "plug" / "a.txt", "a")
    _write(dst / "plug" / "a.txt", "a")
    _write(src / "top.txt", "t")
    _write(dst / "keep.txt", "k")
    _write(dst / "drop.txt", "d")
    ps.sync_mirror(
        src, dst, IgnoreMatcher([]), False,
        sweep_top_level_orphans=True, preserve_paths=["keep.txt"],
    )
    assert (dst / "keep.txt").exists()
    assert not (dst / "drop.txt").exists()


def test_default_preserve_paths_is_legacy_behaviour(tmp_path):
    src, dst = _flat(tmp_path)
    ps.sync_flat_mirror(src, dst, IgnoreMatcher([]), False)
    assert (dst / "held.txt").read_text() == "new-held"
    assert not (dst / "orphan-held.txt").exists()


@pytest.mark.parametrize(
    "bad",
    ["/abs", "a\\b", "../x", "a/../b", "", "a//b", "dir/", "x*", "CON", "a:b", "trail."],
)
def test_normalize_rejects_non_exact_file_paths(tmp_path, bad):
    with pytest.raises(ValueError):
        ps.normalize_preserve_paths(tmp_path, [bad])


def test_normalize_rejects_bare_string_and_directory_target(tmp_path):
    with pytest.raises(ValueError):
        ps.normalize_preserve_paths(tmp_path, "held.txt")
    (tmp_path / "d").mkdir()
    with pytest.raises(ValueError):
        ps.normalize_preserve_paths(tmp_path, ["d"])


def test_normalize_accepts_absent_file_and_empty(tmp_path):
    assert ps.normalize_preserve_paths(tmp_path, ["no/such.txt"]) == frozenset({"no/such.txt"})
    assert ps.normalize_preserve_paths(tmp_path) == frozenset()


def test_project_preserve_paths_strips_row_prefix(tmp_path):
    row = tmp_path / "coordinator" / "bin"
    row.mkdir(parents=True)
    got = ps.project_preserve_paths(row, tmp_path, ["coordinator/bin/x.py", "README.md"])
    assert got == frozenset({"x.py"})
    with pytest.raises(ValueError):
        ps.project_preserve_paths(tmp_path.parent, tmp_path, ["a"])
