"""Tests for the disk-only example-retrieval-repo fleet registration detector."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import rag_fleet_registration as rfr
from coordinator_core.install.provisioned_interpreter import ProvisionedInterpreter


@pytest.fixture
def no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def _fleet(monkeypatch, flat):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: flat)


def _registry(tmp_path, monkeypatch, paths):
    reg = tmp_path / "projects.json"
    reg.write_text(
        json.dumps({"version": 2, "sources": [{"name": p.name, "path": str(p), "kind": "python"} for p in paths]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROJECT_RAG_REGISTRY_PATH", str(reg))


def _rag(tmp_path):
    root = tmp_path / "project-rag"
    root.mkdir(exist_ok=True)
    return rfr.RagInstall(root=root, interpreter="/pin/python")


def test_detect_none_when_unset(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.install.provisioned_interpreter.resolve_provisioned_interpreter",
        lambda *a, **k: ProvisionedInterpreter(None, "repos.project_rag unset or missing"),
    )
    assert rfr.detect_example_retrieval_repo() is None
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_detect_returns_install(monkeypatch, tmp_path):
    root = tmp_path / "rag"
    root.mkdir()
    monkeypatch.setattr(
        "coordinator_core.install.provisioned_interpreter.resolve_provisioned_interpreter",
        lambda *a, **k: ProvisionedInterpreter("/pin/python", "", str(root)),
    )
    assert rfr.detect_example_retrieval_repo() == rfr.RagInstall(root=root, interpreter="/pin/python")


def test_detect_none_when_root_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "coordinator_core.install.provisioned_interpreter.resolve_provisioned_interpreter",
        lambda *a, **k: ProvisionedInterpreter("/pin/python", "", str(tmp_path / "gone")),
    )
    assert rfr.detect_example_retrieval_repo() is None


def test_fleet_roots_dedupe_skip_empty_and_missing(monkeypatch, tmp_path, no_spawn):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _fleet(
        monkeypatch,
        {
            "repos.a": str(a),
            "repos.a_alias": str(a),
            "repos.b": str(b),
            "repos.empty": "",
            "repos.gone": str(tmp_path / "gone"),
            "repos.nested.key": str(b),
            "other.x": str(b),
        },
    )
    assert rfr.fleet_repo_roots() == {"a": a.resolve(), "b": b.resolve()}


def test_unregistered_repos_reads_json_only(monkeypatch, tmp_path, no_spawn):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _fleet(monkeypatch, {"repos.a": str(a), "repos.b": str(b)})
    _registry(tmp_path, monkeypatch, [a.resolve()])
    assert rfr.unregistered_repos(_rag(tmp_path)) == [("b", b.resolve())]


def test_missing_registry_means_all_unregistered(monkeypatch, tmp_path, no_spawn):
    a = tmp_path / "a"
    a.mkdir()
    _fleet(monkeypatch, {"repos.a": str(a)})
    monkeypatch.setenv("PROJECT_RAG_REGISTRY_PATH", str(tmp_path / "nope.json"))
    assert rfr.registered_roots(_rag(tmp_path)) == set()
    assert [s for s, _ in rfr.unregistered_repos(_rag(tmp_path))] == ["a"]


@pytest.mark.parametrize("body", ["not json", "[]", '{"sources": 3}'])
def test_malformed_registry_is_empty(monkeypatch, tmp_path, body):
    reg = tmp_path / "projects.json"
    reg.write_text(body, encoding="utf-8")
    monkeypatch.setenv("PROJECT_RAG_REGISTRY_PATH", str(reg))
    assert rfr.registered_roots(_rag(tmp_path)) == set()


def test_remedy_line_is_runnable_script():
    line = rfr.remedy_line("foo")
    assert line == "unregistered with example-retrieval-repo: foo — fix: python scripts/setup.py"


def test_fleet_languages_depth_capped(tmp_path):
    (tmp_path / "x.py").write_text("")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "m.ts").write_text("")
    deep = tmp_path / "d1" / "d2" / "d3" / "d4" / "d5"
    deep.mkdir(parents=True)
    (deep / "z.rs").write_text("")
    skipped = tmp_path / "node_modules"
    skipped.mkdir()
    (skipped / "q.go").write_text("")
    assert rfr.fleet_languages([tmp_path]) == {"python", "typescript"}


def test_extractor_argv_when_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(rfr, "_symbol_extract_importable", lambda: False)
    rag = _rag(tmp_path)
    argv = rfr.extractor_install_argv(rag, {"typescript"})
    assert argv == ["/pin/python", "-m", "pip", "install", f"{rag.root / 'packaging' / 'symbol-extract'}[tree-sitter]"]


def test_extractor_argv_none_when_importable_or_unneeded(monkeypatch, tmp_path):
    rag = _rag(tmp_path)
    monkeypatch.setattr(rfr, "_symbol_extract_importable", lambda: True)
    assert rfr.extractor_install_argv(rag, {"typescript"}) is None
    monkeypatch.setattr(rfr, "_symbol_extract_importable", lambda: False)
    assert rfr.extractor_install_argv(rag, {"python"}) is None


def test_registration_plan_is_json_and_covers_every_root(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "x.ts").write_text("")
    _fleet(monkeypatch, {"repos.a": str(a), "repos.b": str(b)})
    monkeypatch.setattr(rfr, "_symbol_extract_importable", lambda: False)
    rag = _rag(tmp_path)
    plan = rfr.registration_plan(rag)
    json.dumps(plan)
    assert set(plan["roots"]) == {"a", "b"}
    for slug, root in plan["roots"].items():
        assert plan["register_argv"][slug][-3:] == ["--register-only", "--project-root", root]
        assert "--non-interactive" in plan["index_argv"][slug]
    assert plan["extractor_argv"][:4] == ["/pin/python", "-m", "pip", "install"]
