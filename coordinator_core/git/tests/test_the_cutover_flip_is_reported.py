"""The `commit_scoped` -> `commit_paths` cutover flips whose content lands,
and the flip is now visible in the outcome.

THE DEFECT, stated as a disagreement rather than a bug in either function.
For the same pathspec over the same tree, `commit_scoped` INFERS a deliberate
partial stage from divergence and commits the INDEX blob; `commit_paths`
commits the WORKTREE unless the caller declared `prefer_staged`. Both are
defensible and they answer oppositely, so moving a caller from one to the
other silently changes which bytes land. Nothing in either outcome said so.

WHY THE DEFAULT IS NOT WHAT CHANGED. `commit_paths` is right that intent must
be DECLARED: divergence does not identify a deliberate partial stage, it is
equally true of an ordinary unstaged edit, which is the common case and whose
worktree bytes are exactly what the caller means. Inferring is what committed
a stale index blob and left the worktree modified. Refusing undeclared
divergence would be the same error wearing a block instead of a guess -- it
would make the safe default unusable for the case it was designed for.

SO THE FIX IS A REPORT, NOT A REFUSAL, and `worktree_over_staged` is the half
that was missing. `staged_preferred` reported the DECLARED case, which is safe
by construction because the caller asked for it. The undeclared divergence is
the direction that can lose work, and it was the silent one -- the field
pointed the safe way.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "p")
    _git(tmp_path, "config", "user.email", "p@x")
    (tmp_path / "a.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _partial_stage(repo: pathlib.Path, name: str = "a.txt") -> None:
    (repo / name).write_text("staged\n", encoding="utf-8")
    _git(repo, "add", name)
    (repo / name).write_text("worktree\n", encoding="utf-8")


def _committed_bytes(repo: pathlib.Path, name: str = "a.txt") -> bytes:
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{name}"],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout


class TestTheFlipIsReported:
    def test_undeclared_divergence_commits_the_worktree_AND_says_so(self, repo):
        _partial_stage(repo)

        outcome = gcommit.commit_paths(repo, ["a.txt"], "undeclared")

        assert _committed_bytes(repo) == b"worktree\n"
        assert outcome.worktree_over_staged == ("a.txt",)
        assert outcome.staged_preferred == ()

    def test_declaring_the_stage_lands_the_stage_and_is_not_a_loss(self, repo):
        _partial_stage(repo)

        outcome = gcommit.commit_paths(
            repo, ["a.txt"], "declared", prefer_staged=["a.txt"]
        )

        assert _committed_bytes(repo) == b"staged\n"
        assert outcome.staged_preferred == ("a.txt",)
        assert outcome.worktree_over_staged == ()

    def test_an_ordinary_unstaged_edit_is_not_reported_as_a_loss(self, repo):
        (repo / "a.txt").write_text("just an edit\n", encoding="utf-8")

        outcome = gcommit.commit_paths(repo, ["a.txt"], "ordinary edit")

        assert _committed_bytes(repo) == b"just an edit\n"
        assert outcome.worktree_over_staged == ()

    def test_a_supplied_blob_is_the_callers_own_bytes_and_is_not_a_loss(self, repo):
        _partial_stage(repo)
        blob = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=b"supplied\n",
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode().strip()

        outcome = gcommit.commit_paths(
            repo, ["a.txt"], "supplied", supplied_blobs={"a.txt": blob}
        )

        assert _committed_bytes(repo) == b"supplied\n"
        assert outcome.worktree_over_staged == ()

    def test_only_the_diverged_path_is_named_not_the_whole_pathspec(self, repo):
        (repo / "b.txt").write_text("b seed\n", encoding="utf-8")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-qm", "b")
        _partial_stage(repo, "a.txt")
        (repo / "b.txt").write_text("b edited\n", encoding="utf-8")

        outcome = gcommit.commit_paths(repo, ["a.txt", "b.txt"], "mixed")

        assert outcome.worktree_over_staged == ("a.txt",)


class TestPreferDeliberateStagePolicy:

    def test_policy_on_partial_stage_lands_the_staged_bytes(self, repo):
        _partial_stage(repo)

        outcome = gcommit.commit_paths(
            repo, ["a.txt"], "policy on", prefer_deliberate_stage=True
        )

        assert _committed_bytes(repo) == b"staged\n"

    def test_policy_on_ordinary_edit_still_lands_worktree_bytes(self, repo):
        # THE CASE THAT PROVES THE DISCRIMINATOR: index still equals HEAD, so
        (repo / "a.txt").write_text("just an edit\n", encoding="utf-8")

        outcome = gcommit.commit_paths(
            repo, ["a.txt"], "policy on, ordinary edit", prefer_deliberate_stage=True
        )

        assert _committed_bytes(repo) == b"just an edit\n"

    def test_policy_off_partial_stage_is_unregressed(self, repo):
        _partial_stage(repo)

        outcome = gcommit.commit_paths(repo, ["a.txt"], "policy off")

        assert _committed_bytes(repo) == b"worktree\n"
        assert outcome.worktree_over_staged == ("a.txt",)

    def test_policy_on_preserved_path_is_not_reported_as_passed_over(self, repo):
        _partial_stage(repo)

        outcome = gcommit.commit_paths(
            repo, ["a.txt"], "policy on, not a loss", prefer_deliberate_stage=True
        )

        assert outcome.worktree_over_staged == ()
        assert outcome.staged_preferred == ("a.txt",)


class TestBothRoutesReportTheDisagreement:
    """The flip is between TWO functions, so a report on one side only moves
    the silence rather than ending it.

    `commit_scoped`'s private-index branch sets the WORKTREE aside and has
    reported it as `GitResult.worktree_excluded` -- plus a loud message on
    success -- since the 2026-08-10 bug-backlog row. `commit_paths` sets the
    STAGE aside and now reports `worktree_over_staged`. Neither function
    changed which bytes it commits: SC-DR-015 is why one preserves the stage,
    invariant 1 is why the other prefers the worktree, and both are ratified.
    What changed is that the disagreement is legible from either side.
    """

    def test_the_v2_route_warns_and_does_not_only_return_a_field(self, repo):
        from coordinator_core.ops.ceremony import commit_v2

        _partial_stage(repo)

        result = commit_v2._handler(
            {"paths": ["a.txt"], "message": "undeclared"},
            repo_root=repo / ".git",
        )

        assert result["committed"] is True
        assert result["worktree_over_staged"] == ["a.txt"]
        assert len(result["warnings"]) == 1
        assert "a.txt" in result["warnings"][0]
        assert "prefer_staged" in result["warnings"][0]

    def test_no_warning_when_nothing_was_set_aside(self, repo):
        from coordinator_core.ops.ceremony import commit_v2

        (repo / "a.txt").write_text("ordinary\n", encoding="utf-8")

        result = commit_v2._handler(
            {"paths": ["a.txt"], "message": "ordinary"},
            repo_root=repo / ".git",
        )

        assert result["committed"] is True
        assert result["warnings"] == []


class TestBlobFallbackLegAlsoReportsTheLoss:

    def test_a_refused_partial_staged_path_is_reported_via_the_fallback_leg(self, repo):
        (repo / ".gitattributes").write_text(
            "*.cmd text eol=crlf\n", encoding="utf-8", newline="\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "attrs")

        # Partial stage: one CR-bearing blob staged, a DIFFERENT CR-bearing
        (repo / "run.cmd").write_bytes(b"echo staged\r\n")
        _git(repo, "add", "run.cmd")
        (repo / "run.cmd").write_bytes(b"echo worktree\r\n")

        def fallback(paths):
            out = {}
            for rel in paths:
                result = subprocess.run(
                    ["git", "-C", str(repo), "hash-object", "-w", f"--path={rel}", "--", rel],
                    check=True,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                out[rel] = result.stdout.decode().strip()
            return out

        outcome = gcommit.commit_paths(
            repo, ["run.cmd"], "refused, partial staged", blob_fallback=fallback
        )

        assert _committed_bytes(repo, "run.cmd") == b"echo worktree\n"
        assert outcome.worktree_over_staged == ("run.cmd",)
