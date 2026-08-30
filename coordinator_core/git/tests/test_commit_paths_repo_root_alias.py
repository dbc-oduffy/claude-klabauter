"""The dispatched committer's documented call shape reaches `commit_paths`.

`agents/git-commit-agent.md`'s VERBATIM block and
`snippets/scoped-commit-route.md`'s parameter list -- both in a tree claude-klabauter
does not own -- name the repo argument `repo_root`, and `emit.py` uses that
name for the same value throughout. A `TypeError` on that keyword is not read
by the dispatched committer as a wrong keyword; it is read as leg 1 being
unavailable, and the agent drops to the plain `git commit` fallback that the
subagent commit guard denies -- halting a whole emitted workflow at the commit
phase that gates its next wave. These tests pin the alias, the deletion-only
call shape the same docs declare legal, and the refusals that stay refusals.
"""

import subprocess
import pytest

from coordinator_core.git import commit as gcommit

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/z")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    (r / "gone.txt").write_text("gone\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _subjects(repo):
    return _git(repo, "log", "--format=%s").stdout.splitlines()


def test_the_agents_verbatim_block_lands_a_commit(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    (repo / "gone.txt").unlink()

    gcommit.commit_paths(
        repo_root=repo,
        paths=["a.txt"],
        deleted_paths=["gone.txt"],
        message="verbatim block\n\nbody",
    )

    assert _subjects(repo)[0] == "verbatim block"
    assert _git(repo, "show", "--name-status", "--format=", "HEAD").stdout.split() == [
        "A",
        "a.txt",
        "D",
        "gone.txt",
    ]


def test_a_deletion_only_call_reaches_git_not_a_missing_argument_error(repo):
    (repo / "gone.txt").unlink()

    gcommit.commit_paths(repo_root=repo, deleted_paths=["gone.txt"], message="drop it")

    assert _subjects(repo)[0] == "drop it"


def test_repo_positional_still_wins_and_the_alias_stays_optional(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")

    gcommit.commit_paths(repo, ["a.txt"], "positional")

    assert _subjects(repo)[0] == "positional"


def test_supplying_both_names_for_the_same_value_is_refused(repo):
    with pytest.raises(TypeError, match="both `repo` and its alias"):
        gcommit.commit_paths(repo, ["seed.txt"], "two roots", repo_root=repo)


def test_omitting_the_root_entirely_names_all_three_spellings(repo):
    with pytest.raises(TypeError, match="repo_root"):
        gcommit.commit_paths(paths=["seed.txt"], message="no root")


def test_an_empty_pathspec_is_still_refused_not_defaulted(repo):
    with pytest.raises(gcommit.CommitRefused, match="empty pathspec"):
        gcommit.commit_paths(repo_root=repo, message="nothing named")
