"""testing.suite_runs: freshness verdicts, git spawn pin, and handler CPU time."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from coordinator_core.ops import suite_runs_op
from coordinator_core.testing import suite_run_record as srr
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("one\n")
    (tmp_path / "mod.py").write_text("a = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _record(repo, sha=None, dirty=(), collection: str = "default", at: str = "t0") -> None:
    fields = {
        "repo_root": str(repo), "collection": collection, "command": ["pytest"],
        "head_sha": sha or _git(repo, "rev-parse", "HEAD"),
        "started_at": at, "ended_at": "t1", "dirty_paths": list(dirty),
        "duration_s": 1.0, "exit_code": 0, "outcome": "pass", "serialized": True,
        "log_truncated": False,
    }
    srr.write_run(srr.record_dir(repo, collection), fields, None)


def _fresh(repo, **extra):
    out = suite_runs_op._suite_runs({"repo_root": str(repo), **extra})
    return out["collections"]["default"]["current"]["freshness"]


def test_sha_match_is_fresh(repo) -> None:
    _record(repo)
    assert _fresh(repo) == {"fresh": True}


def test_docs_only_edit_is_fresh(repo) -> None:
    _record(repo)
    (repo / "docs" / "x.md").write_text("two\n")
    assert _fresh(repo) == {"fresh": True}


def test_source_edit_is_stale_with_files(repo) -> None:
    _record(repo)
    (repo / "mod.py").write_text("a = 2\n")
    assert _fresh(repo) == {"fresh": False, "changed_files": ["mod.py"]}


def test_untracked_py_is_stale(repo) -> None:
    _record(repo)
    (repo / "new_test.py").write_text("x = 1\n")
    v = _fresh(repo)
    assert v["fresh"] is False and "new_test.py" in v["changed_files"]


def test_dirty_at_run_short_circuits(repo) -> None:
    _record(repo, dirty=["mod.py"])
    assert _fresh(repo)["reason"] == "dirty-at-run"


def test_unresolvable_sha(repo) -> None:
    _record(repo, sha="0" * 40)
    assert _fresh(repo) == {"fresh": False, "reason": "sha-unresolvable"}


def test_option_like_sha_is_unresolvable(repo) -> None:
    _record(repo, sha="--output=x")
    assert _fresh(repo) == {"fresh": False, "reason": "sha-unresolvable"}


def test_non_slug_collection_is_skipped(repo) -> None:
    _record(repo)
    (repo / ".coordinator-local" / "suite-runs" / "Bad Name").mkdir()
    out = suite_runs_op._suite_runs({"repo_root": str(repo)})
    assert list(out["collections"]) == ["default"]


def _count_spawns(monkeypatch):
    calls = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        calls.append(argv)
        return real(argv, *a, **kw)

    monkeypatch.setattr(suite_runs_op.subprocess, "run", counting)
    return calls


def test_no_records_zero_spawns(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_spawns(monkeypatch)
    out = suite_runs_op._suite_runs({"repo_root": str(repo)})
    assert out["collections"] == {} and calls == []


def test_spawns_constant_two_regardless_of_sha_count(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _git(repo, "rev-parse", "HEAD")
    _record(repo, sha=first)
    _record(repo, sha=first)
    (repo / "docs" / "x.md").write_text("two\n")
    _git(repo, "commit", "-qam", "docs")
    _record(repo, collection="other")
    calls = _count_spawns(monkeypatch)
    suite_runs_op._suite_runs({"repo_root": str(repo)})
    assert len(calls) == 2


def test_spawn_count_independent_of_distinct_shas(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(4):
        _record(repo, sha=_git(repo, "rev-parse", "HEAD"), collection=f"c{i}", at=f"t{i}")
        (repo / "docs" / "x.md").write_text(f"v{i}\n")
        _git(repo, "commit", "-qam", f"docs{i}")
    calls = _count_spawns(monkeypatch)
    out = suite_runs_op._suite_runs({"repo_root": str(repo)})
    assert len(calls) == 2
    assert all(c["current"]["freshness"]["fresh"] for c in out["collections"].values())


def test_docs_dirty_fresh_and_source_dirty_stale_in_two_spawns(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(repo)
    (repo / "docs" / "x.md").write_text("dirty\n")
    calls = _count_spawns(monkeypatch)
    assert _fresh(repo) == {"fresh": True}
    assert len(calls) == 1
    (repo / "mod.py").write_text("a = 2\n")
    assert _fresh(repo) == {"fresh": False, "changed_files": ["mod.py"]}


def test_state_and_root_md_dirty_fresh_py_stale(repo) -> None:
    _record(repo)
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "q.yaml").write_text("a: 1\n")
    (repo / "README.md").write_text("hi\n")
    assert _fresh(repo) == {"fresh": True}
    (repo / "new.py").write_text("b = 1\n")
    assert _fresh(repo) == {"fresh": False, "changed_files": ["new.py"]}


def test_log_skipped_when_every_sha_is_head_and_bounded_otherwise(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(repo)
    calls = _count_spawns(monkeypatch)
    suite_runs_op._suite_runs({"repo_root": str(repo)})
    assert [c[1] for c in calls] == ["status"]
    (repo / "mod.py").write_text("a = 3\n")
    _git(repo, "commit", "-qam", "src")
    calls.clear()
    assert _fresh(repo)["fresh"] is False
    assert [c[1] for c in calls] == ["status", "log"]
    assert calls[1][-1] == f"{_git(repo, 'rev-parse', 'HEAD~1')}..HEAD"


def _red(repo, ids, sha=None, collection: str = "default") -> None:
    log = repo / "run.log"
    log.write_text("".join(f"FAILED {i} - boom\n" for i in ids), encoding="utf-8")
    fields = {
        "repo_root": str(repo), "collection": collection, "command": ["pytest"],
        "head_sha": sha or _git(repo, "rev-parse", "HEAD"),
        "started_at": "t0", "ended_at": "t1", "dirty_paths": [],
        "duration_s": 1.0, "exit_code": 1, "outcome": "fail", "serialized": True,
        "log_truncated": False,
    }
    srr.write_run(srr.record_dir(repo, collection), fields, log if ids else None)


def test_split_names_previous_and_new_with_both_shas(repo) -> None:
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/feature")
    _red(repo, ["t.py::a", "t.py::b"], sha="a" * 40)
    _red(repo, ["t.py::b", "t.py::c"], sha="b" * 40)
    split = suite_runs_op._suite_runs({"repo_root": str(repo)})["collections"]["default"]["failure_split"]
    assert split["previous_sha"] == "a" * 40 and split["main_sha"] is None
    assert split["ids"]["t.py::b"] == {"status": "pre-existing", "red_at": ["previous"]}
    assert split["ids"]["t.py::c"] == {"status": "new", "red_at": []}


def test_main_slot_written_only_on_main_and_keeps_its_sha(repo) -> None:
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _red(repo, ["t.py::m"], sha="c" * 40)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/feature")
    _red(repo, ["t.py::x"], sha="d" * 40)
    _red(repo, ["t.py::x", "t.py::m", "t.py::n"], sha="e" * 40)
    out = suite_runs_op._suite_runs({"repo_root": str(repo)})["collections"]["default"]
    assert out["main"]["head_sha"] == "c" * 40
    split = out["failure_split"]
    assert split["main_sha"] == "c" * 40 and split["previous_sha"] == "d" * 40
    assert split["ids"]["t.py::m"] == {"status": "pre-existing", "red_at": ["main"]}
    assert split["ids"]["t.py::x"]["red_at"] == ["previous"]
    assert split["ids"]["t.py::n"]["status"] == "new"


def test_explicit_failed_ids_override_current_and_add_no_spawn(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _red(repo, ["t.py::a"], sha="1" * 40)
    _red(repo, [], sha="2" * 40)
    baseline = _count_spawns(monkeypatch)
    suite_runs_op._suite_runs({"repo_root": str(repo)})
    plain = len(baseline)
    baseline.clear()
    out = suite_runs_op._suite_runs({"repo_root": str(repo), "failed_ids": ["t.py::a", "t.py::z"]})
    ids = out["collections"]["default"]["failure_split"]["ids"]
    assert ids["t.py::a"]["status"] == "pre-existing" and ids["t.py::z"]["status"] == "new"
    assert len(baseline) == plain


def test_run_on_main_is_not_its_own_baseline(repo) -> None:
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _red(repo, ["t.py::a"], sha="1" * 40)
    split = suite_runs_op._suite_runs({"repo_root": str(repo)})["collections"]["default"]["failure_split"]
    assert split["ids"]["t.py::a"] == {"status": "new", "red_at": []}


def test_green_run_has_no_split(repo) -> None:
    _record(repo)
    assert "failure_split" not in suite_runs_op._suite_runs({"repo_root": str(repo)})["collections"]["default"]


def test_handler_cpu_under_500ms(repo) -> None:
    _record(repo)
    _record(repo)
    before = os.times()
    t0 = time.process_time()
    out = suite_runs_op._suite_runs({"repo_root": str(repo)})
    after = os.times()
    assert out["collections"]["default"]["current"]["freshness"]["fresh"] is True
    cpu = (time.process_time() - t0) + (after.children_user - before.children_user) + (
        after.children_system - before.children_system
    )
    assert cpu < 0.5
