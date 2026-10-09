"""dispatch.emit pipeline branch: params, defaults, refusal routing, receipt extras, reply.

load_manifest, validate and compose_pipeline_script are stubbed through sys.modules, so these
tests hold the op wiring only; the real emission is proven by the parity, hook and budget tests.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import PipelineParamConflictError, _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FanOut, Manifest, PipelineEmitRefused, Schedule,
)

_SCRIPT = "export const meta = { name: 'p', phases: [{ title: 'a' }] };\nreturn {};\n"
_PKG = "coordinator_core.ops.dispatch_emit"


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    seen: dict = {}
    manifest = Manifest("structured", tmp_path, {}, (), {}, {}, "m" * 64)
    schedule = Schedule({}, {})

    def load_manifest(content_root, pipeline):
        seen["content_root"] = content_root
        seen["pipeline"] = pipeline
        return manifest

    def validate(m, inputs):
        seen["inputs"] = inputs
        if seen.get("refuse"):
            raise PipelineEmitRefused(["bad token {{x}}"])
        return schedule

    def compose(m, inputs, s, *, run_id, agent_type_host):
        seen["run_id"] = run_id
        return _SCRIPT

    monkeypatch.setitem(sys.modules, f"{_PKG}.pipeline_manifest",
                        types.SimpleNamespace(load_manifest=load_manifest, validate=validate))
    monkeypatch.setitem(sys.modules, f"{_PKG}.pipeline_compose",
                        types.SimpleNamespace(compose_pipeline_script=compose))
    content = tmp_path / "doe-content"
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content)
    seen["doe_content"] = content
    return seen


def _params(tmp_path, **extra):
    (tmp_path / "brief.md").write_text("research X", encoding="utf-8")
    return {"pipeline": "structured", "brief": "brief.md", "target_root": str(tmp_path),
            "session_id": "sess-1", **extra}


def _written(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*") if p.is_file() and p.name != "brief.md")


def test_emit_writes_script_and_receipt_with_extras(stubs, tmp_path):
    out = tmp_path / "out.mjs"
    reply = _dispatch_emit(_params(tmp_path, subjects=["a", "b"], output_path=str(out)))

    assert _written(tmp_path) == ["_em.jsonl", "out.mjs", "out.mjs.emitted.json"]
    receipt = json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert receipt["plan"] is None
    assert (receipt["pipeline"], receipt["manifest_sha256"], receipt["subjects"]) == (
        "structured", "m" * 64, ["a", "b"])
    assert receipt["run_id"] == reply["run_id"] == stubs["run_id"]
    assert stubs["content_root"] == stubs["doe_content"]
    assert stubs["pipeline"] == "structured"


def test_defaults_are_unique_per_run_and_under_the_warp_dir(stubs, tmp_path):
    first = _dispatch_emit(_params(tmp_path))
    second = _dispatch_emit(_params(tmp_path))

    assert first["run_id"] != second["run_id"]
    assert first["run_id"].startswith("pipeline-structured-")
    assert Path(first["path"]) == (tmp_path / "scratch/warp" / f"{first['run_id']}.workflow.mjs").resolve()
    assert first["scratch_dir"] == f"scratch/warp/{first['run_id']}"
    assert first["fire_args"] == {"repoRoot": tmp_path.as_posix()}


def test_caller_scratch_dir_is_guarded_under_the_root(stubs, tmp_path):
    reply = _dispatch_emit(_params(tmp_path, scratch_dir="state/mine"))
    assert reply["scratch_dir"] == "state/mine"
    assert stubs["inputs"].scratch_dir == "state/mine"

    with pytest.raises(op_module.PathEscapeError):
        _dispatch_emit(_params(tmp_path, scratch_dir="../elsewhere"))


def test_inputs_carry_brief_subjects_and_flags(stubs, tmp_path):
    (tmp_path / "brief.md").write_text("research X", encoding="utf-8")
    _dispatch_emit(_params(tmp_path, subjects=["a"], flags={"depth": "deep"}))
    inputs = stubs["inputs"]
    assert (inputs.brief, inputs.subjects, dict(inputs.flags)) == ("brief.md", ("a",), {"depth": "deep"})


@pytest.mark.parametrize(
    "other",
    [{"plan_path": "p.md"}, {"inventory_path": "i.md"}, {"queue": ["q"]}, {"profile": "g"},
     {"ask": "do it"}, {"sizing_path": "s.yaml"}, {"cloud_spawn": {}}],
)
def test_pipeline_with_another_route_selector_conflicts(stubs, tmp_path, other):
    with pytest.raises(PipelineParamConflictError):
        _dispatch_emit(_params(tmp_path, **other))
    assert _written(tmp_path) == []


def test_missing_brief_path_is_refused(stubs, tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        _dispatch_emit(_params(tmp_path, brief="nope/brief.md"))
    assert _written(tmp_path) == []


@pytest.mark.parametrize("brief", ["", "   ", None])
def test_empty_brief_is_refused(stubs, tmp_path, brief):
    with pytest.raises(ValueError, match="brief"):
        _dispatch_emit(_params(tmp_path, brief=brief))


def test_validate_refusal_propagates_with_nothing_written(stubs, tmp_path):
    stubs["refuse"] = True
    with pytest.raises(PipelineEmitRefused, match="bad token"):
        _dispatch_emit(_params(tmp_path))
    assert _written(tmp_path) == []


def test_empty_root_pointer_refuses_naming_the_root_and_writes_nothing(stubs, tmp_path, monkeypatch):
    monkeypatch.setattr(op_module, "read_content_root", lambda: "")
    with pytest.raises(PipelineEmitRefused, match="read_content_root"):
        _dispatch_emit(_params(tmp_path))
    assert _written(tmp_path) == []


def test_unresolvable_content_root_refuses_naming_the_root_and_writes_nothing(stubs, tmp_path, monkeypatch):
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: None)
    with pytest.raises(PipelineEmitRefused, match="doe"):
        _dispatch_emit(_params(tmp_path))
    assert _written(tmp_path) == []


def test_route_needs_a_root(stubs, tmp_path):
    with pytest.raises(ValueError, match="repo_root or target_root"):
        _dispatch_emit({"pipeline": "structured", "brief": "b"})


def test_a_research_manifest_fired_by_name_closes_through_research_close(stubs, tmp_path):
    # addon-28: `--pipeline notebooklm` returned no next_action, so the close was hand-routed.
    reply = _dispatch_emit(_params(tmp_path, pipeline="notebooklm"))
    params = reply["next_action"]["params"]
    assert reply["next_action"]["op"] == "research.close"
    assert params["tier"] == "corpus" and params["run_id"] == reply["run_id"] and params["topic_slug"] == "brief"
    assert params["scratch_dir"] == (tmp_path / reply["scratch_dir"]).as_posix()
    assert "next_action" not in _dispatch_emit(_params(tmp_path))


def test_the_emit_creates_the_em_mailbox_and_names_its_tail(stubs, tmp_path):
    # example-game-repo-d2: break-class finds sat ~10 min in peer mailboxes with no channel up to the EM.
    reply = _dispatch_emit(_params(tmp_path))
    box = Path(reply["em_mailbox"])
    assert box == tmp_path / reply["scratch_dir"] / "mail" / "_em.jsonl"
    assert box.is_file() and box.read_bytes() == b""
    assert reply["em_watch"].endswith(box.as_posix())
