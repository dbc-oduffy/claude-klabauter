"""Research route: `scout_questions` / `scout_mode` params drive per-question scouts and the reply's targets."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research_probe"


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research")
    return tmp_path


_QS = [
    "What does the web say?",
    {"text": "How does a work?", "target": "repo", "ref": "src/a.py"},
    {"text": "Is tool present?", "target": "local", "ref": "claude-json-keys"},
]


def _setup(repo, **extra):
    params = {"scout_questions": _QS, **extra}
    ctx, _, _ = op_module._research_route_setup(params, repo, None, "Feasibility ask")
    return ctx


def test_split_gives_one_slug_per_question_and_reports_targets(repo):
    ctx = _setup(repo)
    shape = ctx["shape"]
    slugs = shape["segments"][0]["lists"]["questions"]
    assert len(slugs) == 3 and "question" not in slugs
    assert shape["scout_mode"] == "split"
    assert shape["scout_targets"] == [
        {"slug": slugs[0], "targets": [{"target": "web", "ref": ""}]},
        {"slug": slugs[1], "targets": [{"target": "repo", "ref": "src/a.py"}]},
        {"slug": slugs[2], "targets": [{"target": "local", "ref": "claude-json-keys"}]},
    ]
    ask = (repo / ctx["brief"]).read_text(encoding="utf-8")
    assert "Target: repo `src/a.py`" in ask and "Target: local `claude-json-keys`" in ask


def test_combine_gives_one_combined_scout(repo):
    ctx = _setup(repo, scout_mode="combine")
    shape = ctx["shape"]
    assert shape["segments"][0]["lists"]["questions"] == ["question"]
    assert shape["scout_mode"] == "combine"
    assert [t["slug"] for t in shape["scout_targets"]] == ["question"] * 3
    assert "## question" in (repo / ctx["brief"]).read_text(encoding="utf-8")


def test_mode_or_targets_change_the_run_id(repo):
    assert _setup(repo)["run_id"] != _setup(repo, scout_mode="combine")["run_id"]


def test_bare_ask_reports_no_targets(repo):
    ctx, _, _ = op_module._research_route_setup({}, repo, None, "What is X?")
    assert ctx["shape"]["scout_targets"] == [] and ctx["shape"]["scout_mode"] == "split"


def test_unknown_mode_is_refused(repo):
    with pytest.raises(PipelineEmitRefused, match="scout_mode"):
        _setup(repo, scout_mode="both")


def test_scout_questions_with_lists_questions_is_refused(repo):
    with pytest.raises(PipelineEmitRefused, match="lists.questions"):
        _setup(repo, lists={"questions": ["q"]})


def test_scout_questions_with_from_sizing_is_refused(repo):
    with pytest.raises(PipelineEmitRefused, match="from_sizing"):
        op_module._research_route_setup({"scout_questions": _QS}, repo, "some/sizing.yaml", None)


def test_bad_target_is_refused_naming_the_question(repo):
    bad = [{"text": "q", "target": "repo", "ref": "no/such/path"}]
    with pytest.raises(PipelineEmitRefused, match="scout question 1"):
        _setup(repo, scout_questions=bad)
