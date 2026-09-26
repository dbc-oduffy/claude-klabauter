
from __future__ import annotations

from coordinator_core.write_guards import nudge_handoff_ac_shape as guard

_PROSE_BODY = (
    "---\n"
    "kind: session-handoff\n"
    "title: \"example\"\n"
    "---\n\n"
    "# Example\n\n"
    "## Acceptance criteria\n\n"
    "- Did the thing\n"
    "- Did the other thing\n\n"
    "## Notes\n\n"
    "some notes\n"
)

_CHECKBOX_BODY = _PROSE_BODY.replace(
    "- Did the thing\n- Did the other thing\n",
    "- [ ] Did the thing\n- [x] Did the other thing\n",
)

_NO_HEADING_BODY = (
    "---\n"
    "kind: session-handoff\n"
    "title: \"example\"\n"
    "---\n\n"
    "# Example\n\n"
    "## Notes\n\n"
    "some notes\n"
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


class TestFires:
    def test_fires_on_write_with_prose_bullets(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        spinoff_body = _PROSE_BODY.replace(
            "kind: session-handoff", "kind: spinoff"
        )
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": spinoff_body},
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hso
        assert "checkboxes" in hso["additionalContext"]
        assert "COORDINATOR_" not in hso["additionalContext"]

    def test_fires_on_spinoff_shaped_path_same_directory(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-spinoff.md"
        spinoff_body = _PROSE_BODY.replace(
            "kind: session-handoff", "kind: spinoff"
        )
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": spinoff_body},
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]

    def test_fires_on_multi_edit_sequential_fragments(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(
            _NO_HEADING_BODY.replace("kind: session-handoff", "kind: spinoff")
        )
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {
                            "old_string": "this text is not present\n",
                            "new_string": "irrelevant\n",
                        },
                        {
                            "old_string": "# Example\n\n## Notes\n",
                            "new_string": (
                                "# Example\n\n"
                                "## Acceptance criteria\n\n"
                                "- Did the thing\n\n"
                                "## Notes\n"
                            ),
                        },
                    ],
                },
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert "checkboxes" in result["hookSpecificOutput"]["additionalContext"]


class TestSilent:
    def test_silent_on_checkboxes(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _CHECKBOX_BODY},
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_when_heading_absent(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _NO_HEADING_BODY},
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_session_handoff_kind(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _PROSE_BODY},
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_unchanged_ac_section(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        spinoff_body = _PROSE_BODY.replace(
            "kind: session-handoff", "kind: spinoff"
        )
        target.write_text(spinoff_body)
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "some notes\n",
                    "new_string": "some more notes, unrelated to AC\n",
                },
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_non_handoff_path(self, tmp_path):
        d = tmp_path / "docs" / "plans"
        d.mkdir(parents=True)
        target = d / "some-plan.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _PROSE_BODY},
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_edit_producing_no_ac_section(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_NO_HEADING_BODY)
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "some notes\n",
                    "new_string": "some more notes\n",
                },
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_edit_with_stale_old_string(self, tmp_path):
        d = _handoff_dir(tmp_path)
        target = d / "some-handoff.md"
        target.write_text(_PROSE_BODY)
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "this text does not appear anywhere\n",
                    "new_string": "irrelevant\n",
                },
                cwd=str(tmp_path),
            )
        )
        assert result is None

    def test_silent_on_path_outside_git_root(self, tmp_path):
        """Containment regression test: `_HANDOFF_RE` matches on a
        substring, so an absolute path carrying `state/handoffs/<x>.md`
        outside the resolved git root still matches the regex — the
        containment check (via `contained_path`), not the regex, is what
        keeps this out of scope."""
        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)
        outside_dir = tmp_path / "elsewhere" / "state" / "handoffs"
        outside_dir.mkdir(parents=True)
        target = outside_dir / "some-handoff.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": _PROSE_BODY},
                cwd=str(repo_dir),
            )
        )
        assert result is None
