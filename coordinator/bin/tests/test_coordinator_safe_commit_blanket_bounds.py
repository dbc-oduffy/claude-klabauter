"""`--blanket` refuses an oversize untracked set before staging, and kills an overrunning add."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _staged(repo):
    out = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True)
    return out.stdout.split()


def test_oversize_untracked_set_is_refused_before_anything_is_staged(repo, monkeypatch, capsys):
    mod = _load_cli_module()
    (repo / "data").mkdir()
    (repo / "data" / "big.bin").write_bytes(b"x" * 4096)
    (repo / "note.md").write_text("n", encoding="utf-8")
    monkeypatch.setattr(mod, "_BLANKET_MAX_UNTRACKED_BYTES", 1024)
    with pytest.raises(SystemExit) as exc:
        mod._git_add_all_blanket()
    assert exc.value.code == 1
    assert "data" in capsys.readouterr().err
    assert _staged(repo) == []


def test_ignored_trees_do_not_count_toward_the_ceiling(repo, monkeypatch):
    mod = _load_cli_module()
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "big.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(mod, "_BLANKET_MAX_UNTRACKED_BYTES", 1024)
    mod._git_add_all_blanket()
    assert _staged(repo) == [".gitignore"]


def test_an_overrunning_add_is_killed_and_refused(repo, monkeypatch, capsys):
    mod = _load_cli_module()
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["git", "add", "-A"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        mod._git_add_all_blanket()
    assert exc.value.code == 1
    assert "killed" in capsys.readouterr().err
