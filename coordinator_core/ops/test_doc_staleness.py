"""Requirement-level contract for the doc-staleness read.

A registered human-facing doc is stale when `commits_since >= C AND days_since >= D`,
both measured from its newest genuine authored touch. A touch is not genuine when it
is a sweep drive-by (`files_touched > K AND lines_changed_in_doc < L`), whitespace-only
or link-only. A rename counts as a touch. Evidence fields and the spawn ceiling are
asserted at the end of the module.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import doc_staleness
from coordinator_core.ops.doc_staleness import (
    _has_commit_graph,
    build_doc_staleness_report,
    build_doc_staleness_report_from_registry,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

THRESHOLD_COMMITS = 5
THRESHOLD_DAYS = 21

_DOC_LINES = [f"Line {i} of the guide." for i in range(1, 21)]
_LINK_LINE = "See [the guide](docs/old.md) for more."


def _git(repo: Path, *args: str, when: datetime | None = None) -> None:
    env = dict(os.environ)
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        env=env,
        **no_console_creationflags(),
    )


def _at(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _commit(repo: Path, files: dict[str, str], days_ago: int, msg: str = "c") -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg, when=_at(days_ago))


def _fillers(repo: Path, n: int, days_ago: int) -> None:
    for i in range(n):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"filler {i}", when=_at(days_ago))


def _doc(extra: list[str] | None = None, link: str = _LINK_LINE) -> str:
    return "\n".join([*_DOC_LINES, link, *(extra or [])]) + "\n"


def _repo_with_old_touch(tmp_path: Path, touch_days_ago: int = 60) -> Path:
    repo = _init(tmp_path / "repo")
    _commit(repo, {"README.md": _doc()}, touch_days_ago, "authored")
    return repo


def _report(repo: Path, doc: str = "README.md", **kw) -> dict:
    out = build_doc_staleness_report(
        str(repo), [doc], THRESHOLD_COMMITS, THRESHOLD_DAYS, **kw
    )
    assert len(out["docs"]) == 1
    entry = out["docs"][0]
    assert entry["path"] == doc
    return entry


def test_and_rule_fires_when_both_legs_pass(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    entry = _report(repo)
    assert entry["status"] == "ok"
    assert entry["stale"] is True


def test_commits_alone_do_not_fire(tmp_path):
    repo = _repo_with_old_touch(tmp_path, touch_days_ago=5)
    _fillers(repo, THRESHOLD_COMMITS + 5, 1)
    entry = _report(repo)
    assert entry["status"] == "ok"
    assert entry["stale"] is False


def test_days_alone_do_not_fire(tmp_path):
    repo = _repo_with_old_touch(tmp_path, touch_days_ago=90)
    _fillers(repo, 2, 30)
    entry = _report(repo)
    assert entry["status"] == "ok"
    assert entry["stale"] is False


def test_recent_touch_in_a_busy_week_does_not_fire(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    _commit(repo, {"README.md": _doc(["A fresh paragraph."])}, 1, "refresh")
    _fillers(repo, THRESHOLD_COMMITS + 5, 0)
    entry = _report(repo)
    assert entry["stale"] is False


def _stale_after(repo: Path) -> bool:
    return _report(repo)["stale"]


def test_sweep_drive_by_does_not_reset_the_clock(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    sweep = {f"src/f{i}.txt": "x\n" for i in range(10)}
    sweep["README.md"] = _doc(["Swept line."])
    _commit(repo, sweep, 1, "sweep")
    assert _stale_after(repo) is True


def test_whitespace_only_touch_does_not_reset_the_clock(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    spaced = _doc().replace("Line 3 of the guide.", "Line 3 of the guide.   ")
    spaced += "\n\n"
    _commit(repo, {"README.md": spaced}, 1, "whitespace")
    assert _stale_after(repo) is True


def test_link_only_touch_does_not_reset_the_clock(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    _commit(
        repo,
        {"README.md": _doc(link="See [the guide](docs/new.md) for more.")},
        1,
        "relink",
    )
    assert _stale_after(repo) is True


@pytest.mark.parametrize(
    ("files_touched", "lines_changed", "resets_clock"),
    [
        (10, 1, True),
        (11, 1, False),
        (11, 14, False),
        (11, 15, True),
        (10, 14, True),
    ],
    ids=["files==K", "files==K+1", "lines==L-1", "lines==L", "files==K,lines==L-1"],
)
def test_sweep_boundaries(tmp_path, files_touched, lines_changed, resets_clock):
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    files = {f"src/f{i}.txt": "x\n" for i in range(files_touched - 1)}
    files["README.md"] = _doc([f"Added note {i}." for i in range(lines_changed)])
    _commit(repo, files, 1, "boundary")
    _fillers(repo, THRESHOLD_COMMITS + 1, 0)
    assert _stale_after(repo) is (not resets_clock)


def test_rename_counts_as_a_touch(tmp_path):
    repo = _init(tmp_path / "repo")
    _commit(repo, {"OLD.md": _doc()}, 60, "authored")
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    _git(repo, "mv", "OLD.md", "README.md")
    _git(repo, "commit", "-q", "-m", "rename", when=_at(1))
    entry = _report(repo)
    assert entry["status"] == "ok"
    assert entry["stale"] is False


def test_doc_with_no_content_modifying_history(tmp_path):
    repo = _init(tmp_path / "repo")
    files = {f"src/f{i}.txt": "x\n" for i in range(10)}
    files["README.md"] = _doc()[:60] + "\n"
    _commit(repo, files, 60, "only a sweep")
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    entry = _report(repo)
    assert entry["status"] == "no_content_modifying_history"
    assert entry["stale"] is False


def test_absent_doc_reports_absent(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    entry = _report(repo, doc="CONTEXT.md")
    assert entry["status"] == "absent"
    assert entry["stale"] is False


def test_empty_doc_list_gives_empty_report(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    out = build_doc_staleness_report(
        str(repo), [], THRESHOLD_COMMITS, THRESHOLD_DAYS
    )
    assert out == {"docs": []}


def test_failed_git_read_degrades_the_doc_and_does_not_raise(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "README.md").write_text(_doc(), encoding="utf-8")
    entry = _report(not_a_repo)
    assert entry["stale"] is False
    assert entry["status"] == "no_content_modifying_history"

    missing = build_doc_staleness_report(
        str(tmp_path / "does-not-exist"), ["README.md"], THRESHOLD_COMMITS, THRESHOLD_DAYS
    )
    assert [d["path"] for d in missing["docs"]] == ["README.md"]
    assert missing["docs"][0]["stale"] is False


def _write_registry(repo: Path, docs: list[str], commits: int, days: int) -> None:
    (repo / "coordinator.local.md").write_text(
        "---\n"
        f"human_facing_docs: [{', '.join(docs)}]\n"
        f"doc_staleness_commits: {commits}\n"
        f"doc_staleness_days: {days}\n"
        "---\n",
        encoding="utf-8",
    )


def test_from_registry_reads_docs_and_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_staleness, "_has_commit_graph", lambda _root: True)
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    _write_registry(repo, ["README.md", "CONTEXT.md"], THRESHOLD_COMMITS, THRESHOLD_DAYS)
    out = build_doc_staleness_report_from_registry(str(repo))
    by_path = {d["path"]: d for d in out["docs"]}
    assert set(by_path) == {"README.md", "CONTEXT.md"}
    assert by_path["README.md"]["stale"] is True
    assert by_path["CONTEXT.md"]["status"] == "absent"


def test_from_registry_defaults_do_not_flag_a_small_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_staleness, "_has_commit_graph", lambda _root: True)
    repo = _repo_with_old_touch(tmp_path)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    out = build_doc_staleness_report_from_registry(str(repo))
    by_path = {d["path"]: d for d in out["docs"]}
    assert by_path["README.md"]["status"] == "ok"
    assert by_path["README.md"]["stale"] is False


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_ok_entry_carries_the_evidence_fields(tmp_path):
    repo = _repo_with_old_touch(tmp_path, touch_days_ago=60)
    touch_sha = _head(repo)
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    entry = _report(repo)
    assert entry["commits_since"] == THRESHOLD_COMMITS + 1
    assert entry["days_since"] == 60
    assert entry["last_touch_sha"] == touch_sha
    assert entry["last_touch_date"].startswith(_at(60).strftime("%Y-%m-%d"))
    assert entry["changed_areas"] == []
    assert entry["threshold_commits"] == THRESHOLD_COMMITS
    assert entry["threshold_days"] == THRESHOLD_DAYS


def test_absent_entry_carries_only_path_status_stale(tmp_path):
    repo = _repo_with_old_touch(tmp_path)
    assert _report(repo, doc="CONTEXT.md") == {
        "path": "CONTEXT.md",
        "status": "absent",
        "stale": False,
    }


def test_today_moves_days_since_and_flips_stale(tmp_path):
    repo = _repo_with_old_touch(tmp_path, touch_days_ago=10)
    _fillers(repo, THRESHOLD_COMMITS + 1, 5)
    touch_day = _at(10).date()
    early = _report(repo, today=touch_day + timedelta(days=THRESHOLD_DAYS - 1))
    late = _report(repo, today=touch_day + timedelta(days=THRESHOLD_DAYS))
    assert (early["days_since"], early["stale"]) == (THRESHOLD_DAYS - 1, False)
    assert (late["days_since"], late["stale"]) == (THRESHOLD_DAYS, True)


def test_spawn_count_does_not_grow_with_the_touched_doc_count(tmp_path, monkeypatch):
    repo = _init(tmp_path / "repo")
    docs = [f"DOC{i}.md" for i in range(5)]
    for i, doc in enumerate(docs):
        _commit(repo, {doc: _doc([f"Doc {i}."])}, 60 - i, f"doc {i}")
    _fillers(repo, THRESHOLD_COMMITS + 1, 30)
    spawns: list[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    def spawned_for(names: list[str]) -> int:
        spawns.clear()
        out = build_doc_staleness_report(str(repo), names, THRESHOLD_COMMITS, THRESHOLD_DAYS)
        assert all(d["stale"] for d in out["docs"])
        return len(spawns)

    two = spawned_for(docs[:2])
    five = spawned_for(docs)
    assert 0 < two == five


def test_batched_count_equals_per_doc_rev_list_count_across_a_merged_side_branch(tmp_path):
    repo = _init(tmp_path / "repo")
    _commit(repo, {"README.md": _doc()}, 90, "readme")
    readme_sha = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _fillers(repo, 3, 80)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"INSTALL.md": _doc(["Install."])}, 70, "install after the fork")
    install_sha = _head(repo)
    _commit(repo, {"CONTEXT.md": _doc(["Context."])}, 65, "context")
    context_sha = _head(repo)
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=_at(60))
    _fillers(repo, 2, 40)
    docs = ["README.md", "INSTALL.md", "CONTEXT.md"]
    touch_shas = {"README.md": readme_sha, "INSTALL.md": install_sha, "CONTEXT.md": context_sha}
    out = build_doc_staleness_report(str(repo), docs, THRESHOLD_COMMITS, THRESHOLD_DAYS)
    for entry in out["docs"]:
        sha = touch_shas[entry["path"]]
        expected = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", f"{sha}..HEAD"],
            check=True,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        ).stdout.strip()
        assert entry["last_touch_sha"] == sha
        assert entry["commits_since"] == int(expected)


def test_genuine_touch_buried_under_many_non_genuine_touches_is_found(tmp_path, monkeypatch):
    spawns: list[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    repo = _repo_with_old_touch(tmp_path)
    touch_sha = _head(repo)
    _fillers(repo, THRESHOLD_COMMITS + 1, 40)
    spaced = _doc()
    for i in range(12):
        spaced += "   \n"
        _commit(repo, {"README.md": spaced}, 20, f"whitespace {i}")
    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    entry = _report(repo)
    assert entry["last_touch_sha"] == touch_sha
    assert entry["stale"] is True
    assert 0 < len(spawns) <= 8


def test_commits_since_is_exact_when_a_side_branch_forks_before_the_touch(tmp_path):
    repo = _init(tmp_path / "repo")
    _commit(repo, {"README.md": _doc()}, 80, "base")
    _git(repo, "checkout", "-q", "-b", "side")
    _fillers(repo, 3, 70)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"README.md": _doc(["Touched after the fork."])}, 60, "authored")
    touch_sha = _head(repo)
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=_at(50))
    _fillers(repo, 2, 40)
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", f"{touch_sha}..HEAD"],
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip()
    entry = _report(repo)
    assert entry["last_touch_sha"] == touch_sha
    assert entry["commits_since"] == int(expected) == 6


_SKIP = {"docs": [], "skipped": doc_staleness.NO_COMMIT_GRAPH_SKIP}


def test_from_registry_names_the_skip_on_a_graph_free_repo_and_spawns_no_git(tmp_path, monkeypatch):
    repo = _repo_with_old_touch(tmp_path)
    _write_registry(repo, ["README.md"], THRESHOLD_COMMITS, THRESHOLD_DAYS)

    def refuse(*_a, **_k):
        raise AssertionError("git spawned on a graph-free repo")

    monkeypatch.setattr(doc_staleness, "_git", refuse)
    monkeypatch.setattr(doc_staleness, "run_git", refuse)
    assert build_doc_staleness_report_from_registry(str(repo)) == _SKIP


def test_from_registry_walks_a_repo_with_a_commit_graph(tmp_path):
    from coordinator_core.benchmarks.doc_staleness_full_path import ensure_fixture

    repo = ensure_fixture(tmp_path)
    _write_registry(repo, ["README.md", "INSTALL.md"], THRESHOLD_COMMITS, THRESHOLD_DAYS)
    out = build_doc_staleness_report_from_registry(str(repo))
    assert "skipped" not in out
    assert {d["path"]: d["status"] for d in out["docs"]} == {"README.md": "ok", "INSTALL.md": "ok"}


def test_has_commit_graph_follows_a_gitdir_file_and_commondir(tmp_path):
    common = tmp_path / "common.git"
    chain = common / "objects" / "info" / "commit-graphs"
    chain.mkdir(parents=True)
    gitdir = tmp_path / "wt-gitdir"
    gitdir.mkdir()
    (gitdir / "commondir").write_text("../common.git\n", encoding="utf-8")
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert _has_commit_graph(repo) is False
    (chain / "commit-graph-chain").write_text("x\n", encoding="utf-8")
    assert _has_commit_graph(repo) is True


def test_has_commit_graph_reads_the_single_file_graph(tmp_path):
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True)
    assert _has_commit_graph(tmp_path) is False
    (info / "commit-graph").write_bytes(b"CGPH")
    assert _has_commit_graph(tmp_path) is True
