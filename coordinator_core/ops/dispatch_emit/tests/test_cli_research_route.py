"""CLI research route: --from-sizing and --research --ask emit one chained script through the real op."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
import coordinator_core.sizing_assemble as sa
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.session import record_homes

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"
_NAME = "2026-10-08-r.yaml"
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research")
    return tmp_path


def _write_sizing(repo: Path, *flags: str) -> str:
    home = Path(record_homes.home_dir(str(repo), "sizings"))
    home.mkdir(parents=True, exist_ok=True)
    (home / _NAME).write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, *flags]) == sa.EXIT_OK
    return rel


def _run(capsys, argv):
    code = cli_module.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip().startswith("{") else None


def test_from_sizing_emits_a_chain_with_halts_and_brief(repo, capsys):
    rel = _write_sizing(
        repo, "--research-class", "corpus", "--research-appetite", "medium",
        "--research-source", "web", "--research-source", "notebooklm", "--research-target", "notebooklm=A",
    )
    capsys.readouterr()
    code, reply = _run(capsys, ["--from-sizing", rel])
    assert code == cli_module.EXIT_OK
    assert reply["ok"] and reply["tier"] == "corpus"
    assert reply["pipelines"] == ["nlm-preflight", "web", "notebooklm"]
    assert "corpus" in reply["reason"]
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert "halted: 'nlm-preflight." in script
    assert rel in script
    receipt = json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))
    assert receipt["tier"] == "corpus" and receipt["pipelines"] == reply["pipelines"]
    close = reply["next_action"]
    assert close["op"] == "research.close"
    assert close["params"] == {
        "scratch_dir": (repo / reply["scratch_dir"]).as_posix(),
        "tier": "corpus",
        "run_id": reply["run_id"],
        "topic_slug": "r",
    }
    assert f"next_action: {json.dumps(close, sort_keys=True)}" in script


def test_from_sizing_honours_a_given_list(repo, capsys):
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "notebooklm")
    ctx, _, _ = op_module._research_route_setup(
        {"lists": {"notebooks": ["A", "B"]}}, repo, rel, None
    )
    bound = {m.pipeline: i.lists for m, i, _ in ctx["segments"]}
    assert bound["notebooklm"]["notebooks"] == ("A", "B")
    assert "notebooks" not in bound["nlm-preflight"]


def test_a_notebooklm_sizing_with_no_notebooks_names_both_routes(repo, capsys):
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "notebooklm")
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_DATA_ERROR
    err = capsys.readouterr().err
    assert "--research-target notebooklm=" in err and "--list notebooks=" in err


def test_research_ask_writes_ask_md_and_binds_it_as_the_brief(repo, capsys):
    code, reply = _run(
        capsys, ["--research", "--ask", "What is X?", "--list", "questions=What is X?"]
    )
    assert code == cli_module.EXIT_OK
    assert reply["tier"] == "scouts" and reply["pipelines"] == ["scouts"]
    ask = repo / reply["scratch_dir"] / "ask.md"
    assert ask.is_file() and "What is X?" in ask.read_text(encoding="utf-8")
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert f"{reply['scratch_dir']}/ask.md" in script
    assert "what-is-x" in script
    assert reply["next_action"]["params"]["topic_slug"] == "what-is-x"
    assert reply["next_action"]["params"]["tier"] == "scouts"


def test_research_ask_spawns_no_process(repo, capsys, monkeypatch):
    spawns: list[str] = []

    def counter(name):
        def count(*args, **kwargs):
            spawns.append(name)
            raise AssertionError(f"{name} called on the research route")
        return count

    monkeypatch.setattr(subprocess, "Popen", counter("subprocess.Popen"))
    for attr in ("posix_spawn", "fork"):
        if hasattr(os, attr):
            monkeypatch.setattr(os, attr, counter(f"os.{attr}"))
    code, reply = _run(capsys, ["--research", "--ask", "What is X?"])
    assert code == cli_module.EXIT_OK and reply["ok"]
    assert spawns == []


def test_sizing_without_research_block_is_refused(repo, capsys):
    rel = _write_sizing(repo)
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_DATA_ERROR
    assert "no research block" in capsys.readouterr().err


def test_research_without_ask_is_a_usage_error(repo):
    assert cli_module.main(["--research"]) == cli_module.EXIT_USAGE
    assert cli_module.main(["--research", "--ask"]) == cli_module.EXIT_USAGE


@pytest.mark.parametrize(
    "extra",
    [["--ask", "q"], ["--plan", "p.md"], ["--inventory", "i.md"], ["--sizing", "s.yaml"],
     ["--pipeline", "p"], ["--fire"], ["--writes", "a"]],
)
def test_from_sizing_is_exclusive_of_other_selectors(repo, extra):
    assert cli_module.main(["--from-sizing", "s.yaml", *extra]) == cli_module.EXIT_USAGE


def test_three_scout_questions_are_refused(repo, capsys):
    argv = ["--research", "--ask", "t", "--list", "questions=a,b,c"]
    assert cli_module.main(argv) == cli_module.EXIT_DATA_ERROR


def test_context_file_is_named_in_the_brief_and_needs_a_research_route(repo, capsys):
    (repo / "notes.md").write_text("n\n", encoding="utf-8")
    code, reply = _run(capsys, ["--research", "--ask", "What is X?", "--context", "notes.md"])
    assert code == cli_module.EXIT_OK
    assert "- `notes.md`" in (repo / reply["scratch_dir"] / "ask.md").read_text(encoding="utf-8")
    assert cli_module.main(["--pipeline", "scouts", "--context", "notes.md"]) == cli_module.EXIT_USAGE


def _content_root() -> Path:
    from coordinator_core.conftest import _REAL_CONTENT_ROOT

    content = op_module.content_root_for(_REAL_CONTENT_ROOT) if _REAL_CONTENT_ROOT else None
    if not (content and (Path(content) / "pipelines").is_dir()):
        pytest.skip("coordinator-content-repo tree not present")
    return Path(content)


def test_a_repo_and_web_corpus_sizing_emits_with_the_scope_templates_lists(repo, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", _content_root)
    rel = _write_sizing(
        repo, "--research-class", "corpus", "--research-source", "repo", "--research-source", "web",
        "--research-target", "repo=C:/some/repo",
    )
    ctx, _, _ = op_module._research_route_setup({}, repo, rel, None)
    segments = {s["pipeline"]: s for s in ctx["shape"]["segments"]}
    assert segments["repo"]["lists"] == {"chunks": ["A", "B", "C", "D"], "haiku_scouts": ["1", "2"]}
    assert segments["web"]["lists"] == {"topics": ["a", "b", "c", "d"]}


def test_a_flag_reaches_the_pipeline_that_declares_it_and_an_unclaimed_one_is_refused(repo, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", _content_root)
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "repo")
    ctx, _, _ = op_module._research_route_setup({"flags": {"compare": "true"}}, repo, rel, None)
    assert ctx["shape"]["segments"][0]["flags"] == {"compare": "true"}
    with pytest.raises(op_module.PipelineEmitRefused) as exc:
        op_module._research_route_setup({"flags": {"nope": "true"}}, repo, rel, None)
    assert any("--flag nope" in r for r in exc.value.reasons)


def test_every_segments_refusals_arrive_together(repo):
    rel = _write_sizing(
        repo, "--research-class", "corpus", "--research-source", "web", "--research-source", "notebooklm",
    )
    with pytest.raises(op_module.PipelineEmitRefused) as exc:
        op_module._research_route_setup({"flags": {"nope": "1"}}, repo, rel, None)
    reasons = exc.value.reasons
    assert any("needs its notebooks" in r for r in reasons) and any("--flag nope" in r for r in reasons)


def test_sizing_questions_seed_web_topics(repo, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", _content_root)
    rel = _write_sizing(
        repo, "--research-class", "corpus", "--research-source", "web",
        "--research-question", "One?", "--research-question", "Two?", "--research-question", "Three?",
    )
    ctx, _, _ = op_module._research_route_setup({}, repo, rel, None)
    assert ctx["shape"]["segments"][0]["lists"] == {"topics": ["a", "b", "c"]}
