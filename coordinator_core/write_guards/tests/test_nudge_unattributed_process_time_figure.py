
from __future__ import annotations

import pytest

from coordinator_core.write_guards import nudge_unattributed_process_time_figure as guard


@pytest.fixture
def _repo(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "bug-backlog").mkdir(parents=True)
    (repo_root / "docs" / "research").mkdir(parents=True)
    (repo_root / "docs" / "plans").mkdir(parents=True)
    (repo_root / ".git").mkdir()
    monkeypatch.chdir(repo_root)
    return repo_root


def _write_payload(file_path: str, content: str, cwd: str, agent_id=None) -> dict:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
        "cwd": cwd,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _edit_payload(file_path: str, old_string: str, new_string: str, cwd: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        },
        "cwd": cwd,
    }


def _multiedit_payload(file_path: str, edits, cwd: str) -> dict:
    return {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": file_path,
            "edits": [
                {"old_string": o, "new_string": n} for (o, n) in edits
            ],
        },
        "cwd": cwd,
    }


def test_fires_on_unattributed_figure_in_bug_backlog(_repo):
    target = str(_repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md")
    payload = _write_payload(
        target, "The process time of 143ms is over budget.", str(_repo)
    )
    result = guard.check(payload)
    assert result is not None
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "additionalContext" in out
    assert "benchmarks.measure" in out["additionalContext"]


def test_attributed_figure_passes_silently(_repo):
    target = str(_repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md")
    content = (
        "process time of 143ms via batched_process_time_ms, well under budget."
    )
    payload = _write_payload(target, content, str(_repo))
    assert guard.check(payload) is None


def test_bar_citation_does_not_fire(_repo):
    target = str(_repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md")
    payload = _write_payload(target, "Stays under the 500ms bar.", str(_repo))
    assert guard.check(payload) is None


def test_path_outside_scoped_prefixes_does_not_fire(_repo):
    target = str(_repo / "docs" / "plans" / "2026-09-23-some-plan.md")
    payload = _write_payload(
        target, "The process time of 143ms is over budget.", str(_repo)
    )
    assert guard.check(payload) is None


def test_subagent_payload_behaves_identically(_repo):
    target = str(_repo / "docs" / "research" / "2026-09-23-figure.md")
    content = "The process time of 143ms is over budget."
    main_result = guard.check(_write_payload(target, content, str(_repo)))
    sub_result = guard.check(
        _write_payload(target, content, str(_repo), agent_id="ab12cd34ef56")
    )
    assert main_result is not None
    assert sub_result is not None
    assert (
        main_result["hookSpecificOutput"]["additionalContext"]
        == sub_result["hookSpecificOutput"]["additionalContext"]
    )


def test_edit_reconstructs_resulting_text_from_pre_image(_repo):
    target = _repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md"
    target.write_text("Baseline notes. old text here.", encoding="utf-8")
    payload = _edit_payload(
        str(target),
        "old text here.",
        "process time of 143ms is over budget.",
        str(_repo),
    )
    result = guard.check(payload)
    assert result is not None


def test_edit_no_fire_when_pre_image_already_attributed(_repo):
    target = _repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md"
    target.write_text(
        "Measured via batched_process_time_ms already. old text here.",
        encoding="utf-8",
    )
    payload = _edit_payload(
        str(target),
        "old text here.",
        "process time of 143ms is over budget.",
        str(_repo),
    )
    assert guard.check(payload) is None


def test_multiedit_scans_reconstructed_body(_repo):
    target = _repo / "docs" / "research" / "2026-09-23-figure.md"
    target.write_text("first old. second old.", encoding="utf-8")
    payload = _multiedit_payload(
        str(target),
        [("first old.", "plain prose."), ("second old.", "process time of 210ms.")],
        str(_repo),
    )
    result = guard.check(payload)
    assert result is not None


def test_edit_stale_old_string_fails_open(_repo):
    target = _repo / "state" / "bug-backlog" / "2026-09-23-slow-op.md"
    target.write_text("Baseline notes.", encoding="utf-8")
    payload = _edit_payload(
        str(target),
        "not present in file",
        "process time of 143ms is over budget.",
        str(_repo),
    )
    assert guard.check(payload) is None


def test_fails_open_when_repo_root_unresolvable(tmp_path):
    unresolvable = tmp_path / "no-repo" / "state" / "bug-backlog" / "x.md"
    payload = _write_payload(
        str(unresolvable),
        "The process time of 143ms is over budget.",
        str(tmp_path / "no-repo"),
    )
    assert guard.check(payload) is None


def test_malformed_payload_fails_open():
    assert guard.check({}) is None
    assert guard.check({"tool_input": "not-a-dict"}) is None
    assert guard.check({"tool_name": "Write", "tool_input": {}}) is None


def test_never_raises_on_unexpected_shapes():
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": 5}}) is None
    assert guard.check(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": "x", "edits": "bad"}}
    ) is None
