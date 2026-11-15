"""Triage verdicts: isolation rerun for test rows, HEAD re-verify for non-test rows."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.grind_triage import triage as triage_mod
from coordinator_core.grind_triage.contract import TriageRow, Verdict


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def alive():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "test_a.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "test_b.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _fake_runner(failed=(), unrun=None):
    calls = []

    def run_chunk(files, root, timeout, verdicts):
        calls.append(list(files))
        for f in files:
            if unrun and f in unrun:
                verdicts[f] = {"unrun": unrun[f]}
            else:
                ids = [i for i in failed if i.startswith(f)]
                verdicts[f] = {"failed": ids, "ran": ids + [f + "::ran"]}

    run_chunk.calls = calls
    return run_chunk


def _test_row(key, target):
    return TriageRow(key=key, title=key, is_test=True, target=target)


def _by_key(results):
    return {r.key: r for r in results}


def test_test_row_passing_in_isolation_is_flake(repo):
    runner = _fake_runner()
    # node "ran" is reported by the fake; a node absent from `ran` would be "not collected".
    rows = [_test_row("a", "pkg/test_a.py")]
    res = _by_key(triage_mod.triage(rows, repo, run_chunk=runner))
    assert res["a"].verdict is Verdict.FLAKE


def test_test_row_red_in_isolation_survives(repo):
    runner = _fake_runner(failed=["pkg/test_a.py::test_x"])
    rows = [_test_row("a", "pkg/test_a.py::test_x")]
    res = _by_key(triage_mod.triage(rows, repo, run_chunk=runner))
    assert res["a"].verdict is Verdict.SURVIVOR
    assert "red" in res["a"].evidence


def test_unrun_and_gone_test_rows_survive_never_flake(repo):
    runner = _fake_runner(unrun={"pkg/test_b.py": "collection error"})
    rows = [
        _test_row("b", "pkg/test_b.py"),
        _test_row("gone", "pkg/test_missing.py"),
        _test_row("notpy", "pkg/mod.py"),
        _test_row("empty", ""),
    ]
    res = _by_key(triage_mod.triage(rows, repo, run_chunk=runner))
    assert {r.verdict for r in res.values()} == {Verdict.SURVIVOR}
    assert "collection error" in res["b"].evidence
    assert runner.calls == [["pkg/test_b.py"]]


def test_test_files_run_in_chunks_not_per_row(repo):
    runner = _fake_runner()
    rows = [_test_row("a1", "pkg/test_a.py::test_x"), _test_row("a2", "pkg/test_a.py"), _test_row("b", "pkg/test_b.py")]
    triage_mod.triage(rows, repo, chunk_files=2, run_chunk=runner)
    assert runner.calls == [["pkg/test_a.py", "pkg/test_b.py"]]
    runner2 = _fake_runner()
    triage_mod.triage(rows, repo, chunk_files=1, run_chunk=runner2)
    assert runner2.calls == [["pkg/test_a.py"], ["pkg/test_b.py"]]


def test_non_test_row_with_present_path_and_anchor_survives(repo):
    rows = [TriageRow(key="n", title="n", is_test=False, target="pkg/mod.py::def alive")]
    res = _by_key(triage_mod.triage(rows, repo, run_chunk=_fake_runner()))
    assert res["n"].verdict is Verdict.SURVIVOR


def test_non_test_row_missing_path_or_anchor_is_not_reproduced(repo):
    rows = [
        TriageRow(key="p", title="p", is_test=False, target="pkg/gone.py"),
        TriageRow(key="a", title="a", is_test=False, target="pkg/mod.py::def removed"),
    ]
    res = _by_key(triage_mod.triage(rows, repo))
    assert res["p"].verdict is Verdict.NOT_REPRODUCED
    assert res["a"].verdict is Verdict.NOT_REPRODUCED


def test_cited_line_suffix_and_spaced_paths_resolve_at_head(repo):
    (repo / "pkg" / "has space.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "space")
    rows = [
        TriageRow(key="l", title="l", is_test=False, target="pkg/mod.py:12"),
        TriageRow(key="s", title="s", is_test=False, target="pkg/has space.py"),
        TriageRow(key="m", title="m", is_test=False, target="pkg/gone space.py"),
    ]
    res = _by_key(triage_mod.triage(rows, repo))
    assert res["l"].verdict is Verdict.SURVIVOR
    assert res["s"].verdict is Verdict.SURVIVOR
    assert res["m"].verdict is Verdict.NOT_REPRODUCED


def test_cited_directory_survives_never_not_reproduced(repo):
    rows = [
        TriageRow(key="d1", title="d", is_test=False, target="pkg/"),
        TriageRow(key="d2", title="d", is_test=False, target="pkg/subdir"),
    ]
    res = _by_key(triage_mod.triage(rows, repo))
    assert {r.verdict for r in res.values()} == {Verdict.SURVIVOR}


def test_non_test_judged_at_head_not_worktree(repo):
    (repo / "pkg" / "mod.py").write_text("# rewritten, anchor removed\n", encoding="utf-8")
    (repo / "pkg" / "new_untracked.py").write_text("def alive():\n    pass\n", encoding="utf-8")
    rows = [
        TriageRow(key="h", title="h", is_test=False, target="pkg/mod.py::def alive"),
        TriageRow(key="u", title="u", is_test=False, target="pkg/new_untracked.py"),
    ]
    res = _by_key(triage_mod.triage(rows, repo))
    assert res["h"].verdict is Verdict.SURVIVOR
    assert res["u"].verdict is Verdict.NOT_REPRODUCED


def test_non_test_rows_spawn_no_git(repo, monkeypatch):
    calls = []
    real = subprocess.run

    def counting(*a, **kw):
        calls.append(a[0])
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "run", counting)
    rows = [TriageRow(key=f"r{i}", title="t", is_test=False, target=f"pkg/mod.py::def {i}") for i in range(20)]
    triage_mod.triage(rows, repo)
    assert calls == []


def test_unreadable_head_survives(tmp_path):
    rows = [TriageRow(key="x", title="x", is_test=False, target="a.py")]
    res = _by_key(triage_mod.triage(rows, tmp_path))
    assert res["x"].verdict is Verdict.SURVIVOR
    assert res["x"].evidence == "HEAD unreadable"


def test_order_preserved_and_survivors_filter(repo):
    rows = [
        TriageRow(key="n1", title="", is_test=False, target="pkg/gone.py"),
        _test_row("t1", "pkg/test_a.py"),
        TriageRow(key="n2", title="", is_test=False, target="pkg/mod.py"),
    ]
    results = triage_mod.triage(rows, repo, run_chunk=_fake_runner())
    assert [r.key for r in results] == ["n1", "t1", "n2"]
    assert [r.key for r in triage_mod.survivors(rows, results)] == ["n2"]
