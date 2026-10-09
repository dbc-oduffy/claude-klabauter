"""A recently-touched stale branch is never deleted by the brief, and absorbing
it keeps it; `apply --help` lists every flag."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import consolidate_assemble as ca
from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

# Real git repos built by the _git/repo helpers; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOW = 1_800_000_000.0
FRESH = int(NOW - 600)
OLD = int(NOW - ca.RECENCY_FLOOR_SECONDS - 3600)


def _fake_git(tip_time: int, commits: str):
    rows = (
        f"refs/heads/current\tcurrent\tme@x\t\t{OLD}\n"
        f"refs/heads/main\tmain\tme@x\t\t{OLD}\n"
        f"refs/heads/work/stale\twork/stale\tme@x\t\t{tip_time}\n"
    )

    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        while args and args[0].startswith("-"):
            args = args[1:]
        out = ""
        rc = 0
        if args[:2] == ["config", "user.email"]:
            out = "me@x\n"
        elif args[:2] == ["rev-parse", "--abbrev-ref"]:
            out = "current\n"
        elif args[:2] == ["rev-parse", "--verify"]:
            rc = 0 if args[-1] == "main" else 1
        elif args[0] == "for-each-ref":
            out = rows
        elif args[0] == "log" and args[1].startswith("--format="):
            # unique_commits_by_ref's batched walk: one chain, tip first, parent next.
            lines = [ln for ln in commits.splitlines() if ln.strip()]
            shas = [f"{i:040x}" for i in range(1, len(lines) + 1)]
            parents = shas[1:] + [""]
            out = "".join(f"{s}\x1f{p}\x1f{ln}\n" for s, p, ln in zip(shas, parents, lines))
        elif args[0] == "rev-parse" and args[-1].endswith("^{commit}"):
            out = f"{1:040x}\n" * (len(args) - 1)
        elif args[0] == "worktree":
            out = "worktree /repo\nHEAD abc\nbranch refs/heads/current\n"
        elif args[0] == "merge-base":
            rc = 0
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    return run_git


def _brief(tip_time: int, commits: str) -> dict:
    return ca.brief(repo_root=Path("/repo"), run_git=_fake_git(tip_time, commits), now=NOW)


def _clis(obj: dict) -> list[str]:
    return [d["cli"] for d in obj["directives"]]


def test_old_zero_commit_branch_still_gets_a_delete() -> None:
    assert "delete-only" in _clis(_brief(OLD, ""))


def test_recent_zero_commit_branch_gets_no_delete() -> None:
    obj = _brief(FRESH, "")
    assert "delete-only" not in _clis(obj)
    stale = next(b for b in obj["gates"]["branches"] if b["name"] == "work/stale")
    assert stale["recent"] is True


def test_recent_branch_with_commits_absorbs_without_deleting() -> None:
    obj = _brief(FRESH, "abc123 one\n")
    clis = _clis(obj)
    assert "cherry-pick-only" in clis
    assert not {"delete-only", "cherry-pick-and-delete", "merge-and-delete"} & set(clis)
    jp = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-work/stale")
    by_value = {d["value"]: d["resolves"] for d in jp["dispositions"]}
    assert by_value == {"absorb": ["d-absorb-work/stale"], "skip": []}


def test_recent_branch_with_many_commits_uses_merge_only() -> None:
    commits = "".join(f"a{i} c\n" for i in range(ca._CHERRY_PICK_MAX_COMMITS + 1))
    assert "merge-only" in _clis(_brief(FRESH, commits))


def test_unknown_tip_time_applies_no_floor() -> None:
    assert ca.tip_times_from([("refs/heads/x", "x", "e", "", "")]) == {}


def test_old_branch_with_commits_keeps_the_delete_verbs() -> None:
    assert "cherry-pick-and-delete" in _clis(_brief(OLD, "abc123 one\n"))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/me")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "work/sibling")
    (root / "b.txt").write_text("sibling\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "sibling")
    _git(root, "checkout", "-q", "work/me")
    return root


@pytest.mark.spawns_process
@pytest.mark.parametrize("verb", ["cherry-pick-only", "merge-only"])
def test_absorb_only_verbs_land_the_work_and_keep_the_branch(repo: Path, verb: str) -> None:
    apply_mod._CLI_DISPATCH[verb](["work/sibling", "work/sibling"], repo)
    assert (repo / "b.txt").exists()
    assert "work/sibling" in _git(repo, "branch", "--list", "work/sibling")


def test_apply_help_lists_every_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert apply_mod.main_apply(["--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--session-id", "--decisions", "--decisions-file", "--help"):
        assert flag in out
