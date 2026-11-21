"""Tests for the G-EM home absolute-path guard and its pre-commit hook."""

from __future__ import annotations

import subprocess

from coordinator_core.group_em import gem_home_path_guard as g
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import CompletedProcess

# Fixtures are assembled from fragments so no literal machine path sits in this source.
_DRIVE_BS = "C" + ":" + "\\" + "Users" + "\\" + "x" + "\\" + "a.md"
_DRIVE_FS = "e" + ":" + "/" + "proj/x"
_MAC = "/" + "Users" + "/bob/f"
_LINUX = "/" + "home" + "/al/f"
_NO_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_scan_flags_machine_paths_and_passes_clean(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text(f"see {_DRIVE_BS}\nalso {_DRIVE_FS}\nmac {_MAC}\nlinux {_LINUX}\n", encoding="utf-8")
    ok = tmp_path / "ok.md"
    ok.write_text("https://example.com/a and ~/home/x and $HOME/Users/y\nrel/path/here\n", encoding="utf-8")
    hits = g.scan([bad, ok])
    assert [h[1] for h in hits] == [1, 2, 3, 4]
    assert all(h[0] == str(bad) for h in hits)
    assert hits[0][2] == _DRIVE_BS
    assert g.scan([ok]) == []


def test_scan_skips_binary_and_missing(tmp_path: Path) -> None:
    b = tmp_path / "b.bin"
    b.write_bytes(_DRIVE_BS.encode() + b"\x00")
    assert g.scan([b, tmp_path / "nope"]) == []
