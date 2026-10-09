"""
coordinator_core.hooks.tests.test_context_pressure_precompact — tests for the
`## Active Workflow Runs` state-snapshot section
(`_build_workflow_runs_section`/`_render_resume_call`) added to carry a
persisted `Workflow` run-id capture across a `/compact` that killed the
background run's own session state.

Spec backlink: state/cross-repo/inbox/2026-09-25-coordinator-content-repo-em-mise-workflow-run-id-across-compaction.md
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


# ---------------------------------------------------------------------------
# _build_git_section: zero-spawn reads of branch, log and staged paths.
# ---------------------------------------------------------------------------


def _git(repo, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_build_git_section_reads_repo_without_spawning(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feature")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first subject\n\nbody")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess.Popen called")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    lines = cpp._build_git_section(str(repo))

    assert lines[:4] == ["", "## Git State", "Branch: feature", "Recent commits:"]
    assert lines[4].endswith(" first subject") and len(lines[4].split()[0]) == 10
    assert lines[5:] == [
        "",
        "Modified files:",
        cpp._UNSTAGED_NOT_LISTED,
        "Staged files:",
        "b.txt",
    ]


def test_build_git_section_outside_repo(tmp_path):
    assert cpp._build_git_section(str(tmp_path)) == [
        "", "## Git State", "(not a git repository)",
    ]


# ---------------------------------------------------------------------------
# Compaction steering: run() returns the summarizer instructions.
# ---------------------------------------------------------------------------


def _session_repo(tmp_path, sid, *, notes=None, turns=0):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True)
    (sdir / "baton.json").write_text(
        json.dumps({"session_id": sid, "carry_forward": notes or []}), encoding="utf-8"
    )
    if turns:
        (sdir / "pm_turns.jsonl").write_text(
            "".join(json.dumps({"turn": i, "prompt": f"p{i}"}) + "\n" for i in range(turns)),
            encoding="utf-8",
        )
    return repo, sdir


def test_steering_points_at_journal_and_keeps_carry_forward(tmp_path):
    repo, sdir = _session_repo(tmp_path, SESSION, notes=["next: wire the shim"], turns=3)

    text = cpp.build_steering(SESSION, str(repo))

    assert text.startswith(cpp._STEERING_RULES)
    assert f"({3} turns) are saved at {sdir / 'pm_turns.jsonl'}" in text
    assert str(sdir / "baton.json") in text
    assert "  - next: wire the shim" in text


def test_steering_defers_to_typed_instructions(tmp_path):
    repo, _ = _session_repo(tmp_path, SESSION)
    text = cpp.build_steering(SESSION, str(repo), typed="focus on the parser")
    assert "the PM's instructions above take precedence" in text


def test_steering_flattens_and_bounds_carry_forward(tmp_path):
    notes = [f"n{i}" for i in range(10)] + ["## FAKE\nHEADER" + "x" * 500]
    repo, _ = _session_repo(tmp_path, SESSION, notes=notes)

    text = cpp.build_steering(SESSION, str(repo))

    assert "  - n5\n" not in text and "  - n6\n" in text
    assert "\n## FAKE" not in text
    assert len(text) <= cpp._STEERING_CHAR_CAP


def test_steering_falls_back_to_static_rules_without_journal(tmp_path):
    assert cpp.build_steering(SESSION, str(tmp_path)) == cpp._STEERING_RULES


def test_steering_survives_corrupt_baton(tmp_path):
    repo, sdir = _session_repo(tmp_path, SESSION)
    (sdir / "baton.json").write_text("{nope", encoding="utf-8")
    text = cpp.build_steering(SESSION, str(repo))
    assert str(sdir / "baton.json") in text


@pytest.mark.parametrize("raw", ["", "not json", "[]", json.dumps({"session_id": "../x"})])
def test_run_returns_static_rules_on_unusable_payload(raw):
    assert cpp.run(raw) == cpp._STEERING_RULES


def test_run_returns_journal_steering(tmp_path, monkeypatch):
    repo, sdir = _session_repo(tmp_path, SESSION, turns=1)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(repo)

    text = cpp.run(json.dumps({"session_id": SESSION, "trigger": "auto"}))

    assert str(sdir / "pm_turns.jsonl") in text
    assert (tmp_path / f"compaction-occurred-{SESSION}").is_file()
