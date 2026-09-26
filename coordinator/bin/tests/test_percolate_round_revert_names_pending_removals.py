from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_revert_warning", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True, **_NO_WINDOW)


def _dest_with(files: dict, tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    dest.mkdir()
    _git(["git", "init", "-q"], dest)
    for rel, content in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["git", "add", "-A"], dest)
    _git(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "seed"],
        dest,
    )
    return dest


def test_clean_dest_produces_no_warning(tmp_path):
    dest = _dest_with({"a.md": "a\n"}, tmp_path)
    assert _mod._pending_removal_warning(str(dest)) == ""


def test_pending_removals_are_counted_and_named(tmp_path):
    dest = _dest_with({"a.md": "a\n", "b.md": "b\n", "c.md": "c\n"}, tmp_path)
    (dest / "b.md").unlink()
    (dest / "c.md").unlink()

    warning = _mod._pending_removal_warning(str(dest))
    assert "2 pending removal(s)" in warning
    assert "ONLY record" in warning


def test_modified_and_untracked_files_are_not_counted(tmp_path):
    dest = _dest_with({"a.md": "a\n", "b.md": "b\n"}, tmp_path)
    (dest / "a.md").write_text("changed\n", encoding="utf-8")
    (dest / "new.md").write_text("new\n", encoding="utf-8")

    assert _mod._pending_removal_warning(str(dest)) == ""

    (dest / "b.md").unlink()
    assert "1 pending removal(s)" in _mod._pending_removal_warning(str(dest))


def test_staged_deletion_is_counted(tmp_path):
    dest = _dest_with({"a.md": "a\n", "b.md": "b\n"}, tmp_path)
    (dest / "b.md").unlink()
    _git(["git", "add", "-A"], dest)

    warning = _mod._pending_removal_warning(str(dest))
    assert "1 pending removal(s)" in warning
    assert "ONLY record" in warning


def test_unreadable_dest_says_nothing(tmp_path):
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()
    assert _mod._pending_removal_warning(str(not_a_repo)) == ""
