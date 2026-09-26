
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import session_commits
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo: Path, rel_path: str, content: str, *messages: str) -> str:
    _write(repo, rel_path, content)
    _git(["add", "--", rel_path], repo)
    args = ["commit", "-q"]
    for m in messages:
        args += ["-m", m]
    _git(args, repo)
    return _git(["rev-parse", "HEAD"], repo).strip()


def test_blank_session_id_raises(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed")
    with pytest.raises(ValueError):
        session_commits.resolve_session_commits(repo, "")


def test_no_matching_commits_returns_empty_list(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed", "Session-Id: sid-aaa")

    result = session_commits.resolve_session_commits(repo, "sid-zzz-no-match")

    assert result == []


def test_attributed_commit_returns_sha_subject_touched_paths_numstat(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed", "Session-Id: sid-other")
    sha = _commit(
        repo,
        "b.txt",
        "line1\nline2\n",
        "feat: add b",
        "Session-Id: sid-target",
    )

    result = session_commits.resolve_session_commits(repo, "sid-target")

    assert len(result) == 1
    row = result[0]
    assert row["sha"] == sha
    assert row["subject"] == "feat: add b"
    assert row["touched_paths"] == ["b.txt"]
    assert row["added"] == 2
    assert row["deleted"] == 0
    assert row["files"] == [
        {"path": "b.txt", "added": 2, "deleted": 0, "status": "A"}
    ]


def test_oldest_first_ordering(tmp_path):
    repo = _init_repo(tmp_path)
    first = _commit(repo, "a.txt", "1\n", "first", "Session-Id: sid-order")
    second = _commit(repo, "b.txt", "2\n", "second", "Session-Id: sid-order")

    result = session_commits.resolve_session_commits(repo, "sid-order")

    assert [row["sha"] for row in result] == [first, second]


def test_multi_session_id_commit_matches_once_not_duplicated(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(
        repo,
        "a.txt",
        "1\n",
        "fold: two sessions",
        "Session-Id: sid-fold-a\nSession-Id: sid-fold-b",
    )

    result_a = session_commits.resolve_session_commits(repo, "sid-fold-a")
    result_b = session_commits.resolve_session_commits(repo, "sid-fold-b")

    assert [row["sha"] for row in result_a] == [sha]
    assert [row["sha"] for row in result_b] == [sha]


def test_plumbing_commit_with_no_trailer_is_absent_not_error(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "1\n")
    _git(["add", "--", "a.txt"], repo)
    tree = _git(["write-tree"], repo).strip()
    plumbing_sha = _git(
        ["commit-tree", tree, "-m", "plumbing commit, no trailer"], repo
    ).strip()
    _git(["update-ref", "refs/heads/master", plumbing_sha], repo)
    _git(["checkout", "-q", "master"], repo)
    tagged_sha = _commit(repo, "b.txt", "2\n", "tagged", "Session-Id: sid-plumb")

    result = session_commits.resolve_session_commits(repo, "sid-plumb")

    shas = [row["sha"] for row in result]
    assert shas == [tagged_sha]
    assert plumbing_sha not in shas


def test_end_anchored_trailing_dollar_form_would_undercount(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(
        repo,
        "a.txt",
        "1\n",
        "feat: trailer not last line",
        "Session-Id: sid-notlast\nCo-Authored-By: Someone <someone@example.com>",
    )

    result = session_commits.resolve_session_commits(repo, "sid-notlast")

    assert [row["sha"] for row in result] == [sha]


def test_commit_range_narrows_the_walk(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "a.txt", "1\n", "before range", "Session-Id: sid-range")
    _commit(repo, "b.txt", "2\n", "after range", "Session-Id: sid-range")

    result = session_commits.resolve_session_commits(
        repo, "sid-range", commit_range=f"{base}..HEAD"
    )

    assert len(result) == 1
    assert result[0]["subject"] == "after range"


def test_sha_only_path_returns_sha_only_dicts(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed", "Session-Id: sid-other")
    sha = _commit(
        repo, "b.txt", "line1\nline2\n", "feat: add b", "Session-Id: sid-target"
    )

    result = session_commits.resolve_session_commits(
        repo, "sid-target", sha_only=True
    )

    assert result == [{"sha": sha}]


def test_sha_only_path_issues_no_raw_or_numstat_flags(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed", "Session-Id: sid-target")

    captured_args = []
    real_git = session_commits._git

    def _spy(args, cwd):
        captured_args.append(list(args))
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(session_commits, "_git", _spy)

    session_commits.resolve_session_commits(repo, "sid-target", sha_only=True)

    assert len(captured_args) == 1
    assert "--raw" not in captured_args[0]
    assert "--numstat" not in captured_args[0]
    assert any(
        a == "--grep=^Session-Id: sid-target" for a in captured_args[0]
    )


def test_default_return_is_byte_identical_with_sha_only_param_present(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "a.txt", "1\n", "seed", "Session-Id: sid-other")
    sha = _commit(
        repo, "b.txt", "line1\nline2\n", "feat: add b", "Session-Id: sid-target"
    )

    result = session_commits.resolve_session_commits(repo, "sid-target")

    assert result == [
        {
            "sha": sha,
            "subject": "feat: add b",
            "committer_epoch": result[0]["committer_epoch"],
            "touched_paths": ["b.txt"],
            "added": 2,
            "deleted": 0,
            "files": [
                {"path": "b.txt", "added": 2, "deleted": 0, "status": "A"}
            ],
        }
    ]


def test_body_line_quoting_a_trailer_is_a_documented_accepted_over_match(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(
        repo,
        "a.txt",
        "1\n",
        "chore: quoting another session",
        "Session-Id: sid-quoted",
    )

    result = session_commits.resolve_session_commits(repo, "sid-quoted")

    assert [row["sha"] for row in result] == [sha]


def test_anchored_and_unanchored_trailer_greps_agree(tmp_path):
    """The `$`-anchored trailer grep drops nothing the unanchored one keeps.

    `review_brightline_gate` greps `^Session-Id: <sid>$` where this op greps
    `^Session-Id: <sid>`, and the difference has been read as an under-count on
    the gate's side -- a commit whose trailer is followed by another trailer
    line silently vanishing from a review-scale measurement. It does not:
    `git log --grep` applies its regex LINE-WISE, so `$` is end-of-line, never
    end-of-message. This pins that, so nobody "fixes" the gate by dropping the
    anchor, and nobody reintroduces the claim into a docstring.
    """
    repo = _init_repo(tmp_path)
    sid = "863331b0-d278-5ae9-8d0f-9c0ab350de8c"
    sha = _commit(
        repo,
        "a.txt",
        "1\n",
        "subject",
        f"Session-Id: {sid}",
        "Deliverable-Id: dlv-something-abc123",
    )

    anchored = _git(
        ["log", "--no-merges", "--pretty=%H", f"--grep=^Session-Id: {sid}$", "HEAD"],
        repo,
    ).split()
    unanchored = _git(
        ["log", "--no-merges", "--pretty=%H", f"--grep=^Session-Id: {sid}", "HEAD"],
        repo,
    ).split()

    assert anchored == unanchored == [sha]
