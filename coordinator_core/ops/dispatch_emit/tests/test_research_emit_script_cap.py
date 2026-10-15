"""The 3-source research emit stays under the Workflow script cap, and template text is emitted once however many items fan out."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import WORKFLOW_SCRIPT_CAP_BYTES
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_doe_manifests import _doe_content_root

VENDORED = Path(__file__).parent / "fixtures" / "pipeline_research"
_MIN_LINE = 30
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


def _emit(monkeypatch, tmp_path, capsys, content_root, sources, count, *, questions=True):
    """Emit a corpus research run over `sources` with `count` notebooklm targets (and questions); return (script, reply)."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content_root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research-cap")
    flags = ["--research-class", "corpus", "--research-depth", "deepest"]
    for source in sources:
        flags += ["--research-source", source]
    for i in range(count):
        flags += ["--research-target", f"notebooklm=T{i}"]
        if questions:
            flags += ["--research-question", f"Question number {i} about the subject under research?"]
    rel = _write_sizing(tmp_path, *flags)
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    return Path(reply["path"]).read_text(encoding="utf-8"), reply


def _template_markers(content_root, pipelines):
    """Per template, its longest literal run (a line split at `{{...}}` placeholders) of at least `_MIN_LINE` characters."""
    markers = {}
    for pipeline in pipelines:
        manifest = load_manifest(content_root, pipeline)
        for name, text in manifest.templates.items():
            lines = [
                run.strip() for ln in text.splitlines() for run in _PLACEHOLDER.split(ln) if len(run.strip()) >= _MIN_LINE
            ]
            if lines:
                markers[f"{pipeline}/{name}"] = max(lines, key=len)
    return markers


def _assert_invariant(monkeypatch, tmp_path, capsys, content_root, sources):
    one, reply_one = _emit(monkeypatch, tmp_path / "one", capsys, content_root, sources, 1)
    many, _ = _emit(monkeypatch, tmp_path / "many", capsys, content_root, sources, 8)
    markers = _template_markers(content_root, reply_one["pipelines"])
    assert markers
    drift = {k: (one.count(m), many.count(m)) for k, m in markers.items() if one.count(m) != many.count(m)}
    assert not drift, f"template text scales with item count (1 item, 8 items): {drift}"


def test_the_does_live_three_source_emit_is_under_the_workflow_script_cap(monkeypatch, tmp_path, capsys):
    root = _doe_content_root()
    if root is None:
        pytest.skip("coordinator-content-repo tree not present")
    script, reply = _emit(monkeypatch, tmp_path, capsys, root, ["web", "repo", "notebooklm"], 8)
    assert {"web", "notebooklm"} <= set(reply["pipelines"]) and any("repo" in p for p in reply["pipelines"])
    size = len(script.encode("utf-8"))
    assert size < WORKFLOW_SCRIPT_CAP_BYTES, f"3-source script is {size} bytes, cap {WORKFLOW_SCRIPT_CAP_BYTES}"


def test_template_text_is_emitted_once_whatever_the_item_count_on_the_vendored_fixture(monkeypatch, tmp_path, capsys):
    _assert_invariant(monkeypatch, tmp_path, capsys, VENDORED, ["web", "notebooklm"])


def test_template_text_is_emitted_once_whatever_the_item_count_on_does_live(monkeypatch, tmp_path, capsys):
    root = _doe_content_root()
    if root is None:
        pytest.skip("coordinator-content-repo tree not present")
    _assert_invariant(monkeypatch, tmp_path, capsys, root, ["web", "repo", "notebooklm"])
