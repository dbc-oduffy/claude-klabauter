"""Unit tests for atlas_freshness: plan_repair, apply_repair and the acted ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography.atlas_freshness import RepairPlan, apply_repair, read_acted

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# File index

## Directory → system

### cartography — atlas tooling

`coordinator_core/cartography/`
"""

_PAGE = "docs/architecture/systems/cartography.md"
_PAGE_TEXT = "---\nsystem: cartography\nfiles: 7\nlines: 10\n---\n\n# cartography\n\nprose 7 files\n"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _atlas(root: Path) -> None:
    _write(root, "docs/architecture/file-index.md", _FILE_INDEX)
    _write(root, _PAGE, _PAGE_TEXT)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _tracked_atlas(root: Path) -> None:
    _atlas(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")


def test_apply_repair_rewrites_only_files_line(tmp_path):
    _tracked_atlas(tmp_path)
    written = apply_repair(tmp_path, [RepairPlan(_PAGE, 2)])
    assert written == [_PAGE]
    assert (tmp_path / _PAGE).read_text(encoding="utf-8") == _PAGE_TEXT.replace("files: 7", "files: 9")


def test_apply_repair_preserves_crlf(tmp_path):
    _atlas(tmp_path)
    (tmp_path / _PAGE).write_bytes(_PAGE_TEXT.replace("\n", "\r\n").encode())
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "core.autocrlf=false", "add", "-A")
    apply_repair(tmp_path, [RepairPlan(_PAGE, -1)])
    assert (tmp_path / _PAGE).read_bytes() == _PAGE_TEXT.replace("files: 7", "files: 6").replace("\n", "\r\n").encode()


def test_apply_repair_skips_dirty_page_and_records_page_dirty(tmp_path):
    _tracked_atlas(tmp_path)
    peer_edit = _PAGE_TEXT + "peer's uncommitted prose\n"
    (tmp_path / _PAGE).write_text(peer_edit, encoding="utf-8")
    assert apply_repair(tmp_path, [RepairPlan(_PAGE, 1)]) == []
    assert (tmp_path / _PAGE).read_text(encoding="utf-8") == peer_edit
    fact = read_acted(tmp_path)[_PAGE]
    assert fact.kind == "page_dirty"


def test_apply_repair_never_goes_negative(tmp_path):
    _tracked_atlas(tmp_path)
    apply_repair(tmp_path, [RepairPlan(_PAGE, -50)])
    assert "files: 0\n" in (tmp_path / _PAGE).read_text(encoding="utf-8")


def test_apply_repair_does_not_spawn(tmp_path, monkeypatch):
    _tracked_atlas(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert apply_repair(tmp_path, [RepairPlan(_PAGE, 1)]) == [_PAGE]
