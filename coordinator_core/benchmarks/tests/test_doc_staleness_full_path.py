"""Unit tests of the doc-staleness fixture driver's own logic: no git spawn, no fixture build."""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.benchmarks import doc_staleness_full_path as drv

_COMMIT = re.compile(r"^commit (refs/heads/\w+)\nmark :(\d+)\n", re.M)


@pytest.fixture(scope="module")
def commits() -> dict[int, dict]:
    stream = drv._history_stream(drv.HISTORY_COMMITS)
    marks = list(_COMMIT.finditer(stream))
    out: dict[int, dict] = {}
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(stream)
        chunk = stream[m.end():end]
        parent = re.search(r"^from :(\d+)$", chunk, re.M)
        merge = re.search(r"^merge :(\d+)$", chunk, re.M)
        out[int(m.group(2))] = {
            "ref": m.group(1),
            "from": int(parent.group(1)) if parent else None,
            "merge": int(merge.group(1)) if merge else None,
            "chunk": chunk,
        }
    return out


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_history_has_every_commit(commits):
    assert sorted(commits) == list(range(1, drv.HISTORY_COMMITS + 1))


def test_merge_ratio_matches_real_repo(commits):
    merges = sum(1 for c in commits.values() if c["merge"] is not None)
    target = 0.0094 * drv.HISTORY_COMMITS
    assert abs(merges - target) <= 0.10 * target


def test_long_branch_forks_before_readme_touch_and_merges_after(commits):
    touch = drv._README_TOUCH
    long_marks = [n for n, c in commits.items() if c["ref"] == "refs/heads/long"]
    assert sorted(long_marks) == list(drv._LONG_BRANCH)
    assert max(long_marks) < touch
    merged = [n for n, c in commits.items() if c["merge"] in long_marks]
    assert merged == [drv._LONG_MERGE]
    assert drv._LONG_MERGE > touch


def test_readme_authored_touch_sits_back_from_head(commits):
    touch = [
        n for n, c in commits.items()
        if "inline README.md" in c["chunk"] and "Revised line" in c["chunk"] and n > 1
    ]
    assert touch[0] == drv._README_TOUCH
    assert drv.HISTORY_COMMITS - drv._README_TOUCH == drv.README_COMMITS_BACK


def test_install_rename_and_sweep_present(commits):
    assert "R docs/old-install.md INSTALL.md" in commits[drv._INSTALL_TOUCH + 1]["chunk"]
    sweep = commits[drv._INSTALL_TOUCH + 2]["chunk"]
    assert sweep.count("inline src/sweep") == drv.SWEEP_FILES
    assert "inline INSTALL.md" in sweep


def _report(**readme) -> dict:
    entry = {"path": "README.md", "status": "ok", "stale": True,
             "commits_since": drv.README_EXPECTED_COMMITS_SINCE}
    entry.update(readme)
    return {"docs": [entry, {"path": "INSTALL.md", "status": "ok"}]}


def test_check_report_accepts_fixture_shape():
    assert drv.check_report(_report()) is None


@pytest.mark.parametrize(
    "report",
    [
        _report(stale=False),
        _report(commits_since=drv.README_EXPECTED_COMMITS_SINCE + 1),
        {"docs": [{"path": "INSTALL.md", "status": "ok"}]},
    ],
    ids=["not-stale", "off-by-one", "missing-readme"],
)
def test_check_report_names_readme_on_mutation(report):
    assert "README.md" in drv.check_report(report)


@pytest.mark.parametrize("argv", [[], ["--bogus", "dir"], ["a", "b"]], ids=["no-dir", "unknown-flag", "two-dirs"])
def test_main_wrong_argv_returns_2(argv, monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    assert drv.main(argv) == 2
