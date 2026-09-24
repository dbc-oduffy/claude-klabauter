"""Behavioral tests for coordinator_core.write_guards.nudge_handoff_author_lint.

Drives `check()` with PreToolUse payloads, not on-disk fixtures — see this
plan's exit-criterion 2 note: an on-disk-fixture oracle cannot falsify a
PreToolUse guard that reads from disk. Every fire case here is a `Write` to a
NOT-YET-EXISTING path, or an `Edit` whose pre-image is on disk but whose
defect is introduced by the edit itself.

Spec backlink: docs/plans/2026-09-11-wire-the-authoring-surface-lint-and-clos.md
(chunk C1)
"""

from __future__ import annotations

from coordinator_core.write_guards import nudge_handoff_author_lint as guard

_LEDGER_HEADING = "## Session Ledger"
_LEDGER_FORMAT_COMMENT = (
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)


def _doc(summary="a real one-line summary", ac="- [ ] do the thing", ledger_rows=""):
    return (
        "---\n"
        "title: t\n"
        f"summary: {summary}\n"
        "kind: spinoff\n"
        "---\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        f"{ac}\n"
        "\n"
        f"{_LEDGER_HEADING}\n"
        "\n"
        f"{_LEDGER_FORMAT_COMMENT}"
        f"{ledger_rows}"
    )


def _payload(tool_name, tool_input, cwd=None):
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _handoff_dir(tmp_path):
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


def _context(result):
    return result["hookSpecificOutput"]["additionalContext"]


class TestFiresOnWriteToNewPath:
    def test_over_cap_summary_fires(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(summary="x" * 200)},
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "hookSpecificOutput" in result
        assert "permissionDecision" not in result["hookSpecificOutput"]
        assert "truncat" in _context(result)

    def test_unparseable_ledger_row_fires(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        rows = "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(ledger_rows=rows)},
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "Session Ledger" in _context(result)

    def test_placeholder_summary_fires(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        placeholder = '"PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)"'
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(summary=placeholder)},
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "summary:" in _context(result)


class TestFiresOnEditIntroducingDefect:
    def test_edit_adding_unparseable_row_to_clean_pre_image_fires(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_doc())
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": _LEDGER_FORMAT_COMMENT,
                    "new_string": (
                        _LEDGER_FORMAT_COMMENT
                        + "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
                    ),
                },
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "Session Ledger" in _context(result)


class TestSilentOnAlreadyPresentDefect:
    def test_edit_touching_unrelated_line_with_pre_existing_defect_is_silent(
        self, tmp_path
    ):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_doc(summary="x" * 200))
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "do the thing\n",
                    "new_string": "do the other thing\n",
                },
                cwd=str(tmp_path),
            )
        )
        assert result is None


class TestSilentOnFilteredCode:
    def test_ac_no_checkboxes_alone_is_silent_here(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(ac="- prose bullet")},
                cwd=str(tmp_path),
            )
        )
        assert result is None


class TestSilentOnCleanBody:
    def test_clean_handoff_is_silent(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc()},
                cwd=str(tmp_path),
            )
        )
        assert result is None


class TestSilentOnScope:
    def test_non_handoff_path_is_silent(self, tmp_path):
        d = tmp_path / "docs" / "plans"
        d.mkdir(parents=True)
        target = d / "some-plan.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(summary="x" * 200)},
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_path_outside_git_root_matching_handoff_shape_is_silent(self, tmp_path):
        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)
        outside_dir = tmp_path / "elsewhere" / "state" / "handoffs"
        outside_dir.mkdir(parents=True)
        target = outside_dir / "some-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _doc(summary="x" * 200)},
                cwd=str(repo_dir),
            )
        )
        assert result is None


class TestNeverDenies:
    def test_no_branch_returns_a_deny_envelope(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "new-handoff.md"
        for content in (_doc(), _doc(summary="x" * 200), "not even markdown"):
            result = guard.check(
                _payload(
                    "Write",
                    {"file_path": str(target), "content": content},
                    cwd=str(tmp_path),
                )
            )
            if result is not None:
                assert "permissionDecision" not in result["hookSpecificOutput"]


class TestRewordedSummary:
    def test_reword_to_different_still_over_cap_length_fires(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_doc(summary="y" * 200))
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "y" * 200,
                    "new_string": "z" * 210,
                },
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "truncat" in _context(result)

    def test_reword_to_same_length_is_silent(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_doc(summary="y" * 200))
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "y" * 200,
                    "new_string": "z" * 200,
                },
                cwd=str(tmp_path),
            )
        )
        assert result is None


class TestMultisetDedupe:
    def test_second_byte_identical_ledger_row_fires_exactly_once(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        row = "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        target.write_text(_doc(ledger_rows=row))
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": row,
                    "new_string": row + row,
                },
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        context = _context(result)
        assert context.count("LEDGER_ROW_UNPARSEABLE".lower()) == 0  # not embedded literally
        # exactly one relayed line for the ledger finding
        ledger_lines = [
            line for line in context.splitlines() if "Session Ledger" in line
        ]
        assert len(ledger_lines) == 1
