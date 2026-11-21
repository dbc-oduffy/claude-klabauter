"""Tests of the update-docs ops budget benchmark: output-key pin, fixture shape, export, rtt summary."""

from __future__ import annotations


import pytest

from coordinator_core.benchmarks import update_docs_ops_budget as bench
from coordinator_core.git.run import run_git
from pathlib import Path

OP_KEYS = {"route", "reply_code", "process_ms", "spawn_count", "dominant_leg", "wall_ms"}
DOOR_KEYS = {
    "callers", "door_rtt_p50_ms_idle", "door_rtt_p95_ms_idle", "door_rtt_p50_ms_load",
    "door_rtt_p95_ms_load", "failed_idle", "failed_load", "unavailable",
}


def test_op_table_names_the_four_ops_with_default_argv() -> None:
    assert set(bench.OPS) == {"repomap-gate", "refresh-queries", "reap-stale-subagent-sidecars", "check-citation-integrity"}
    assert bench.OPS["repomap-gate"] == ("update-docs-probes", ["repomap-gate"])
    assert all(not argv for op, (_n, argv) in bench.OPS.items() if op != "repomap-gate")


def test_default_callers_is_bounded_and_positive() -> None:
    import os

    n = bench.default_callers()
    assert 1 <= n <= max(1, (os.cpu_count() or 2) // 2)


def test_percentile_interpolates_and_rejects_empty() -> None:
    assert bench.percentile([10.0], 95) == 10.0
    assert bench.percentile([0.0, 100.0], 50) == 50.0
    assert bench.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0
    with pytest.raises(ValueError):
        bench.percentile([], 50)


def test_summarize_rtt_counts_failures_without_timing_them() -> None:
    assert bench.summarize_rtt([(10.0, True), (30.0, True), (9999.0, False)]) == (20.0, 29.0, 1)
    assert bench.summarize_rtt([(5.0, False)]) == (None, None, 1)


def test_dominant_leg_prefers_spawned_children_when_they_outweigh_self() -> None:
    spawned = {"spawned": ["generate-repomap.py", "git", "git"], "self_ms": 10.0, "top_function": "x.py:f", "route": "warm"}
    assert bench.dominant_leg(spawned, 400.0).startswith("spawned:git")
    own = {"spawned": [], "self_ms": 300.0, "top_function": "refresh_queries.py:process_file", "route": "warm"}
    assert bench.dominant_leg(own, 310.0) == "refresh_queries.py:process_file"
    assert bench.dominant_leg({"route": "cold", "self_ms": 0.0}, 5.0) == "cold launcher"


def test_fixture_is_committed_with_wiki_pages_and_seeded_baseline(tmp_path: Path) -> None:
    repo = bench.ensure_ops_fixture(tmp_path, wiki_pages=7)
    tracked = run_git(["ls-files", "-z"], cwd=str(repo)).stdout.split("\0")
    assert sum(1 for t in tracked if t.startswith("docs/wiki/")) == 7
    assert bench._BASELINE_REL in tracked
    assert run_git(["status", "--porcelain", "--untracked-files=no"], cwd=str(repo)).stdout.strip() == ""


def test_export_source_extracts_only_the_named_sha(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    (repo / "coordinator_core").mkdir(parents=True)
    (repo / "coordinator" / "bin").mkdir(parents=True)
    (repo / "coordinator" / "lib").mkdir(parents=True)
    (repo / "coordinator" / "lib" / "helper.py").write_text("H = 1\n", encoding="utf-8")
    (repo / "coordinator_core" / "mod.py").write_text("V = 1\n", encoding="utf-8")
    (repo / "coordinator" / "bin" / "tool.py").write_text("T = 1\n", encoding="utf-8")
    (repo / "other.txt").write_text("not exported\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "one"]):
        assert run_git(args, cwd=str(repo), env=env).returncode == 0
    sha = run_git(["rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()
    (repo / "coordinator_core" / "mod.py").write_text("V = 2\n", encoding="utf-8")
    out = bench.export_source(repo, sha, tmp_path / "export")
    assert (out / "coordinator_core" / "mod.py").read_text(encoding="utf-8") == "V = 1\n"
    assert (out / "coordinator" / "bin" / "tool.py").is_file()
    assert (out / "coordinator" / "lib" / "helper.py").is_file()
    assert not (out / "other.txt").exists()
    with pytest.raises(RuntimeError):
        bench.export_source(repo, "0" * 40, tmp_path / "bad")


def test_remove_tree_clears_read_only_files(tmp_path: Path) -> None:
    import os
    import stat

    d = tmp_path / "w" / "objects"
    d.mkdir(parents=True)
    f = d / "obj"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, stat.S_IREAD)
    bench._remove_tree(tmp_path / "w")
    assert not (tmp_path / "w").exists()


def test_parse_rejects_bad_flags() -> None:
    assert bench._parse(["--callers", "0"]) is None
    assert bench._parse(["--callers", "x"]) is None
    assert bench._parse(["--source"]) is None
    assert bench._parse(["--bogus"]) is None
    assert bench._parse([])["source"] == "live"
    assert bench._parse(["--source", "abc", "--no-door"]) == {"source": "abc", "callers": None, "workdir": None, "door": False}


def test_main_usage_error_returns_2() -> None:
    assert bench.main(["--callers", "0"]) == 2


def test_main_prints_the_pinned_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seen = {}

    def fake_measure(source, workdir, callers, with_door: bool = True):
        seen.update(source=source, callers=callers, with_door=with_door, workdir=workdir)
        return {"source": source, "ops": {op: dict.fromkeys(OP_KEYS) for op in bench.OPS}, "door": dict.fromkeys(DOOR_KEYS)}

    monkeypatch.setattr(bench, "measure", fake_measure)
    monkeypatch.setattr("coordinator_core.benchmarks.declare_benchmark_origin", lambda: None)
    assert bench.main(["--workdir", str(tmp_path / "w"), "--callers", "3", "--no-door"]) == 0
    assert seen["callers"] == 3 and seen["with_door"] is False and seen["workdir"] == (tmp_path / "w").resolve()
    import json

    report = json.loads(capsys.readouterr().out)
    assert set(report["ops"]) == set(bench.OPS)
    assert all(set(v) == OP_KEYS for v in report["ops"].values())
    assert set(report["door"]) == DOOR_KEYS


def test_measure_door_without_installed_door_reports_why(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "installed_door", lambda name="coordinator-invoke": None)
    door = bench.measure_door(tmp_path, 2)
    assert set(door) == DOOR_KEYS
    assert door["unavailable"] and door["door_rtt_p95_ms_load"] is None and door["callers"] == 2
