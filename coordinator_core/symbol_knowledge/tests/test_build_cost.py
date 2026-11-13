"""Tests for symbol_knowledge.build_cost, the contract types it returns, and the code_intel op shells."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.symbol_knowledge import build_cost, contract

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def alpha():\n    '''doc'''\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    (tmp_path / "notes.md").write_text("not python\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_flagged_symbol_docstring_empty_defaults_true_and_overrides() -> None:
    assert contract.FlaggedSymbol("m.f", "m.py", contract.KIND_ADJUST, True).docstring_empty is True
    assert contract.FlaggedSymbol("m.f", "m.py", contract.KIND_ADJUST, True, docstring_empty=False).docstring_empty is False


def test_workstate_ref_accepts_each_doc_kind_and_refuses_unknown() -> None:
    for kind in contract.DOC_KINDS:
        assert contract.WorkstateRef("a.py", "docs/x.md", kind).doc_kind == kind
    with pytest.raises(ValueError):
        contract.WorkstateRef("a.py", "docs/x.md", "memo")


def test_measure_counts_spawns_and_returns_result() -> None:
    result, cost = build_cost.measure(
        "t.op",
        lambda: subprocess.run(["git", "--version"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode,
        files_reextracted=3,
    )
    assert result == 0
    assert (cost.op, cost.spawns, cost.files_reextracted) == ("t.op", 1, 3)
    assert cost.process_ms >= 0


def test_measure_without_spawn_counts_zero_and_restores_subprocess() -> None:
    original = subprocess.run
    _, cost = build_cost.measure("t.op", lambda: sum(range(1000)))
    assert cost.spawns == 0
    assert subprocess.run is original


def test_measure_propagates_exception_and_restores_subprocess() -> None:
    original = subprocess.run

    def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        build_cost.measure("t.op", boom)
    assert subprocess.run is original


def test_full_tree_reextract_counts_only_python_files_and_one_git_spawn(repo: Path) -> None:
    cost, absent = build_cost.full_tree_reextract(repo, "HEAD")
    assert cost.op == build_cost.FULL_TREE_OP
    assert cost.files_reextracted == 2
    assert cost.spawns == 1
    assert absent == 0


def test_full_tree_reextract_reports_files_absent_from_worktree(repo: Path) -> None:
    (repo / "b.py").unlink()
    cost, absent = build_cost.full_tree_reextract(repo, "HEAD")
    assert (cost.files_reextracted, absent) == (1, 1)


def test_contract_entry_points_delegate_to_body_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import types

    fresh = types.ModuleType("coordinator_core.symbol_knowledge.freshness")
    fresh.freshness = lambda root, sha: {"root": str(root), "sha": sha}
    ws = types.ModuleType("coordinator_core.symbol_knowledge.workstate_index")
    ws.workstate_index = lambda root: {"root": str(root)}
    monkeypatch.setitem(__import__("sys").modules, "coordinator_core.symbol_knowledge.freshness", fresh)
    monkeypatch.setitem(__import__("sys").modules, "coordinator_core.symbol_knowledge.workstate_index", ws)
    assert contract.freshness(tmp_path, "abc") == {"root": str(tmp_path), "sha": "abc"}
    assert contract.workstate_index(tmp_path) == {"root": str(tmp_path)}


def test_freshness_without_body_module_is_not_applicable_naming_gaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "coordinator_core.symbol_knowledge.freshness", None)
    result = contract.freshness(tmp_path, "HEAD")
    assert result["status"] == "not_applicable"
    assert result["files_reextracted"] == 0 and result["queued"] == []
    assert "7e8139" in result["blocked_on"] and len(result["gaps"]) == 2
    result["gaps"].clear()
    assert len(contract.freshness(tmp_path, "HEAD")["gaps"]) == 2


def test_op_shells_pass_params_through_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops.code_intel_freshness import code_intel_freshness
    from coordinator_core.ops.code_intel_workstate_index import code_intel_workstate_index

    seen: dict = {}
    monkeypatch.setattr(contract, "freshness", lambda root, sha: seen.setdefault("f", (root, sha)) and {"ok": 1})
    monkeypatch.setattr(contract, "workstate_index", lambda root: seen.setdefault("w", root) and {"ok": 2})
    code_intel_freshness({}, tmp_path)
    code_intel_freshness({"sha": "abc"}, tmp_path)
    code_intel_workstate_index({}, tmp_path)
    assert seen["f"] == (tmp_path, "HEAD")
    assert seen["w"] == tmp_path
    seen.clear()
    code_intel_freshness({"sha": "abc"}, tmp_path)
    assert seen["f"] == (tmp_path, "abc")


def test_ops_are_registered_in_every_enumeration() -> None:
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    for op in ("code_intel.freshness", "code_intel.workstate_index"):
        assert op in OP_MODULE_MAP
        assert op in OP_KEY_SCOPE
    assert OP_CLASSIFICATION["code_intel.freshness"] is OpClass.COMPUTE_ONLY
    assert OP_CLASSIFICATION["code_intel.workstate_index"] is OpClass.MUTATING
