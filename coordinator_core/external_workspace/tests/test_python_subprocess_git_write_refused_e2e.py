"""End to end: with the installer's include applied, a Python-subprocess git write into a
registered external workspace is refused; the rehydrate op's registered handler is the only
route that clones, and its bulk form lists and clones nothing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.external_workspace.vcs_guard_contract import TOKEN_ENV
from coordinator_core.install import external_workspace_git_guard as installer
from coordinator_core.ipc import get_op_handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _run(args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def _git(args, cwd=None):
    proc = _run(args, cwd)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Guarded tmp registry: `missing` and `other` registered at absent paths, url a local repo."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))

    src = tmp_path / "src"
    _git(["init", "-q", "-b", "main", str(src)])
    (src / "a.txt").write_text("one", encoding="utf-8")
    _git(["add", "a.txt"], cwd=src)
    _git([*_IDENT, "commit", "-q", "-m", "one"], cwd=src)
    first = _git(["rev-parse", "HEAD"], cwd=src)
    (src / "a.txt").write_text("two", encoding="utf-8")
    _git([*_IDENT, "commit", "-q", "-am", "two"], cwd=src)

    missing = tmp_path / "ws" / "missing"
    other = tmp_path / "ws" / "other"
    (reg / "registry.local.toml").write_text(
        "[external_workspaces.missing]\n"
        f"path = '{missing.as_posix()}'\nurl = '{src.as_posix()}'\nref = '{first}'\n"
        "[external_workspaces.other]\n"
        f"path = '{other.as_posix()}'\nurl = '{src.as_posix()}'\n",
        encoding="utf-8",
    )
    installer.apply()
    return src, first, missing, other


def _refs(repo: Path) -> str:
    if not (repo / ".git").exists():
        return ""
    return _run(["for-each-ref"], cwd=repo).stdout.strip()


def test_subprocess_clone_is_refused_then_op_clones_then_commit_is_refused(world):
    src, first, missing, _other = world

    refused = _run(["clone", "--", str(src), str(missing)])
    assert refused.returncode != 0
    assert "external_workspace.rehydrate" in (refused.stderr + refused.stdout)
    assert _refs(missing) == ""

    handler = get_op_handler("external_workspace.rehydrate")
    out = handler({"key": "missing"})
    assert out["action"] == "cloned"
    assert _git(["rev-parse", "HEAD"], cwd=missing) == first
    assert (missing / "a.txt").read_text(encoding="utf-8") == "one"

    (missing / "a.txt").write_text("edit", encoding="utf-8")
    _git(["add", "a.txt"], cwd=missing)
    commit = _run([*_IDENT, "commit", "-m", "x"], cwd=missing)
    assert commit.returncode != 0
    assert "external_workspace.rehydrate" in (commit.stderr + commit.stdout)
    assert _git(["rev-parse", "HEAD"], cwd=missing) == first


def test_all_missing_lists_every_missing_key_and_clones_none(world):
    _src, _first, missing, other = world

    out = get_op_handler("external_workspace.rehydrate")({"all_missing": True})

    assert {row["key"] for row in out["missing"]} == {"missing", "other"}
    assert not missing.exists()
    assert not other.exists()
