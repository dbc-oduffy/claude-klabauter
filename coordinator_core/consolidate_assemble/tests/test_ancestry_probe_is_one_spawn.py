"""Ancestry probes in `brief` and `_refuse_unmerged_tips` cost a constant number of spawns."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import consolidate_assemble as ca
from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


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
    _git(root, "checkout", "-q", "-b", "work/side")
    (root / "b.txt").write_text("side\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "side")
    _git(root, "checkout", "-q", "work/me")
    return root


def _counting(calls: list[list[str]]):
    def run_git(args, cwd, input=None):
        calls.append(list(args))
        return ca.default_run_git(args, cwd, input=input)

    return run_git


def test_ancestors_of_head_real_git(repo):
    ancestor = _git(repo, "rev-parse", "HEAD")
    short = ancestor[:10]
    non_ancestor = _git(repo, "rev-parse", "work/side")
    got = ca._ancestors_of_head(
        ca.default_run_git,
        repo,
        [ancestor, short, non_ancestor, "", "-x", "deadbeef" * 5, "no-such-ref"],
    )
    assert got == {ancestor, short}


def test_ancestors_of_head_spawns_do_not_grow_with_shas(repo):
    ancestor = _git(repo, "rev-parse", "HEAD")
    non_ancestor = _git(repo, "rev-parse", "work/side")
    counts = []
    for shas in ([ancestor], [ancestor, non_ancestor, "bogus", "", "-x", ancestor[:8], "cafe" * 10]):
        calls: list[list[str]] = []
        ca._ancestors_of_head(_counting(calls), repo, shas)
        counts.append(len(calls))
    assert counts[0] == counts[1] == 2


def test_refuse_unmerged_tips_spawns_do_not_grow_with_tips(repo):
    _git(repo, "branch", "feat-merged")
    _git(repo, "update-ref", "refs/remotes/origin/feat-merged", "work/side")

    seen: list[list[str]] = []
    real = apply_mod._run_git

    def counting(args, cwd):
        seen.append(list(args))
        return real(args, cwd)

    orig = apply_mod._run_git
    apply_mod._run_git = counting
    try:
        one = apply_mod._refuse_unmerged_tips("feat-merged", False, repo)
        n_one = len(seen)
        seen.clear()
        both = apply_mod._refuse_unmerged_tips("feat-merged", True, repo)
        n_both = len(seen)
    finally:
        apply_mod._run_git = orig

    assert one is None
    assert n_one == n_both == 2
    assert both is not None and "origin/feat-merged" in both and "is not an ancestor of HEAD" in both
    assert "heads/feat-merged" not in both


def test_brief_superseded_probe_is_flat(repo):
    """N superseded branches cost the same ancestry spawns as one."""
    ancestor = _git(repo, "rev-parse", "HEAD")
    for i in range(5):
        _git(repo, "branch", f"stale-{i}", "work/side")

    def spawns(superseded):
        calls: list[list[str]] = []
        ca.brief(repo_root=repo, my_email="t@local", run_git=_counting(calls), superseded=superseded, now=1e12)
        return sum(1 for c in calls if c[0] in ("cat-file", "rev-list") or c[0] == "merge-base")

    assert spawns({"stale-0": ancestor}) == spawns({f"stale-{i}": ancestor for i in range(5)}) == 2
