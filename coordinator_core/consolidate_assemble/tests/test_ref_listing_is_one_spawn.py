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
    assert ["branch", "-a"] not in calls


def test_a_remotes_symbolic_head_is_not_a_branch() -> None:
    calls: list[list[str]] = []
    decision_object = ca.brief(repo_root=Path("/repo"), run_git=_fake_git(calls))

    names = {b["name"] for b in decision_object["gates"]["branches"]}
    assert "origin" not in names, "refs/remotes/origin/HEAD is an alias, not a branch"
    assert names == {"current", "main", "work/peer"}
    assert not [c for c in calls if c[:2] == ["log", "--oneline"]]


def test_local_and_remote_flags_come_from_the_full_refname() -> None:
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
    rows = ca.list_branches_from([("refs/heads/orphan", "orphan", "")])
    assert [r["name"] for r in rows] == ["orphan"]
