"""compose_daily_summary_gap: archive-shape summary for a day with no file, from all refs."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.daily_summary_gap import GapRefusal, compose_daily_summary_gap

pytestmark = pytest.mark.complete

DAY = "2026-01-15"


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def _commit(repo: Path, name: str, msg: str, when: str) -> str:
    (repo / name).write_text(msg + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _topo_tip(root: str, shas: "list[str]") -> "str | None":
    if len(shas) == 1:
        return shas[0]
    lead = _git(Path(root), "rev-list", "--topo-order", *shas).splitlines()[0]
    ancestors = set(_git(Path(root), "rev-list", lead).splitlines())
    return lead if all(s in ancestors for s in shas) else None


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "config", "commit.gpgsign", "false")
    _commit(r, "root.txt", "init", "2026-01-01T12:00:00")
    _git(r, "branch", "-M", "main")
    return r


def test_side_branch_only_commit_is_composed_with_rollup_frontmatter(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "work/m1/side")
    tip = _commit(repo, "a.txt", "side work", f"{DAY}T10:00:00")
    _git(repo, "checkout", "-q", "main")

    path, text = compose_daily_summary_gap(repo, DAY, "m1", _topo_tip, today="2026-01-20")

    assert path == repo / "archive" / "daily-summaries" / f"{DAY}-m1.md"
    assert not path.exists()
    for line in (
        "rollup_schema: 1", "grain: day", f"period: {DAY}", "machine: m1",
        "commits: 1", f"covered_tip_sha: {tip}", "covered_machine: m1", "backfilled: true",
    ):
        assert f"\n{line}\n" in text, line
    assert text.startswith("---\n")
    assert f"# Daily Summary — {DAY}" in text
    assert "## Work Completed\n- side work (" in text
    assert text.count(f"\ncovered_tip_sha: {tip}\n") == 2


def test_lists_every_commit_and_tip_is_the_descendant(repo: Path) -> None:
    first = _commit(repo, "a.txt", "first", f"{DAY}T09:00:00")
    second = _commit(repo, "b.txt", "second", f"{DAY}T11:00:00")
    _commit(repo, "c.txt", "other day", "2026-01-16T09:00:00")

    _, text = compose_daily_summary_gap(repo, DAY, "m1", _topo_tip)

    assert "commits: 2" in text
    assert f"covered_tip_sha: {second}" in text
    assert "- first (" in text and "- second (" in text and "other day" not in text
    assert first[:10] in text


def test_refuses_when_a_summary_for_the_day_exists(repo: Path) -> None:
    _commit(repo, "a.txt", "work", f"{DAY}T09:00:00")
    ds = repo / "archive" / "daily-summaries"
    ds.mkdir(parents=True)
    (ds / f"{DAY}-other.md").write_text("x\n")

    with pytest.raises(GapRefusal, match="already exists"):
        compose_daily_summary_gap(repo, DAY, "m1", _topo_tip)


def test_refuses_a_day_with_no_commits(repo: Path) -> None:
    with pytest.raises(GapRefusal, match="no commits"):
        compose_daily_summary_gap(repo, DAY, "m1", _topo_tip)


def test_refuses_diverged_tips(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "left")
    _commit(repo, "l.txt", "left", f"{DAY}T09:00:00")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "right")
    _commit(repo, "r.txt", "right", f"{DAY}T10:00:00")

    with pytest.raises(GapRefusal, match="diverged"):
        compose_daily_summary_gap(repo, DAY, "m1", _topo_tip)


@pytest.mark.parametrize("bad", ["2026-1-5", "2026-02-30", ""])
def test_refuses_a_malformed_date(repo: Path, bad: str) -> None:
    with pytest.raises(GapRefusal):
        compose_daily_summary_gap(repo, bad, "m1", _topo_tip)
