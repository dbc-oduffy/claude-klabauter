"""Tests for backup-global-claude-md: backup, no-clobber, sentinel skip, empty skip."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backup_global_claude_md", Path(__file__).resolve().parents[1] / "backup-global-claude-md.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _setup(tmp_path: Path, text: str | None) -> Path:
    (tmp_path / ".claude").mkdir()
    if text is not None:
        (tmp_path / ".claude" / "CLAUDE.md").write_text(text, encoding="utf-8")
    return tmp_path


def _bak(home: Path) -> Path:
    return home / ".claude" / "CLAUDE.md.pre-coordinator.bak"


def test_backs_up_hand_authored(tmp_path):
    home = _setup(tmp_path, "mine\n")
    assert mod.backup(home) == "backed-up"
    assert _bak(home).read_text(encoding="utf-8") == "mine\n"


def test_no_clobber_existing_bak(tmp_path):
    home = _setup(tmp_path, "later edit\n")
    _bak(home).write_text("first\n", encoding="utf-8")
    assert mod.backup(home) == "skip-exists"
    assert _bak(home).read_text(encoding="utf-8") == "first\n"


def test_skips_when_sentinel_present(tmp_path):
    home = _setup(tmp_path, "x <!-- coordinator:claude-md-seed:v1 -->\n")
    assert mod.backup(home) == "skip-seeded"
    assert not _bak(home).exists()


def test_skips_empty_and_missing(tmp_path):
    home = _setup(tmp_path, "")
    assert mod.backup(home) == "skip-empty"
    assert not _bak(home).exists()
    (home / ".claude" / "CLAUDE.md").unlink()
    assert mod.backup(home) == "skip-no-file"


def test_main_prints_outcome(tmp_path, capsys):
    home = _setup(tmp_path, "mine\n")
    assert mod.main(["--claude-home", str(home)]) == 0
    assert capsys.readouterr().out.strip() == "backed-up"
