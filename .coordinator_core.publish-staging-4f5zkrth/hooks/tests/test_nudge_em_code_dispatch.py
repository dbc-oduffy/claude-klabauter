"""
Tests for coordinator_core.hooks.nudge_em_code_dispatch, scoped to chunk C5
(plan docs/plans/2026-08-01-advisory-firing-shape-predicate.md): the `op()`
size-floor semantic-bypass mechanism (AC6) and the dispatch-brief TODO drop
(AC6, AC10).

Subject under test: `op()` only — the async `_handler` (pcore-04 mcp_tool op)
never receives old_string/new_string/content per this module's own MultiEdit
negative-spec, so it has nothing to classify and is out of C5's scope.

The invariants worth pinning, in priority order:
  1. A semantic-bypass edit (whitespace-only, comment-only, single-token
     rename, no-op) suppresses the nudge on Edit, MultiEdit (ALL entries must
     qualify), and Write (against the pre-write on-disk content).
  2. A substantive edit still fires post-fix (AC10) and the emitted dispatch
     brief carries no `[TODO` placeholder anywhere (AC6).
  3. Ambiguous/unreadable cases (new file with no on-disk baseline, malformed
     edit fields) do NOT qualify for the bypass — fail toward the nudge.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from coordinator_core.hooks import nudge_em_code_dispatch as _mod
from coordinator_core.hooks.nudge_em_code_dispatch import (
    _is_comment_or_docstring_only_diff,
    _is_semantic_bypass_edit,
    _is_single_token_rename,
    _is_whitespace_only_diff,
    op,
)


@pytest.fixture(autouse=True)
def _outside_f7_carveout_scope(monkeypatch):
    """C5's remit is the size-floor/TODO fix, not the F7 bootstrap/out-of-repo
    carve-out (a different, already-landed bypass). All fixture paths below are
    synthetic (`/repo/...`) so the real F7 walk would treat every one of them as
    outside a git work-tree and bypass the nudge for the wrong reason — pin that
    carve-out off so these tests exercise only the bypasses C5 owns.
    """
    monkeypatch.setattr(_mod, "_is_bootstrap_or_out_of_repo", lambda file_path: False)


def _advisory_text(result: dict) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


# --------------------------------------------------------------------------------------
# Unit-level predicates.
# --------------------------------------------------------------------------------------

def test_whitespace_only_diff_true() -> None:
    assert _is_whitespace_only_diff("x = 1\n", "x = 1\n\n") is True


def test_whitespace_only_diff_false_on_substantive_change() -> None:
    assert _is_whitespace_only_diff("x = 1", "x = 2") is False


def test_comment_only_diff_true() -> None:
    assert _is_comment_or_docstring_only_diff(
        "# old note\ndef f(): pass",
        "# new note, updated\ndef f(): pass",
    ) is True


def test_comment_only_diff_false_when_code_line_changes() -> None:
    assert _is_comment_or_docstring_only_diff(
        "# note\ndef f(): pass",
        "# note\ndef f(): return 1",
    ) is False


def test_comment_only_diff_false_when_no_change() -> None:
    # No changed lines at all — not a "comment-only diff", the no-op case is
    # handled separately by _is_semantic_bypass_edit's old == new short-circuit.
    assert _is_comment_or_docstring_only_diff("# note\n", "# note\n") is False


def test_single_token_rename_true() -> None:
    assert _is_single_token_rename("value = compute(x)", "value = compute(y)") is True


def test_single_token_rename_false_on_multi_token_change() -> None:
    assert _is_single_token_rename("value = compute(x)", "total = calculate(x)") is False


def test_single_token_rename_false_on_non_identifier_swap() -> None:
    assert _is_single_token_rename("x = 1", "x = 2") is False


def test_semantic_bypass_edit_covers_noop() -> None:
    assert _is_semantic_bypass_edit("same", "same") is True


def test_semantic_bypass_edit_false_on_substantive_change() -> None:
    assert _is_semantic_bypass_edit("def f():\n    return 1", "def f():\n    return compute(x)") is False


# --------------------------------------------------------------------------------------
# op() — Edit shape.
# --------------------------------------------------------------------------------------

def test_edit_whitespace_only_suppresses_nudge() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "x = 1\n",
            "new_string": "x = 1\n\n",
        },
    }
    assert op(payload) is None


def test_edit_single_token_rename_suppresses_nudge() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "value = compute(x)",
            "new_string": "value = compute(y)",
        },
    }
    assert op(payload) is None


def test_edit_substantive_change_fires_and_brief_has_no_todo() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "def f():\n    return 1",
            "new_string": "def f():\n    return compute_something(x, y)",
        },
    }
    result = op(payload)
    assert result is not None
    text = _advisory_text(result)
    assert "[TODO" not in text
    assert "module.py" in text


# --------------------------------------------------------------------------------------
# op() — MultiEdit shape: ALL entries must qualify.
# --------------------------------------------------------------------------------------

def test_multiedit_all_bypass_suppresses_nudge() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {"file_path": "/repo/pkg/module.py", "old_string": "# a", "new_string": "# b"},
            ],
        },
    }
    assert op(payload) is None


def test_multiedit_one_substantive_entry_fires() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {
                    "file_path": "/repo/pkg/module.py",
                    "old_string": "def f(): pass",
                    "new_string": "def f(): return compute(x, y, z)",
                },
            ],
        },
    }
    result = op(payload)
    assert result is not None
    assert "[TODO" not in _advisory_text(result)


def test_multiedit_malformed_entry_does_not_bypass() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {"file_path": "/repo/pkg/module.py", "old_string": 123, "new_string": "x"},
            ],
        },
    }
    assert op(payload) is not None


# --------------------------------------------------------------------------------------
# op() — Write shape: baseline read from disk (PreToolUse fires before the write).
# --------------------------------------------------------------------------------------

def test_write_whitespace_only_against_disk_content_suppresses_nudge(tmp_path) -> None:
    target = tmp_path / "module.py"
    target.write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "x = 1\n\n",
        },
    }
    assert op(payload) is None


def test_write_substantive_change_against_disk_content_fires(tmp_path) -> None:
    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "def f():\n    return compute_something(x, y)\n",
        },
    }
    result = op(payload)
    assert result is not None
    assert "[TODO" not in _advisory_text(result)


def test_write_brand_new_file_never_bypasses(tmp_path) -> None:
    """No on-disk baseline to diff against — fails toward the nudge, not toward
    silently suppressing it on unreadable/nonexistent content."""
    target = tmp_path / "does-not-exist-yet.py"
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "x = 1\n",
        },
    }
    assert op(payload) is not None


# --------------------------------------------------------------------------------------
# Dispatch-brief content: no TODO placeholders anywhere, regardless of shape (AC6).
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("Write", {"file_path": "/repo/pkg/new_module.py", "content": "def g():\n    return compute(a, b)\n"}),
        (
            "Edit",
            {
                "file_path": "/repo/pkg/module.py",
                "old_string": "def f(): pass",
                "new_string": "def f(): return compute(a, b, c)",
            },
        ),
        (
            "MultiEdit",
            {
                "edits": [
                    {
                        "file_path": "/repo/pkg/module.py",
                        "old_string": "def f(): pass",
                        "new_string": "def f(): return compute(a, b, c)",
                    },
                ],
            },
        ),
    ],
)
def test_dispatch_brief_never_contains_todo_placeholder(tool_name, tool_input) -> None:
    payload = {"tool_name": tool_name, "session_id": "sess-1", "tool_input": tool_input}
    result = op(payload)
    assert result is not None
    text = _advisory_text(result)
    assert "[TODO" not in text
    assert "acceptance-criteria" not in text


# --------------------------------------------------------------------------------------
# Registration.
# --------------------------------------------------------------------------------------

def test_op_is_registered() -> None:
    import coordinator_core.hooks  # noqa: F401 — triggers registration side-effects
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("hooks.nudge_em_code_dispatch") is not None
