"""The prep gate's CENSUS class warns on a census operand git does not track, never changing status."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap.prep_gate import _census

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
pytestmark =[pytest.mark.spawns_process, pytest.mark.cadence]


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=_NO_WINDOW)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x\n")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "n.txt").write_text("y\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "z.txt").write_text("z\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "docs/a.md", ".gitignore"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    return tmp_path


def _entry(command: str) -> dict:
    return {"question": "q", "command": command, "result": "1", "count": 1}


def test_an_ignored_dir_operand_warns_and_status_stays_pass(tmp_path):
    root = _repo(tmp_path)
    out = _census({"census": [_entry("grep -c z ignored/z.txt")]}, root)
    assert out["status"] == "PASS"
    assert out["warnings"] == [
        "[0] census operand ignored/z.txt is not tracked: this entry resolves UNDECIDABLE "
        "at fire time; count over a tracked path"
    ]


def test_a_tracked_and_an_absent_operand_do_not_warn(tmp_path):
    root = _repo(tmp_path)
    out = _census(
        {"census": [_entry("grep -c x docs/a.md"), _entry("grep -c x docs"), _entry("ls -1 nope")]},
        root,
    )
    assert out["status"] == "PASS" and out["warnings"] == []


def test_a_live_state_root_operand_warns_even_when_it_exists_untracked(tmp_path):
    root = _repo(tmp_path)
    out = _census({"census": [_entry("ls -1 scratch")]}, root)
    assert out["status"] == "PASS" and len(out["warnings"]) == 1
    assert "scratch" in out["warnings"][0]
