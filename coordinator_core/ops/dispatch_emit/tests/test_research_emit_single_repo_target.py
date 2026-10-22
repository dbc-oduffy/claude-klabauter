"""A one-repo-target research sizing with questions and no seeds names its target to the scope stage; zero resolved repo targets refuse."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _Admitted, _write_sizing

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "content"
    for name in ("pipeline_research", "pipeline_research_multi_repo"):
        shutil.copytree(_FIXTURES / name, root, dirs_exist_ok=True)
    return root


@pytest.fixture
def repo(monkeypatch, tmp_path, content_root):
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content_root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-single-repo")
    return work


def _questions() -> list[str]:
    flags = []
    for n in range(1, 6):
        flags += ["--research-question", f"Question {n}?"]
    return flags


def test_one_repo_target_with_questions_reaches_the_scope_brief(repo, tmp_path):
    target = (tmp_path / "research" / "kythe").as_posix()
    rel = _write_sizing(
        repo, "--research-class", "corpus", "--research-source", "repo", "--research-depth", "deeper",
        "--research-target", f"repo={target}", *_questions(),
    )
    ctx, _, _ = op_module._research_route_setup({}, repo, rel, None)
    repo_inputs = next(i for m, i, _ in ctx["segments"] if m.pipeline == "repo")
    brief = (repo / repo_inputs.brief).read_text(encoding="utf-8")
    assert target in brief
    assert "depth: deeper" in brief
    assert "Question 5?" in brief


def test_write_ask_renders_depth_and_repo_targets_only(tmp_path):
    research = {
        "depth": "deeper",
        "targets": [{"source": "repo", "ref": "/a/one"}, {"source": "web", "ref": "https://example.com"}],
    }
    rel = re_.write_ask(tmp_path, "scratch/run", "Topic", ["Q one?"], research=research)
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "repo target: /a/one" in text and "depth: deeper" in text
    assert "example.com" not in text and "Q one?" in text


def test_write_ask_without_a_research_block_is_unchanged(tmp_path):
    rel = re_.write_ask(tmp_path, "scratch/run", "Topic", ["Q one?"])
    assert "## Research" not in (tmp_path / rel).read_text(encoding="utf-8")


def test_zero_resolved_repo_targets_refuse_naming_the_sizing(repo):
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "repo", *_questions())
    with pytest.raises(PipelineEmitRefused) as exc:
        op_module._research_route_setup({}, repo, rel, None)
    assert any(
        rel in r and "no resolved repo target" in r and "registered key" in r
        for r in exc.value.reasons
    )
