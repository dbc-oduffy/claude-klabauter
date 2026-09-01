"""The brief enumerates refs ONCE, and that enumeration does not invent a
branch out of a remote's symbolic HEAD.

`brief()` asks two questions of the ref set — which branches exist, and who
authored each tip — and used to spawn a separate `git branch -a` for the first
alongside the `git for-each-ref` that already answered the second. On this repo
`branch -a` measured 284 ms of the op's 931 ms process time: the single most
expensive call in a brief, buying a re-listing of refs already in hand.

The consolidation has a trap the old parse dodged by accident and a naive
rewrite walks straight into. `git branch -a` renders a remote's symbolic HEAD
as `remotes/origin/HEAD -> origin/main`, which the old parse skipped on the
`->`. `for-each-ref` has no arrow: `refs/remotes/origin/HEAD` arrives with the
short name `origin`. Read as a branch it becomes a phantom named `origin`,
categorized `mine-stale`, dragging a `git log` and a `git show --stat` behind
it — two extra spawns and a fabricated row in the operator's branch report,
proposed for deletion.

Spec backlink: CLAUDE.md § The brightline ("git justifies itself per use").
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca


def _rows_stdout(rows: list[tuple[str, str, str]]) -> str:
    return "".join(f"{refname}\t{short}\t{email}\n" for refname, short, email in rows)


_REF_ROWS = [
    ("refs/heads/current", "current", "me@x"),
    ("refs/heads/main", "main", "me@x"),
    ("refs/remotes/origin/HEAD", "origin", "me@x"),
    ("refs/remotes/origin/main", "origin/main", "me@x"),
    ("refs/remotes/origin/work/peer", "origin/work/peer", "peer@x"),
]


def _fake_git(calls: list[list[str]]):
    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        while args and args[0].startswith("-"):
            args = args[1:]
        calls.append(list(args))
        if args[:2] == ["config", "user.email"]:
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
        if args[:2] == ["rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0 if args[-1] == "main" else 1, stdout="", stderr="")
        if args[0] == "for-each-ref":
            return SimpleNamespace(returncode=0, stdout=_rows_stdout(_REF_ROWS), stderr="")
        if args[0] == "worktree":
            return SimpleNamespace(
                returncode=0,
                stdout="worktree /repo\nHEAD abc\nbranch refs/heads/current\n",
                stderr="",
            )
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run_git


def test_brief_enumerates_refs_with_a_single_spawn() -> None:
    calls: list[list[str]] = []
    ca.brief(repo_root=Path("/repo"), run_git=_fake_git(calls))

    assert [c for c in calls if c[0] == "for-each-ref"].__len__() == 1
    # The spawn this consolidation deleted. `git branch --merged <target>` is
    # a different call with a different job (reachability, batched) and stays.
    assert ["branch", "-a"] not in calls


def test_a_remotes_symbolic_head_is_not_a_branch() -> None:
    calls: list[list[str]] = []
    decision_object = ca.brief(repo_root=Path("/repo"), run_git=_fake_git(calls))

    names = {b["name"] for b in decision_object["gates"]["branches"]}
    assert "origin" not in names, "refs/remotes/origin/HEAD is an alias, not a branch"
    assert names == {"current", "main", "work/peer"}
    # The phantom's real cost: it categorized as stale work and pulled a
    # unique-commit walk and a `git show` after it.
    assert not [c for c in calls if c[:2] == ["log", "--oneline"]]


def test_local_and_remote_flags_come_from_the_full_refname() -> None:
    """A local branch may legitimately be named `origin/<something>`. Deciding
    local-vs-remote from the SHORT name would misfile it as remote-tracking and
    then resolve its `ref` to a nonexistent `origin/origin/<something>`."""
    rows = [
        ("refs/heads/origin/local-trap", "origin/local-trap", "me@x"),
        ("refs/remotes/origin/real-remote", "origin/real-remote", "me@x"),
    ]
    entries = {e["name"]: e for e in ca.list_branches_from(rows)}

    assert entries["origin/local-trap"]["is_local"] is True
    assert entries["origin/local-trap"]["is_remote"] is False
    assert entries["origin/local-trap"]["ref"] == "origin/local-trap"

    assert entries["real-remote"]["is_remote"] is True
    assert entries["real-remote"]["is_local"] is False
    assert entries["real-remote"]["ref"] == "origin/real-remote"


def test_a_ref_with_no_author_email_still_parses() -> None:
    """`%(authoremail:trim)` can come back empty. Tab-delimited, that is a
    present-but-empty third field; space-delimited it was indistinguishable
    from a missing one."""
    rows = ca.list_branches_from([("refs/heads/orphan", "orphan", "")])
    assert [r["name"] for r in rows] == ["orphan"]
