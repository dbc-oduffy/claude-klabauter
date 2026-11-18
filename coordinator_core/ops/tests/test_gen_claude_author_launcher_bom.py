"""The launcher op writes the `.ps1` as utf-8-sig and the render parses under PS 5.1."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import gen_claude_author_launcher as op
from coordinator_core.ops import verify_ps51_clean

_BOM = b"\xef\xbb\xbf"
_CMD = "claude-author-launcher.cmd.tmpl"
_PS1 = "claude-author-launcher.ps1.tmpl"


def _templates(tmp_path: Path, ps1: bytes) -> Path:
    tmpl_dir = tmp_path / "tmpl"
    tmpl_dir.mkdir()
    (tmpl_dir / _CMD).write_bytes(b"@echo off\r\nREM \xe2\x80\x94 x\r\n")
    (tmpl_dir / _PS1).write_bytes(ps1)
    return tmpl_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    return home


def _render(tmpl_dir: Path) -> int:
    return op.main(["--template-dir", str(tmpl_dir), "--no-smoke"])


def test_bomless_template_renders_with_bom(tmp_path, home):
    ps1 = "# — launcher\r\nWrite-Output 'x'\r\n".encode("utf-8")
    assert _render(_templates(tmp_path, ps1)) == 0
    out = (home / ".local" / "bin" / "claude-author.ps1").read_bytes()
    assert out == _BOM + ps1


def test_bom_template_is_not_double_prefixed(tmp_path, home):
    ps1 = _BOM + b"# x\r\n"
    assert _render(_templates(tmp_path, ps1)) == 0
    assert (home / ".local" / "bin" / "claude-author.ps1").read_bytes() == ps1


def test_cmd_launcher_stays_byte_identical_to_template(tmp_path, home):
    tmpl_dir = _templates(tmp_path, b"# x\r\n")
    assert _render(tmpl_dir) == 0
    assert (home / ".local" / "bin" / "claude-author.cmd").read_bytes() == (tmpl_dir / _CMD).read_bytes()


def test_check_only_reports_bom_render_up_to_date(tmp_path, home, capsys):
    tmpl_dir = _templates(tmp_path, b"# x\r\n")
    assert _render(tmpl_dir) == 0
    capsys.readouterr()
    assert op.main(["--template-dir", str(tmpl_dir), "--check-only"]) == 0
    err = capsys.readouterr().err
    assert "ps1: up to date" in err


def test_op_render_matches_bin_staged_render(tmp_path, home):
    """The bin twin stages a BOM template; the op must not add a second BOM."""
    bin_dir = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
    sys.path.insert(0, str(bin_dir))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_gcal", bin_dir / "gen-claude-author-launcher.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(bin_dir))
    raw = _templates(tmp_path, "# — x\r\n".encode("utf-8"))
    staged = tmp_path / "staged"
    staged.mkdir()
    mod._stage_bom_templates(str(raw), str(staged))
    assert _render(raw) == 0
    direct = (home / ".local" / "bin" / "claude-author.ps1").read_bytes()
    assert _render(staged) == 0
    assert (home / ".local" / "bin" / "claude-author.ps1").read_bytes() == direct
    assert direct.startswith(_BOM) and not direct.startswith(_BOM + _BOM)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 parse oracle")
def test_rendered_ps1_parses_under_ps51(tmp_path, home):
    ps1 = "# — launcher\r\n$x = 'café'\r\nWrite-Output $x\r\n".encode("utf-8")
    assert _render(_templates(tmp_path, ps1)) == 0
    dest = home / ".local" / "bin" / "claude-author.ps1"
    exe = verify_ps51_clean.find_ps51_exe()
    if exe is None:
        pytest.skip("powershell.exe not on PATH")
    assert verify_ps51_clean.verdict_for_file(str(dest), exe) == "OK"
    assert os.path.getsize(dest) > 3
