"""Memo-send rows route to an EM step, verification rows are exempt from the
Write-tool-only rule, and test candidates resolve to the layout on disk."""
from pathlib import Path

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.pathspec import (
    candidate_test_additions,
    collapse_test_scope,
)
from coordinator_core.ops.dispatch_emit.spine_read import _is_memo_send_row


def test_memo_send_row_is_detected_by_brief_kind_and_outbox_write():
    assert _is_memo_send_row({"brief": "run `cross-repo-memo send` to the peer"})
    assert _is_memo_send_row({"kind": "memo-send"})
    assert not _is_memo_send_row({"brief": "edit a module", "writes": ["a.py"]})


def test_a_memo_outbox_directory_surface_or_writes_under_is_a_memo_send_row():
    # example-game-repo-d2 idk-01 C13: doc-edit, writes: [], writes_under the outbox, a "memo lane" body.
    assert _is_memo_send_row({"surface": "state/memo-outbox/", "writes": [], "writes_under": ["state/memo-outbox/"]})
    assert _is_memo_send_row({"writes_under": [".coordinator-local/memo-outbox"]})
    assert not _is_memo_send_row({"surface": "state/memo-outbox-notes/", "writes_under": ["docs/"]})


def test_memo_send_row_is_narrated_as_an_em_step():
    out = emit._excluded_rows_narration(
        [{"id": "C9", "reason": "em-performed", "detail": "EM STEP: cross-repo memo send"}]
    )
    assert "C9: EM STEP" in out
    assert "OWED WORK" in out


def test_verification_row_head_drops_the_write_tool_only_rule():
    assert emit._WRITE_TOOL_ONLY_CLAUSE in emit._prompt_head(None)
    head = emit._prompt_head(None, verification=True)
    assert emit._WRITE_TOOL_ONLY_CLAUSE not in head
    assert emit._VERIFICATION_ROW_CLAUSE in head
    assert emit._is_verification_row(type("R", (), {"change_kind": "verification"})())
    assert not emit._is_verification_row(type("R", (), {"change_kind": "code"})())


def test_test_candidate_prefers_the_existing_sibling_tests_dir(tmp_path: Path):
    (tmp_path / "pkg" / "sub" / "tests").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    assert candidate_test_additions(["pkg/sub/mod.py"], tmp_path) == [
        "pkg/sub/tests/test_mod.py"
    ]


def test_test_candidate_falls_to_an_ancestor_tests_dir_when_no_sibling(tmp_path: Path):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    assert candidate_test_additions(["pkg/sub/mod.py"], tmp_path) == [
        "tests/test_mod.py"
    ]


def test_test_candidate_is_skipped_when_the_test_is_already_declared(tmp_path: Path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    assert candidate_test_additions(
        ["pkg/mod.py", "tests/test_mod.py"], tmp_path
    ) == []


def test_test_candidate_uses_pytest_testpaths(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "suite").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["suite"]\n', encoding="utf-8"
    )
    assert candidate_test_additions(["pkg/mod.py"], tmp_path) == ["suite/test_mod.py"]


def test_test_runner_scope_collapses_a_name_twin_to_the_one_on_disk(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("", encoding="utf-8")
    assert collapse_test_scope(
        ["pkg/tests/test_mod.py", "tests/test_mod.py"], tmp_path
    ) == ["tests/test_mod.py"]
    brief = emit._test_agent_call_expr(
        ["pkg/tests/test_mod.py", "tests/test_mod.py"], repo_root=tmp_path
    )
    assert "pkg/tests/test_mod.py" not in brief
