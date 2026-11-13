"""Rag-present gate fills the census slot from the tree and writes structure.json; rag-absent keeps cartography."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

GATE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "survey-consume-gate.py"
CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("survey_consume_gate_tree_census", GATE_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survey_consume_gate_tree_census"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_repo(tmp_path: Path, rag: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "a" / "x.py").write_text("x\n", encoding="utf-8")
    (repo / "a" / "y.py").write_text("y\n", encoding="utf-8")
    (repo / "b").mkdir()
    (repo / "b" / "z.py").write_text("z\n", encoding="utf-8")
    if rag:
        (repo / ".project-rag").mkdir()
        (repo / ".project-rag" / "graph.db").write_bytes(b"x")
    return repo


@pytest.fixture(autouse=True)
def _fake_ls_files(monkeypatch):
    """Stands in for the one `git ls-files -z` the tree census spawns: lists the files on disk."""
    from coordinator_core.architecture_survey import tree_census

    def _run(argv, **kwargs):
        root = Path(argv[2])
        names = sorted(
            p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".project-rag" not in p.parts
        )
        return subprocess.CompletedProcess(argv, 0, stdout=("\0".join(names) + "\0").encode(), stderr=b"")

    monkeypatch.setattr(tree_census.subprocess, "run", _run)


def _config(repo: Path, tmp_path: Path) -> dict:
    return {
        "repo_root": str(repo),
        "claude_klabauter_root": str(CLAUDE_KLABAUTER_ROOT),
        "run_id": "20261112T000000Z-t",
        "run_dir": str(tmp_path / "run"),
        "census_buckets": [{"bucketId": "A", "dirs": ["a"]}, {"bucketId": "B", "dirs": ["b"]}],
    }


def _no_rag_registration(gate, monkeypatch):
    monkeypatch.setattr(gate, "_rag_registration", lambda repo_root: {})


def test_rag_present_yields_tree_census_and_invokes_no_op(gate, tmp_path, monkeypatch):
    _no_rag_registration(gate, monkeypatch)
    calls = []
    monkeypatch.setattr(gate, "_invoke_op", lambda *a, **k: calls.append(a) or (1, None, "boom"))
    repo = _git_repo(tmp_path, rag=True)

    out = gate.run_gate(_config(repo, tmp_path))

    assert calls == []
    assert out["chunk_table"]["ok"] is True
    assert out["chunk_table"]["source"] == "tree"
    by_bucket = {r["bucket_id"]: r["files"] for r in out["chunk_table"]["censusShapedResults"]}
    assert sorted(f["path"] for f in by_bucket["A"]) == ["a/x.py", "a/y.py"]
    assert out["coverage"]["total_inventoried"] == 3
    assert out["coverage"]["total_on_disk"] == 3


def test_rag_present_writes_structure_json_and_reports_its_path(gate, tmp_path, monkeypatch):
    _no_rag_registration(gate, monkeypatch)
    repo = _git_repo(tmp_path, rag=True)

    out = gate.run_gate(_config(repo, tmp_path))

    path = Path(out["structure_path"])
    assert path == tmp_path / "run" / "structure.json"
    assert json.loads(path.read_text(encoding="utf-8"))["needs"]["dead-paths"]["covered"] is False
    assert "structure_unavailable_reason" not in out


def test_daemon_unreachable_degrades_to_no_structure_with_an_ok_census(gate, tmp_path, monkeypatch):
    _no_rag_registration(gate, monkeypatch)
    from coordinator_core.architecture_survey import structure

    def _down(*a, **k):
        raise structure.StructureUnavailable("identity probe failed: URLError")

    monkeypatch.setattr(structure, "fetch", _down)
    repo = _git_repo(tmp_path, rag=True)

    out = gate.run_gate(_config(repo, tmp_path))

    assert out["structure_path"] is None
    assert "identity probe failed" in out["structure_unavailable_reason"]
    assert out["chunk_table"]["ok"] is True
    assert out["chunk_table"]["source"] == "tree"


def test_rag_absent_runs_cartography_and_carries_no_structure_keys(gate, tmp_path, monkeypatch):
    _no_rag_registration(gate, monkeypatch)
    calls = []
    monkeypatch.setattr(gate, "_invoke_op", lambda *a, **k: calls.append(a[1]) or (1, None, "boom"))
    repo = _git_repo(tmp_path, rag=False)

    out = gate.run_gate(_config(repo, tmp_path))

    assert calls == ["cartography.chunk_table"]
    assert out["chunk_table"]["ok"] is False
    assert "structure_path" not in out
    assert not (tmp_path / "run").exists()


def test_rag_present_without_census_buckets_declines_the_tree_census(gate, tmp_path, monkeypatch):
    _no_rag_registration(gate, monkeypatch)
    repo = _git_repo(tmp_path, rag=True)
    config = {**_config(repo, tmp_path), "census_buckets": []}

    out = gate.run_gate(config)

    assert out["chunk_table"]["ok"] is False
    assert "censusBuckets" in out["chunk_table"]["declined_reason"]
