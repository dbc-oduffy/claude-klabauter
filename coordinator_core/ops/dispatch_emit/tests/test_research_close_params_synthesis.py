"""The research emit names every fired segment's synthesis.md in close_params.require_segment_outputs."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _Admitted, _write_sizing

_FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "content"
    shutil.copytree(_FIX / "pipeline_research", root)
    shutil.copytree(_FIX / "pipeline_research_multi_repo", root, dirs_exist_ok=True)
    web = root / "pipelines" / "deep-research" / "web.manifest.yaml"
    web.write_text(web.read_text(encoding="utf-8").replace("sweep.md", "synthesis.md"), encoding="utf-8")
    return root


def _emit(monkeypatch, tmp_path, capsys, root, *flags):
    work = tmp_path / "run"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-synthesis")
    rel = _write_sizing(work, *flags)
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    return json.loads(capsys.readouterr().out)["next_action"]["params"]


def test_web_and_repo_emit_names_both_synthesis_files(monkeypatch, tmp_path, capsys, content_root):
    target = (tmp_path / "targets" / "alpha").as_posix()
    params = _emit(
        monkeypatch, tmp_path, capsys, content_root,
        "--research-class", "corpus", "--research-source", "web", "--research-source", "repo",
        "--research-target", f"repo={target}",
    )
    assert sorted(params["require_segment_outputs"]) == ["repo/synthesis.md", "synthesis.md"]


def test_scouts_only_emit_carries_no_key(monkeypatch, tmp_path, capsys, content_root):
    params = _emit(
        monkeypatch, tmp_path, capsys, content_root,
        "--research-class", "corpus", "--research-source", "notebooklm", "--research-target", "notebooklm=A",
    )
    assert "require_segment_outputs" not in params


def test_synthesis_output_keys_on_the_basename(content_root):
    assert research_emit.synthesis_output(load_manifest(content_root, "repo")) == "synthesis.md"
    assert research_emit.synthesis_output(load_manifest(content_root, "scouts")) is None
