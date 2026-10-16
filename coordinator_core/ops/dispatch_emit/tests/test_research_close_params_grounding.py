"""The research route sets require_grounding on the scouts next_action only when the scouts manifest runs a grounding stage."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"
_ARGV = ["--research", "--ask", "Q one?"]
_STAGE = """  - id: grounding
    phase: Ground
    agent_type: general-purpose
    model: sonnet
    template: grounding-template.md
    depends_on: [scout]
    web_caller: true
    output: "{{scratch_dir}}/grounding.md"
"""


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


def _setup(monkeypatch, tmp_path, root):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-grounding-params")


def _script(capsys) -> str:
    assert cli_module.main(_ARGV) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"]
    return Path(reply["path"]).read_text(encoding="utf-8")


def test_require_grounding_is_set_when_the_manifest_runs_a_grounding_stage(monkeypatch, tmp_path, capsys):
    root = tmp_path / "content"
    shutil.copytree(_FIXTURE, root)
    pdir = root / "pipelines" / "deep-research"
    manifest = pdir / "scouts.manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").rstrip("\n") + "\n" + _STAGE, encoding="utf-8")
    (pdir / "grounding-template.md").write_text("Ground the digests.\n", encoding="utf-8")
    _setup(monkeypatch, tmp_path, root)
    assert '"require_grounding":true' in _script(capsys).replace(" ", "")


def test_require_grounding_is_absent_without_a_grounding_stage(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _FIXTURE)
    assert '"require_grounding"' not in _script(capsys)
