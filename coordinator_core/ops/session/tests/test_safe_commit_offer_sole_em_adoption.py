"""`session.safe_commit_offer` adopts a sole live EM's unclaimed dirt in tracked path classes.

Every test drives `_handler` over a tmp_path git fixture with the live set pinned, so the
decision under test is the sole-EM gate and the tracked-class classifier, not liveness itself.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "sole-em"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout


def _fixture(tmp_path, monkeypatch, live):
    repo = seeded_repo(tmp_path, readme="x")
    (repo / "state" / "warp" / "old-run").mkdir(parents=True)
    (repo / "state" / "warp" / "old-run" / "a.md").write_text("old")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed tracked class")
    core.init(SID, cwd=str(repo))
    monkeypatch.setattr(safe_commit_offer, "live_session_ids", lambda cwd=None: frozenset(live))
    (repo / "state" / "warp" / "new-run").mkdir(parents=True)
    (repo / "state" / "warp" / "new-run" / "x.md").write_text("new")
    (repo / "scratchy").mkdir()
    (repo / "scratchy" / "deep").mkdir()
    (repo / "scratchy" / "deep" / "junk.log").write_text("junk")
    return repo


def _tracked(repo):
    return set(_git(repo, "ls-files").split())


def test_sole_em_commits_tracked_class_dirt_at_default_params(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID})

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert "error" not in out
    assert "state/warp/new-run/x.md" in _tracked(repo)
    assert "state/warp/new-run/x.md" not in out["reconciliation"]["unclaimed"]
    assert out["sole_em"] is True
    assert out["adoption_groups"] == []
    assert out["outcome"]["status"] == "committed"


def test_sole_em_never_commits_a_class_without_a_tracked_sibling(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID})

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert out["gitignore_candidates"] == ["scratchy/deep"]
    assert "scratchy/deep/junk.log" not in _tracked(repo)
    assert out["sole_em_residue"] == ["scratchy/deep/junk.log"]
    assert "Gitignore candidates" in out["rendered"]
    assert "Sole-EM residue" in out["rendered"]


def test_second_live_session_commits_nothing_and_returns_passable_groups(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID, "peer"})

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert out["sole_em"] is False
    assert "state/warp/new-run/x.md" not in _tracked(repo)
    assert out["sole_em_residue"] == []
    groups = out["adoption_groups"]
    assert [g["paths"] for g in groups] == [["state/warp/new-run/x.md"]]
    assert "Ready-made groups" in out["rendered"]

    again = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo), "groups": groups})

    assert "state/warp/new-run/x.md" in _tracked(repo)
    assert again["outcome"]["status"] == "committed"
    assert again["outcome"]["dropped_paths"] == []
    assert "scratchy/deep/junk.log" not in _tracked(repo)


def test_passed_back_group_cannot_carry_a_gitignore_candidate(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID, "peer"})
    smuggled = [{"paths": ["scratchy/deep/junk.log"], "message": "smuggle"}]

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo), "groups": smuggled})

    assert "scratchy/deep/junk.log" not in _tracked(repo)
    assert out["outcome"]["dropped_paths"] == ["scratchy/deep/junk.log"]


@pytest.mark.parametrize("live", [set(), {"someone-else"}])
def test_empty_or_foreign_live_set_never_adopts(tmp_path, monkeypatch, live):
    repo = _fixture(tmp_path, monkeypatch, live)

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert out["sole_em"] is False
    assert "state/warp/new-run/x.md" not in _tracked(repo)
    assert out["sole_em_residue"] == []


def test_liveness_error_never_adopts(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID})

    def _boom(cwd=None):
        raise RuntimeError("liveness down")

    monkeypatch.setattr(safe_commit_offer, "live_session_ids", _boom)

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert out["sole_em"] is False
    assert "state/warp/new-run/x.md" not in _tracked(repo)


def test_dry_run_reports_adoption_without_committing(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID})

    out = safe_commit_offer._handler({"session_id": SID, "cwd": str(repo), "dry_run": True})

    assert out["sole_em"] is True
    assert [g["paths"] for g in out["adoption_groups"]] == [["state/warp/new-run/x.md"]]
    assert out["gitignore_candidates"] == ["scratchy/deep"]
    assert "will be adopted" in out["rendered"]
    assert "state/warp/new-run/x.md" not in _tracked(repo)


def test_clean_unclaimed_set_spawns_no_ls_files(tmp_path, monkeypatch):
    repo = seeded_repo(tmp_path, readme="x")
    core.init(SID, cwd=str(repo))
    monkeypatch.setattr(safe_commit_offer, "live_session_ids", lambda cwd=None: frozenset({SID}))
    seen = []
    real = safe_commit_offer.run_git

    def _spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(safe_commit_offer, "run_git", _spy)

    safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert not [a for a in seen if "ls-files" in a]


def test_dirty_unclaimed_set_adds_exactly_one_ls_files_spawn(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID, "peer"})
    seen = []
    real = safe_commit_offer.run_git

    def _spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(safe_commit_offer, "run_git", _spy)

    safe_commit_offer._handler({"session_id": SID, "cwd": str(repo), "dry_run": True})

    assert len([a for a in seen if "ls-files" in a]) == 1


def test_real_call_adds_exactly_one_ls_files_spawn(tmp_path, monkeypatch):
    repo = _fixture(tmp_path, monkeypatch, {SID})
    seen = []
    real = safe_commit_offer.run_git

    def _spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(safe_commit_offer, "run_git", _spy)

    safe_commit_offer._handler({"session_id": SID, "cwd": str(repo)})

    assert len([a for a in seen if "ls-files" in a]) == 1
