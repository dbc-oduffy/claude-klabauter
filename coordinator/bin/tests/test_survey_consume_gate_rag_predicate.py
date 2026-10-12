"""`_rag_predicate` reads the index the contract names; `predicate_only` invokes no op."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

GATE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "survey-consume-gate.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("survey_consume_gate_rag_predicate", GATE_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survey_consume_gate_rag_predicate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _rag_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".project-rag"
    d.mkdir()
    return d


def test_index_present_with_no_manifest_is_present(gate, tmp_path):
    (_rag_dir(tmp_path) / "graph.db").write_bytes(b"x")

    out = gate._rag_predicate(str(tmp_path))

    assert out["rag_present"] is True
    assert "no readable manifest" in out["reason"]


def test_malformed_manifest_falls_back_to_graph_db(gate, tmp_path):
    d = _rag_dir(tmp_path)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    (d / "graph.db").write_bytes(b"x")

    assert gate._rag_predicate(str(tmp_path))["rag_present"] is True


def test_manifest_naming_another_artifact_is_present_when_it_exists(gate, tmp_path):
    d = _rag_dir(tmp_path)
    (d / "manifest.json").write_text(json.dumps({"structural_index_lite": "lite.db"}), encoding="utf-8")
    (d / "lite.db").write_bytes(b"x")

    out = gate._rag_predicate(str(tmp_path))

    assert out["rag_present"] is True
    assert out["checked_path"].endswith("lite.db")


def test_manifest_naming_a_missing_artifact_is_absent_even_with_graph_db(gate, tmp_path):
    d = _rag_dir(tmp_path)
    (d / "manifest.json").write_text(json.dumps({"structural_index_lite": "lite.db"}), encoding="utf-8")
    (d / "graph.db").write_bytes(b"x")

    assert gate._rag_predicate(str(tmp_path))["rag_present"] is False


def test_zero_byte_index_is_absent(gate, tmp_path):
    (_rag_dir(tmp_path) / "graph.db").write_bytes(b"")

    out = gate._rag_predicate(str(tmp_path))

    assert out["rag_present"] is False
    assert "zero bytes" in out["reason"]


def test_no_example_retrieval_repo_dir_is_absent(gate, tmp_path):
    assert gate._rag_predicate(str(tmp_path))["rag_present"] is False


def test_predicate_only_on_a_rag_absent_tree_invokes_no_op(gate, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "_invoke_op", lambda *a, **k: calls.append(a) or (0, {}, None))

    out = gate.run_gate(
        {
            "repo_root": str(tmp_path),
            "claude_klabauter_root": str(tmp_path),
            "run_id": "r",
            "mode": "refresh",
            "census_buckets": [{"bucketId": "a", "dirs": ["a"]}],
            "predicate_only": True,
        }
    )

    assert calls == []
    assert out["rag_present"] is False
    assert out["chunk_table"] is None
    assert out["churn_result"] is None
    assert out["coverage"] is None


def test_without_predicate_only_a_rag_absent_tree_still_runs_extraction(gate, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "_invoke_op", lambda *a, **k: calls.append(a) or (1, None, "boom"))

    out = gate.run_gate(
        {
            "repo_root": str(tmp_path),
            "claude_klabauter_root": str(tmp_path),
            "run_id": "r",
            "census_buckets": [{"bucketId": "a", "dirs": ["a"]}],
        }
    )

    assert len(calls) == 1
    assert out["chunk_table"]["ok"] is False
