"""tree_census: bucketing, precedence, unbucketed count, one git spawn, shape validity."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.architecture_survey import census_shape, tree_census


def _repo(tmp_path: Path, paths: list[str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel in paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _by_id(payload: dict) -> dict[str, list[str]]:
    return {b["bucket_id"]: [f["path"] for f in b["files"]] for b in payload["censusShapedResults"]}


def test_bucketing_nested_precedence_and_unbucketed(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "a/sub/y.py", "b/z.py", "README.md", "ab/w.py"])
    buckets = [
        {"bucketId": "A", "dirs": ["a"]},
        {"bucketId": "Sub", "dirs": ["a/sub/", "other"]},
        {"bucketId": "Empty", "dirs": ["nothing"]},
        {"bucketId": "B", "dirs": ["b"]},
    ]
    payload = tree_census.build(repo, buckets)
    got = _by_id(payload)
    assert got["A"] == ["a/x.py"]
    assert got["Sub"] == ["a/sub/y.py"]
    assert got["B"] == ["b/z.py"]
    assert got["Empty"] == []
    assert payload["unbucketedCount"] == 2
    assert payload["source"] == "tree"
    assert payload["chunkTablePath"] is None
    assert payload["oversizedSignalAvailable"] is False
    assert payload["oversizedCount"] == 0
    assert all(f["loc"] is None and f["oversized"] is False
               for b in payload["censusShapedResults"] for f in b["files"])
    assert census_shape.validate(payload) == []


def test_multiple_dirs_longest_across_buckets_wins(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "a/deep/y.py"])
    buckets = [
        {"bucketId": "Wide", "dirs": ["a", "zzz"]},
        {"bucketId": "Deep", "dirs": ["a/deep"]},
    ]
    got = _by_id(tree_census.build(repo, buckets))
    assert got["Wide"] == ["a/x.py"]
    assert got["Deep"] == ["a/deep/y.py"]


def test_root_prefix_catches_rest_and_tie_goes_to_first(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "top.md"])
    buckets = [
        {"bucketId": "First", "dirs": ["a"]},
        {"bucketId": "Second", "dirs": ["a"]},
        {"bucketId": "Root", "dirs": ["."]},
    ]
    got = _by_id(tree_census.build(repo, buckets))
    assert got["First"] == ["a/x.py"]
    assert got["Second"] == []
    assert got["Root"] == ["top.md"]


def _count_git_spawns(monkeypatch) -> list:
    calls: list = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(tree_census.subprocess, "run", spy)
    return calls


def test_exactly_one_git_spawn_for_one_and_many_files(tmp_path, monkeypatch):
    small = _repo(tmp_path / "small", ["a/one.py"])
    big = _repo(tmp_path / "big", [f"a/f{i}.py" for i in range(500)])
    buckets = [{"bucketId": "A", "dirs": ["a"]}]
    calls = _count_git_spawns(monkeypatch)
    assert len(_by_id(tree_census.build(small, buckets))["A"]) == 1
    assert len(calls) == 1
    calls.clear()
    assert len(_by_id(tree_census.build(big, buckets))["A"]) == 500
    assert len(calls) == 1
    assert calls[0][-2:] == ["ls-files", "-z"]


def _load_gate():
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "survey-consume-gate.py"
    spec = importlib.util.spec_from_file_location("survey_consume_gate_tree_census", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survey_consume_gate_tree_census"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_gate_fills_census_from_tree_when_rag_present(tmp_path, monkeypatch):
    gate = _load_gate()
    repo = _repo(tmp_path, ["a/x.py", "b/y.py"])
    monkeypatch.setattr(gate, "_rag_predicate", lambda root: {"rag_present": True})
    out = gate.run_gate({
        "repo_root": str(repo),
        "claude_klabauter_root": str(Path(__file__).resolve().parents[3]),
        "run_id": "r",
        "census_buckets": [{"bucketId": "A", "dirs": ["a"]}],
    })
    assert out["chunk_table"]["ok"] is True
    assert out["chunk_table"]["source"] == "tree"
    assert census_shape.validate(out["chunk_table"]) == []


def test_gate_declines_tree_census_without_buckets(tmp_path, monkeypatch):
    gate = _load_gate()
    monkeypatch.setattr(gate, "_rag_predicate", lambda root: {"rag_present": True})
    out = gate.run_gate({"repo_root": str(tmp_path), "claude_klabauter_root": str(tmp_path), "run_id": "r", "census_buckets": []})
    assert out["chunk_table"]["ok"] is False
