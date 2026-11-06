"""Research runs state their synthesis and advisory paths under their own scratch dir, never scratch/warp/."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"


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


def _emit(capsys, ask):
    assert cli_module.main(["--research", "--ask", ask]) == cli_module.EXIT_OK
    return json.loads(capsys.readouterr().out)


def _output_section(repo: Path, reply: dict) -> str:
    text = (repo / reply["scratch_dir"] / "ask.md").read_text(encoding="utf-8")
    assert "## Output paths" not in text
    return text.split("Output paths:", 1)[1]


def test_two_default_runs_state_distinct_per_run_output_paths(repo, capsys):
    first = _emit(capsys, "What is X?")
    second = _emit(capsys, "What is Y?")
    assert first["scratch_dir"] != second["scratch_dir"]
    seen = []
    for reply in (first, second):
        section = _output_section(repo, reply)
        synthesis = f"{reply['scratch_dir']}/synthesis.md"
        advisory = f"{reply['scratch_dir']}/outputs/advisory.md"
        assert f"`{synthesis}`" in section and f"`{advisory}`" in section
        seen += [synthesis, advisory]
    assert len(set(seen)) == 4
    assert not any(p in ("scratch/warp/synthesis.md", "scratch/warp/outputs/advisory.md") for p in seen)


def test_sizing_brief_is_pointed_at_not_edited(tmp_path):
    (tmp_path / "sizing.yaml").write_text("intent: x\n", encoding="utf-8")
    brief = research_emit.bind_output_paths(tmp_path, "scratch/warp/run-1", "sizing.yaml")
    assert brief == "scratch/warp/run-1/brief.md"
    text = (tmp_path / brief).read_text(encoding="utf-8")
    assert "`sizing.yaml`" in text and "`scratch/warp/run-1/synthesis.md`" in text
    assert (tmp_path / "sizing.yaml").read_text(encoding="utf-8") == "intent: x\n"


def test_docs_research_workdir_keeps_the_run_dir_default(tmp_path):
    scratch = "docs/research/topic-a/2026-11-03-some-run/workdir"
    assert research_emit.bind_output_paths(tmp_path, scratch, f"{scratch}/ask.md") == f"{scratch}/ask.md"
    assert not (tmp_path / scratch).exists()


@pytest.mark.parametrize("sizing_rel, stem", [
    ("state/sizing/topic.yaml", "topic"),
    ("state/sizing/warp-topic-20261106T002835-2971dd.yaml", "topic"),
    ("state/sizing/topic-20261106T002835-2971dd.yaml", "topic"),
])
def test_unstamped_stem_carries_at_most_one_run_stamp(sizing_rel, stem):
    assert op_module._unstamped_stem(sizing_rel) == stem
