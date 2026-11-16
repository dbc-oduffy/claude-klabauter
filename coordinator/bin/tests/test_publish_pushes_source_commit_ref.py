"""A publish pushes the source commit it stamps to the owned origin as refs/heads/publish/<sha>
before any mirror commit exists. Real git, a tmp_path bare repo as origin."""
from __future__ import annotations

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("publish_pushes_source_ref_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load()


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def source(tmp_path: Path):
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "a"], repo)
    return repo, bare, _git(["rev-parse", "HEAD"], repo).strip()


def _union(sha: str, top: Path, signature: str = "sig"):
    return SimpleNamespace(plan=SimpleNamespace(head=sha, toplevel=top, signature=signature))


def test_stamped_source_commit_lands_on_origin_as_a_publish_ref(source, monkeypatch) -> None:
    repo, bare, sha = source
    monkeypatch.setattr(publish, "_REPO_ROOT", repo)

    refusal = publish._push_stamped_source_commits([_union(sha, repo), _union(sha, repo)])

    assert refusal is None
    assert _git(["ls-remote", str(bare), f"refs/heads/publish/{sha}"], repo).split()[0] == sha


def test_a_failed_push_refuses_naming_the_ref_and_a_runnable_push(source, monkeypatch, tmp_path) -> None:
    repo, bare, sha = source
    monkeypatch.setattr(publish, "_REPO_ROOT", repo)
    _git(["remote", "set-url", "origin", str(tmp_path / "missing.git")], repo)

    refusal = publish._push_stamped_source_commits([_union(sha, repo)])

    assert refusal is not None
    assert f"source commit {sha[:12]} could not be pushed to origin as refs/heads/publish/{sha[:12]}" in refusal
    assert f"git push origin {sha}:refs/heads/publish/{sha}" in refusal
    assert _git(["ls-remote", str(bare)], repo).strip() == ""


def test_a_foreign_toplevel_or_unsigned_root_is_not_pushed(source, monkeypatch, tmp_path) -> None:
    repo, bare, sha = source
    monkeypatch.setattr(publish, "_REPO_ROOT", repo)

    refusal = publish._push_stamped_source_commits(
        [_union(sha, tmp_path / "elsewhere"), _union(sha, repo, signature=""), _union("short", repo)]
    )

    assert refusal is None
    assert _git(["ls-remote", str(bare)], repo).strip() == ""


def test_the_push_runs_before_any_landing_in_the_round() -> None:
    body = inspect.getsource(publish._run_round)

    assert body.index("_push_stamped_source_commits(") < body.index("land_diff(\n")
