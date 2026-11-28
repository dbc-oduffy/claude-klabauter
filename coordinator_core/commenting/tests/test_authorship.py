"""agent_added_comments attributes added comments by trailer, in at most two git spawns."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.commenting import authorship
from coordinator_core.commenting.added_types import AddedComment


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Human Name", "-c", "user.email=h@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_bytes(content.encode())
    _git(root, "add", name)
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def fake_comments(path: str, before: str, after: str) -> list[AddedComment]:
    old = {ln.strip() for ln in before.splitlines()}
    out = []
    for i, ln in enumerate(after.splitlines(), 1):
        if ln.strip().startswith("#") and ln.strip() not in old:
            out.append(AddedComment(path=path, line=i, text=ln.strip().lstrip("# "), kind="comment"))
    return out


class CountingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, stdin):
        self.calls.append(list(args))
        return authorship.default_runner(args, cwd, stdin)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, "a.py", "x = 1\n", "init")
    return tmp_path, base


def test_trailerless_commit_with_agent_looking_author_yields_nothing(tmp_path):
    root, base = _repo(tmp_path)
    _git(root, "config", "user.name", "Claude Agent")
    _commit(root, "a.py", "x = 1\n# explain\n", "human edit")
    run = CountingRunner()
    assert authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=fake_comments) == []
    assert len(run.calls) == 1


def test_session_id_commit_yields_its_added_comments_in_two_spawns(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\n# agent note\n", "work\n\nSession-Id: 44f01aec-12bd-42ea-b5a1-be3bda0fa2e5")
    run = CountingRunner()
    got = authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=fake_comments)
    assert [(c.path, c.line, c.text) for c in got] == [("a.py", 2, "agent note")]
    assert len(run.calls) == 2


def test_engine_session_id_is_not_an_agent(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\n# note\n", "land\n\nSession-Id: claude-klabauter-engine")
    assert authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments) == []


def test_co_authored_by_agent_address_is_an_agent(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\n# note\n", "w\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    got = authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments)
    assert [c.text for c in got] == ["note"]


def test_human_co_author_is_not_an_agent(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\n# note\n", "w\n\nCo-Authored-By: Pat <pat@example.com>")
    assert authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments) == []


def test_human_comment_in_same_file_as_agent_commit_is_not_attributed(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\n# human note\n", "human")
    _commit(root, "a.py", "x = 1\n# human note\ny = 2\n# agent note\n", "agent\n\nSession-Id: abc-123")
    got = authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments)
    assert [c.text for c in got] == ["agent note"]


def test_spawn_count_is_flat_across_many_commits_and_files(tmp_path):
    root, base = _repo(tmp_path)
    for i in range(6):
        _commit(root, f"f{i}.py", f"# c{i}\n", f"m{i}\n\nSession-Id: sid-{i}")
    run = CountingRunner()
    got = authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=fake_comments)
    assert len(got) == 6
    assert len(run.calls) == 2


def test_crlf_blobs_and_non_comment_suffix(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "a.py", "x = 1\r\n# crlf note\r\n", "w\n\nSession-Id: s1")
    _commit(root, "notes.txt", "# not source\n", "w\n\nSession-Id: s1")
    got = authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments)
    assert [c.path for c in got] == ["a.py"]


def test_paths_limit_the_scan_to_the_named_files(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "own.py", "# own note\n", "w\n\nSession-Id: s1")
    _commit(root, "peer.py", "# peer note\n", "w\n\nSession-Id: s2")
    seen: list[str] = []

    def spy(path, before, after):
        seen.append(path)
        return fake_comments(path, before, after)

    run = CountingRunner()
    got = authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=spy, paths=["own.py"])
    assert [c.path for c in got] == ["own.py"]
    assert seen == ["own.py"]
    assert len(run.calls) == 2
    assert not any("peer.py" in a for a in run.calls[0])
    unscoped = authorship.agent_added_comments(root, base, "HEAD", comments_fn=fake_comments)
    assert sorted(c.path for c in unscoped) == ["own.py", "peer.py"]


def test_empty_or_non_code_paths_spawn_nothing(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, "own.py", "# own note\n", "w\n\nSession-Id: s1")
    run = CountingRunner()
    assert authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=fake_comments, paths=[]) == []
    assert authorship.agent_added_comments(root, base, "HEAD", runner=run, comments_fn=fake_comments, paths=["x.txt"]) == []
    assert run.calls == []


def test_git_failure_returns_empty(tmp_path):
    assert authorship.agent_added_comments(tmp_path, "nope", "HEAD", comments_fn=fake_comments) == []
