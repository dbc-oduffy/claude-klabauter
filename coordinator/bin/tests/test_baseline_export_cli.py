"""baseline-export bin trampoline: delegation to the engine and the launcher triple."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI = _BIN_DIR / "baseline-export.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_launcher_triple_present():
    for suffix in (".py", ".cmd", ".ps1"):
        assert (_BIN_DIR / f"baseline-export{suffix}").is_file()


def test_usage_error_exits_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(_CLI)], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 2
    assert "baseline-export" in proc.stderr


def test_engine_unavailable_exits_3_with_result_line(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("baseline_export_cli", _CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(mod, "_load_main", _boom)
    assert mod.main(["--sha", "abc123", "--", "true"]) == 3
    out = capsys.readouterr()
    result = json.loads(out.out.strip().splitlines()[-1])["baseline_export"]
    assert result["sha"] == "abc123"
    assert result["export"] == "failed"
    assert result["runner_exit"] is None
    assert "engine unavailable" in out.err


def test_fixture_repo_result_line_and_cleanup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("hi\n")
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    sha = _git(repo, "rev-parse", "HEAD")

    proc = subprocess.run(
        [sys.executable, str(_CLI), "--sha", sha, "--", sys.executable, "-c", "import os; assert os.path.exists('f.txt')"],
        cwd=repo, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])["baseline_export"]
    assert result["sha"] == sha
    assert result["export"] == "ok"
    assert result["runner_exit"] == 0
    assert result["removed"] is True
    scratch = repo / "scratch"
    assert not scratch.exists() or not list(scratch.glob("test-base-*"))
