"""compose_execute_review carries the UE engine-facts clause only for a repo served by the ue-addon."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ue_engine_facts as uef
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment


def _fake_enabled(root: Path) -> bool:
    return any(p.suffix.lower() == ".uproject" for p in Path(root).iterdir() if p.is_file())


@pytest.fixture
def addon(monkeypatch):
    pkg = types.ModuleType("project_rag_ue_addon")
    mod = types.ModuleType("example_retrieval_repo_ue_addon.tooling_opt_in")
    mod.ue_tooling_enabled = _fake_enabled
    monkeypatch.setitem(sys.modules, "project_rag_ue_addon", pkg)
    monkeypatch.setitem(sys.modules, "example_retrieval_repo_ue_addon.tooling_opt_in", mod)
    monkeypatch.setattr(uef.importlib.util, "find_spec", lambda name: object())


def _repo(tmp_path: Path, *, index: bool, uproject: bool) -> Path:
    if index:
        (tmp_path / ".project-rag").mkdir()
        (tmp_path / ".project-rag" / "state.json").write_text("{}", encoding="utf-8")
    if uproject:
        (tmp_path / "Foo.uproject").write_text("{}", encoding="utf-8")
    return tmp_path


def _briefs(repo_root: str | None) -> str:
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        prompt_head="BRIEF PRECEDENCE CLAUSE",
        repo_root=repo_root,
    )
    return "\n".join(block for _title, block in phases)


def test_served_repo_reviewer_briefs_carry_clause(tmp_path, addon):
    out = _briefs(str(_repo(tmp_path, index=True, uproject=True)))
    assert "your training data on UE5 APIs is stale" in out
    assert "example_retrieval_repo_instructions" in out
    assert "BRIEF PRECEDENCE CLAUSE" in out


def test_clause_reaches_every_reviewer_call(tmp_path, addon):
    served = _briefs(str(_repo(tmp_path, index=True, uproject=True)))
    plain = _briefs(None)
    assert served.count("your training data on UE5 APIs is stale") >= plain.count("BRIEF PRECEDENCE CLAUSE") >= 3


@pytest.mark.parametrize("index,uproject", [(False, True), (True, False), (False, False)])
def test_unserved_repo_briefs_do_not(tmp_path, addon, index, uproject):
    out = _briefs(str(_repo(tmp_path, index=index, uproject=uproject)))
    assert "UE5 APIs is stale" not in out
    assert "BRIEF PRECEDENCE CLAUSE" in out
