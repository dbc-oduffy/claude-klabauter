"""Tests for the PreToolUse(Workflow) args-file expansion body."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.distill import launch_args as la
from coordinator_core.hooks.preuse_workflow_args_inline import handle


def _built(tmp_path):
    payload = {
        "runId": "r",
        "repoRoot": str(tmp_path),
        "batches": [{"batchId": "batch-1", "files": ["a.md"], "description": "d", "formatHints": {}}],
        "wikiDirs": ["docs/wiki"],
        "wikiSlugs": {"g": "docs/wiki/g.md"},
        "batch_count": 1,
        "total_file_count": 1,
        "wikiSlugCount": 1,
    }
    return la.build(payload, run_dir=tmp_path / "run")


def _call(args, extra=None):
    return handle({"tool_name": "Workflow", "tool_input": {"scriptPath": "s.js", "args": args, **(extra or {})}})


def test_stub_args_are_rewritten_to_the_file_content_verbatim(tmp_path):
    built = _built(tmp_path)

    out = _call(la.stub(built["path"], built["sha256"]))

    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["args"] == built["args"]
    assert updated["scriptPath"] == "s.js"
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_relative_args_file_resolves_against_cwd(tmp_path):
    built = _built(tmp_path)
    out = handle({
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "tool_input": {"args": la.stub("run/launch-args.json", built["sha256"])},
    })
    assert out["hookSpecificOutput"]["updatedInput"]["args"] == built["args"]


def test_sha_mismatch_leaves_the_call_unchanged_with_an_advisory_and_no_deny(tmp_path):
    built = _built(tmp_path)

    out = _call(la.stub(built["path"], "0" * 64))

    hso = out["hookSpecificOutput"]
    assert "updatedInput" not in hso and "permissionDecision" not in hso
    assert "sha256 mismatch" in hso["additionalContext"]


def test_missing_file_is_an_advisory_not_a_deny(tmp_path):
    out = _call(la.stub(str(tmp_path / "nope.json"), "0" * 64))
    hso = out["hookSpecificOutput"]
    assert "updatedInput" not in hso and "permissionDecision" not in hso
    assert "unreadable" in hso["additionalContext"]


def test_file_edited_after_build_no_longer_expands(tmp_path):
    built = _built(tmp_path)
    (tmp_path / "run" / la.ARGS_FILE_NAME).write_text(json.dumps({"tampered": True}), encoding="utf-8")
    assert "updatedInput" not in _call(la.stub(built["path"], built["sha256"]))["hookSpecificOutput"]


def test_other_call_shapes_are_silent(tmp_path):
    built = _built(tmp_path)
    assert _call({"runId": "typed-by-hand"}) == {}
    assert _call(None) == {}
    assert _call({"argsFile": built["path"]}) == {}
    assert handle({"tool_name": "Bash", "tool_input": {"args": la.stub(built["path"], built["sha256"])}}) == {}
    assert handle({"tool_name": "Workflow", "tool_input": "x"}) == {}


def test_hook_spawns_no_process(tmp_path, monkeypatch):
    built = _built(tmp_path)

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert "updatedInput" in _call(la.stub(built["path"], built["sha256"]))["hookSpecificOutput"]
