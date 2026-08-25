"""
coordinator_core.ops.tests.test_session_commits

Tests for the `session.commits` op (session_commits.py) — see that module's
docstring for the anchoring-form decision (`^Session-Id: <sid>`, no trailing
`$`) this test suite locks in.

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo, and never this repo's own live history
(peer sessions commit to it concurrently, which would make an assertion
against live history flaky by construction).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import session_commits

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
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
    """A fold can tag one commit with more than one session's Session-Id
    trailer — this must appear exactly once in this session's result, not
    once per trailer line."""
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
    """A plumbing commit (git commit-tree, bypassing porcelain commit) carries
    no Session-Id trailer at all — it must be silently absent from the
    result, never raise and never appear."""
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "1\n")
    _git(["add", "--", "a.txt"], repo)
    tree = _git(["write-tree"], repo).strip()
    plumbing_sha = _git(
        ["commit-tree", tree, "-m", "plumbing commit, no trailer"], repo
    ).strip()
    _git(["update-ref", "refs/heads/master", plumbing_sha], repo)
    _git(["checkout", "-q", "master"], repo)
    # A normal, attributed commit on top, so the branch has a session-tagged commit too.
    tagged_sha = _commit(repo, "b.txt", "2\n", "tagged", "Session-Id: sid-plumb")

    result = session_commits.resolve_session_commits(repo, "sid-plumb")

    shas = [row["sha"] for row in result]
    assert shas == [tagged_sha]
    assert plumbing_sha not in shas


def test_end_anchored_trailing_dollar_form_would_undercount(tmp_path):
    """Locks in the anchoring-form decision: a Session-Id trailer NOT on the
    message's final line (followed by Co-Authored-By) still matches this
    op's `^Session-Id: <sid>` (no trailing $) form — the anchored-both-ends
    form documented elsewhere as a KNOWN under-count must not creep back in
    here."""
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


def test_body_line_quoting_a_trailer_is_a_documented_accepted_over_match(tmp_path):
    """Matches this module's documented over-match acceptance: a body line
    that happens to quote another session's trailer verbatim also matches
    (the safe direction, per this op's docstring)."""
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
