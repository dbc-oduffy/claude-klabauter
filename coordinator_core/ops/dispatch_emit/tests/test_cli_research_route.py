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
        "--research-source", "web", "--research-source", "notebooklm",
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
