"""Tests for reconcile_merge_conclude: every refusal and the success commit,
against real throwaway repos under tmp_path."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import reconcile_manifest as rm
from coordinator_core.ops import reconcile_merge_conclude as conclude
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _g(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    _g(r, "config", "user.email", "t@t")
    _g(r, "config", "user.name", "t")
    _g(r, "config", "commit.gpgsign", "false")
    (r / "a.txt").write_text("base\n")
    (r / "b.txt").write_text("base\n")
    _g(r, "add", ".")
    _g(r, "commit", "-qm", "base")
    _g(r, "checkout", "-qb", "side")
    (r / "a.txt").write_text("side\n")
    (r / "c.txt").write_text("side-new\n")
    _g(r, "add", ".")
    _g(r, "commit", "-qm", "side")
    _g(r, "checkout", "-q", "main")
    (r / "a.txt").write_text("main\n")
    _g(r, "add", ".")
    _g(r, "commit", "-qm", "main")
    _g(r, "merge", "--no-ff", "side", "-m", "m", check=False)
    monkeypatch.chdir(r)
    monkeypatch.setattr(conclude, "show_toplevel", lambda: str(r))
    return r


def _open_manifest(repo, paths=("a.txt",)):
    head = rm.live_merge_head(repo)
    rm.write(repo, merge_head=head, branch="main", paths=paths)


def test_no_merge_refused(tmp_path, monkeypatch, capsys):
    r = tmp_path / "r"
    r.mkdir()
    _g(r, "init", "-q")
    monkeypatch.setattr(conclude, "show_toplevel", lambda: str(r))
    assert conclude.main([]) == 3
    assert rm.REFUSAL_NO_MERGE in capsys.readouterr().err


def test_missing_manifest_refused(repo, capsys):
    assert conclude.main([]) == 3
    assert rm.REFUSAL_MANIFEST_STALE in capsys.readouterr().err


def test_stale_manifest_refused(repo, capsys):
    rm.write(repo, merge_head="0" * 40, branch="main", paths=["a.txt"])
    assert conclude.main([]) == 3
    assert rm.REFUSAL_MANIFEST_STALE in capsys.readouterr().err


def test_markers_refused(repo, capsys):
    _open_manifest(repo)
    assert "<<<<<<<" in (repo / "a.txt").read_text()
    assert conclude.main([]) == 3
    err = capsys.readouterr().err
    assert rm.REFUSAL_MARKERS in err and "a.txt" in err
    assert rm.live_merge_head(repo) is not None


def test_unmerged_refused(repo, capsys):
    (repo / "a.txt").write_text("resolved\n")
    _open_manifest(repo, paths=("b.txt",))
    assert conclude.main([]) == 3
    err = capsys.readouterr().err
    assert rm.REFUSAL_UNMERGED in err and "a.txt" in err


def test_foreign_staged_refused(repo, capsys):
    (repo / "a.txt").write_text("resolved\n")
    (repo / "peer.txt").write_text("peer\n")
    _g(repo, "add", "peer.txt")
    _open_manifest(repo)
    assert conclude.main([]) == 3
    err = capsys.readouterr().err
    assert rm.REFUSAL_FOREIGN_STAGED in err and "peer.txt" in err
    assert rm.live_merge_head(repo) is not None


def test_machine_local_staged_refused(repo, capsys):
    (repo / "a.txt").write_text("resolved\n")
    share = repo / ".coordinator-local" / "subagent-share"
    share.mkdir(parents=True)
    (share / "x.md").write_text("telemetry\n")
    _g(repo, "add", "-f", ".coordinator-local/subagent-share/x.md")
    _open_manifest(repo)
    head = _g(repo, "rev-parse", "HEAD").stdout
    assert conclude.main([]) == 3
    err = capsys.readouterr().err
    assert rm.REFUSAL_MACHINE_LOCAL in err
    assert ".coordinator-local/subagent-share/x.md" in err
    assert _g(repo, "rev-parse", "HEAD").stdout == head
    assert rm.live_merge_head(repo) is not None


def test_success_commits_merge_and_removes_manifest(repo, capsys):
    (repo / "a.txt").write_text("resolved\n")
    _open_manifest(repo)
    assert conclude.main([]) == 0
    assert f"{rm.OUTCOME_CONCLUDED} branch=main" in capsys.readouterr().out
    parents = _g(repo, "rev-list", "--parents", "-n1", "HEAD").stdout.split()
    assert len(parents) == 3
    assert rm.live_merge_head(repo) is None
    assert not rm.manifest_path(repo).exists()
    assert (repo / "c.txt").read_text() == "side-new\n"
