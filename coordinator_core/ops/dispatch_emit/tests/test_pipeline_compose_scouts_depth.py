"""A scouts-class research emit honours research.depth: deeper runs one Sonnet scout per question, standard keeps the grouped Haiku scouts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research_probe"
_QUESTIONS = 8


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    """A repo whose content root pins the scout stage to Haiku, as the live scouts manifest does."""
    content = tmp_path / "content"
    shutil.copytree(_FIXTURE, content)
    for manifest in content.rglob("scouts.manifest.yaml"):
        text = manifest.read_text(encoding="utf-8")
        head, scout = text.split("  - id: scout", 1)
        manifest.write_text(head + "  - id: scout" + scout.replace("model: sonnet", "model: haiku"), encoding="utf-8")
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-scouts-depth")
    return work


def _emit(repo, capsys, depth):
    flags = ["--research-class", "scouts", "--research-depth", depth]
    for i in range(_QUESTIONS):
        flags += ["--research-question", f"Question number {i} about the subject under research?"]
    rel = _write_sizing(repo, *flags)
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    return Path(reply["path"]).read_text(encoding="utf-8")


def test_deeper_runs_one_sonnet_scout_per_question(repo, capsys):
    script = _emit(repo, capsys, "deeper")
    assert "members" not in script
    assert "model: 'haiku'" not in script
    assert script.count("model: 'sonnet'") >= 1


def test_standard_keeps_grouped_haiku_scouts(repo, capsys):
    script = _emit(repo, capsys, "standard")
    assert "model: 'haiku'" in script
    assert "members" in script


def test_deepest_also_runs_sonnet_scouts(repo, capsys):
    script = _emit(repo, capsys, "deepest")
    assert "model: 'haiku'" not in script
    assert "members" not in script
