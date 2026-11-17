"""Pins for the claude-author installer trampoline and launcher (F19, F24, F27).

--help writes nothing; on Windows the installer renders a BOM-prefixed .ps1 and
a .cmd launcher; a nested interactive launch (CLAUDECODE set) prints exactly one
advisory stderr line and still launches; headless/informational calls print none.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN = Path(__file__).resolve().parent.parent
_BOM = b"\xef\xbb\xbf"


def _load(name: str):
    spec = importlib.util.spec_from_file_location("_t_" + name.replace("-", "_").replace(".", "_"), _BIN / name)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_BIN))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(_BIN))
    return mod


def _env(tmp_path: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_AUTHOR_") and k != "CLAUDECODE"}
    env["CLAUDE_HOME"] = str(tmp_path)
    env.update(extra)
    return env


def test_installer_help_prints_usage_and_writes_nothing(tmp_path: Path) -> None:
    for flag in ("--help", "-h"):
        proc = subprocess.run(
            [sys.executable, str(_BIN / "install-claude-author-wrapper.py"), flag],
            capture_output=True, text=True, env=_env(tmp_path), timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Usage: install-claude-author-wrapper.py" in proc.stdout
        assert list(tmp_path.iterdir()) == []


def test_launcher_template_staging_prefixes_ps1_bom(tmp_path: Path) -> None:
    mod = _load("gen-claude-author-launcher.py")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "claude-author-launcher.cmd.tmpl").write_bytes(b"@echo off\r\nREM \xe2\x80\x94\r\n")
    (src / "claude-author-launcher.ps1.tmpl").write_bytes("# — launcher\r\n".encode("utf-8"))
    mod._stage_bom_templates(str(src), str(dst))
    assert (dst / "claude-author-launcher.ps1.tmpl").read_bytes().startswith(_BOM)
    assert (dst / "claude-author-launcher.cmd.tmpl").read_bytes() == (src / "claude-author-launcher.cmd.tmpl").read_bytes()
    # Already-BOM input is not double-prefixed.
    (src / "claude-author-launcher.ps1.tmpl").write_bytes(_BOM + b"# x\r\n")
    mod._stage_bom_templates(str(src), str(dst))
    assert (dst / "claude-author-launcher.ps1.tmpl").read_bytes() == _BOM + b"# x\r\n"


@pytest.mark.skipif(os.name != "nt", reason="launcher rendering is Windows-only")
def test_installer_renders_bom_ps1_and_cmd_on_windows(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_BIN / "install-claude-author-wrapper.py"), "--no-smoke"],
        capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    local_bin = tmp_path / ".local" / "bin"
    assert (local_bin / "claude-author").is_file()
    assert (local_bin / "claude-author.cmd").is_file()
    assert (local_bin / "claude-author.ps1").read_bytes().startswith(_BOM)


def _fake_claude_launch_env(tmp_path: Path, **extra: str) -> dict:
    doe = tmp_path / "fake-doe"
    (doe / "coordinator").mkdir(parents=True)
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "claude-ran"
    (fake_bin / "claude.py").write_text(f'open(r"{marker}", "w").write("ran")\n', encoding="utf-8")
    (fake_bin / "claude.cmd").write_text(f'@"{sys.executable}" "%~dp0claude.py" %*\r\n', encoding="ascii")
    (fake_bin / "claude").write_text(f'#!/usr/bin/env python3\nopen(r"{marker}", "w").write("ran")\n', encoding="utf-8")
    (fake_bin / "claude").chmod(0o755)
    env = _env(
        tmp_path,
        HOME=str(fake_home),
        USERPROFILE=str(fake_home),
        PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        REPO_DOE_CLAUDE=str(doe),
        **extra,
    )
    return env


_ADVISORY = "claude-author: running inside a Claude Code session (CLAUDECODE=1)"


def test_nested_launch_prints_one_advisory_line_and_still_launches(tmp_path: Path) -> None:
    env = _fake_claude_launch_env(tmp_path, CLAUDECODE="1")
    proc = subprocess.run(
        [sys.executable, str(_BIN / "claude-author.py")], capture_output=True, text=True, env=env, timeout=60,
    )
    assert len([ln for ln in proc.stderr.splitlines() if _ADVISORY in ln]) == 1
    assert "refused" not in proc.stderr


def test_nested_vanilla_launch_prints_one_advisory_line(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_BIN / "claude-author.py"), "--vanilla"],
        capture_output=True, text=True, env=_env(tmp_path, CLAUDECODE="1"), timeout=60,
    )
    assert len([ln for ln in proc.stderr.splitlines() if _ADVISORY in ln]) == 1
    assert "refused" not in proc.stderr


def test_launch_outside_claude_code_prints_no_advisory(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_BIN / "claude-author.py"), "--vanilla"],
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    assert _ADVISORY not in proc.stderr


def test_nested_advisory_exempts_headless_and_informational(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load("claude-author.py")
    for k in ("CLAUDE_AUTHOR_DRY_RUN", "CLAUDE_AUTHOR_PRINT_PLUGIN_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert mod._nested_interactive_launch([]) is True
    assert mod._nested_interactive_launch(["--resume"]) is True
    for exempt in (["-p", "hi"], ["--print", "hi"], ["--version"], ["--dry-run"], ["--print-plugin-dir"]):
        assert mod._nested_interactive_launch(exempt) is False
    monkeypatch.delenv("CLAUDECODE")
    assert mod._nested_interactive_launch([]) is False
