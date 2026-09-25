"""
coordinator_core.hooks.tests.test_context_pressure_precompact — tests for the
`## Active Workflow Runs` state-snapshot section
(`_build_workflow_runs_section`/`_render_resume_call`) added to carry a
persisted `Workflow` run-id capture across a `/compact` that killed the
background run's own session state.

Spec backlink: state/cross-repo/inbox/2026-09-25-doe-claude-em-mise-workflow-run-id-across-compaction.md
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import context_pressure_precompact as cpp  # noqa: E402


SESSION = "test-session-precompact-wf"


def _write_record(tmp_path, session_id, task_id, **overrides):
    record = {
        "run_id": "wf_abc123",
        "scriptPath": "docs/plans/some-plan.workflow.mjs",  # abs-path-ok: fixture value, not a real repo path
        "args": [],
        "fired_at": 1234567890,
        "session_id": session_id,
    }
    record.update(overrides)
    path = tmp_path / f"workflow-run-{session_id}-{task_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _render_resume_call
# ---------------------------------------------------------------------------


def test_render_resume_call_with_script_path_and_args():
    call = cpp._render_resume_call(
        {"run_id": "wf_abc123", "scriptPath": "some/plan.workflow.mjs", "args": ["--foo"]}
    )
    assert call == 'Workflow({scriptPath: "some/plan.workflow.mjs", args: ["--foo"], resumeFromRunId: "wf_abc123"})'


def test_render_resume_call_without_args():
    call = cpp._render_resume_call({"run_id": "wf_abc123", "scriptPath": "some/plan.workflow.mjs", "args": []})
    assert call == 'Workflow({scriptPath: "some/plan.workflow.mjs", resumeFromRunId: "wf_abc123"})'


def test_render_resume_call_missing_script_path_names_the_gap():
    call = cpp._render_resume_call({"run_id": "wf_abc123", "scriptPath": None, "args": None})
    assert "wf_abc123" in call
    assert "unknown" in call


def test_render_resume_call_missing_run_id_is_empty():
    assert cpp._render_resume_call({"run_id": None, "scriptPath": "some/plan.workflow.mjs"}) == ""


def test_render_resume_call_escapes_embedded_quote_in_script_path():
    """A scriptPath carrying a literal double quote must not close the
    rendered string early and splice text into the snapshot -- json.dumps
    escapes it in place rather than breaking out (Review: code-reviewer slice
    C, P2)."""
    call = cpp._render_resume_call(
        {"run_id": "wf_abc123", "scriptPath": 'evil"}); FAKE_INJECT', "args": None}
    )
    assert call == 'Workflow({scriptPath: "evil\\"}); FAKE_INJECT", resumeFromRunId: "wf_abc123"})'


def test_render_resume_call_rejects_newline_in_script_path():
    """A newline in scriptPath could inject a fake fresh line (e.g. a forged
    `## ` heading) into the one-line-per-entry state snapshot even through
    json.dumps' escaping -- rejected outright rather than rendered."""
    call = cpp._render_resume_call(
        {"run_id": "wf_abc123", "scriptPath": "a/b.mjs\n## FAKE HEADER", "args": None}
    )
    assert "unknown" in call
    assert "\n" not in call


def test_render_resume_call_rejects_control_char_in_run_id():
    call = cpp._render_resume_call({"run_id": "wf_abc\n123", "scriptPath": "a/b.mjs"})
    assert call == ""


# ---------------------------------------------------------------------------
# _build_workflow_runs_section
# ---------------------------------------------------------------------------


def test_build_workflow_runs_section_renders_persisted_record(tmp_path):
    _write_record(tmp_path, SESSION, "task-1")

    lines = cpp._build_workflow_runs_section(str(tmp_path), SESSION)

    assert "## Active Workflow Runs" in lines
    joined = "\n".join(lines)
    assert "wf_abc123" in joined
    assert "resume: Workflow({scriptPath:" in joined


def test_build_workflow_runs_section_none_when_no_records(tmp_path):
    lines = cpp._build_workflow_runs_section(str(tmp_path), SESSION)
    assert lines == ["", "## Active Workflow Runs", "(none)"]


def test_build_workflow_runs_section_ignores_other_sessions(tmp_path):
    _write_record(tmp_path, "some-other-session", "task-1")

    lines = cpp._build_workflow_runs_section(str(tmp_path), SESSION)

    assert lines == ["", "## Active Workflow Runs", "(none)"]


def test_build_workflow_runs_section_skips_malformed_record(tmp_path):
    path = tmp_path / f"workflow-run-{SESSION}-task-bad.json"
    path.write_text("not json", encoding="utf-8")

    lines = cpp._build_workflow_runs_section(str(tmp_path), SESSION)

    assert lines == ["", "## Active Workflow Runs", "(none)"]


def test_build_workflow_runs_section_no_session_id_is_none(tmp_path):
    lines = cpp._build_workflow_runs_section(str(tmp_path), "")
    assert lines == ["", "## Active Workflow Runs", "(none)"]


# ---------------------------------------------------------------------------
# run() end-to-end: the state snapshot carries the section.
# ---------------------------------------------------------------------------


def test_run_carries_active_workflow_run_into_state_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _write_record(tmp_path, SESSION, "task-1")

    raw = json.dumps({"session_id": SESSION, "transcript_path": ""})
    cpp.run(raw)

    state_path = tmp_path / f"compaction-state-{SESSION}.md"
    assert state_path.is_file()
    content = state_path.read_text(encoding="utf-8")
    assert "## Active Workflow Runs" in content
    assert "wf_abc123" in content
    assert "resume: Workflow({scriptPath:" in content


def test_run_never_raises_when_workflow_run_record_is_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    bad = tmp_path / f"workflow-run-{SESSION}-task-x.json"
    bad.write_text("{not valid json", encoding="utf-8")

    raw = json.dumps({"session_id": SESSION, "transcript_path": ""})
    cpp.run(raw)  # must not raise

    state_path = tmp_path / f"compaction-state-{SESSION}.md"
    assert state_path.is_file()
