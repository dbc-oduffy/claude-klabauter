"""Behavioral tests for
coordinator_core.write_guards.nudge_new_sh_file_naked_python -- the
new-.sh-file naked-Python advisory guard.

Spec: DoE-claude CLAUDE.md § Runtime conventions

Covers: non-Write-tool/non-.sh-path passthrough, new-.sh-file Write fires
(including a Windows-separator path form and a case-varied .SH extension),
existing-.sh-file Write passes through, Edit/MultiEdit of a .sh file never
fires, the two irreducible-leg basename exemptions, the tests/fixtures/
vendor/node_modules path-segment carve-out, content-only .sh mentions (not
the target path) never firing, and the COORDINATOR_NEW_SH_PUNT escape
hatch (non-trivial suppresses, trivial still advises with the trivial
hint appended). Also the module contract (CLASS/PRIORITY/MATCHERS).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import nudge_new_sh_file_naked_python as guard


def _payload(tool_name, tool_input, cwd=None):
    out = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        out["cwd"] = cwd
    return out


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


class TestFiresOnNewShFileWrite:
    def test_new_sh_file_fires(self, tmp_path):
        target = tmp_path / "coordinator" / "scripts" / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        text = _advisory_text(result)
        assert "coordinator_core module" in text
        assert str(target) in text

    def test_message_names_the_two_preapproved_exceptions(self, tmp_path):
        """Review finding (coordinatorcode-reviewer-54284751, Finding 5,
        nit): the compressed advisory dropped the two named bash exceptions
        (invoking-shell-bash4-probe.sh / claude-machine-local.sh), leaving
        an agent re-creating one of them no signal it's already sanctioned.
        Pins that the primary-fire message still names both."""
        target = tmp_path / "coordinator" / "scripts" / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        text = _advisory_text(result)
        assert "invoking-shell-bash4-probe.sh" in text
        assert "claude-machine-local.sh" in text

    def test_windows_separator_path_fires(self, tmp_path):
        # A Windows-separator path that does not exist on this (POSIX) test
        # host -- exercises the backslash-normalization branch for
        # extension/basename detection.
        target = str(tmp_path) + "\\scripts\\new_thing.sh"
        result = guard.check(_payload("Write", {"file_path": target, "content": "echo hi\n"}))
        _advisory_text(result)

    def test_case_varied_extension_fires(self, tmp_path):
        target = tmp_path / "new_thing.SH"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        _advisory_text(result)


class TestSilentOnExistingShFile:
    def test_existing_sh_file_write_passes_through(self, tmp_path):
        target = tmp_path / "already_here.sh"
        target.write_text("echo old\n")
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo new\n"})
        )
        assert result is None


class TestSilentOnEditOrMultiEdit:
    def test_edit_of_sh_file_never_fires(self, tmp_path):
        target = tmp_path / "new_thing.sh"  # need not exist -- MATCHERS excludes Edit anyway
        result = guard.check(
            _payload(
                "Edit",
                {"file_path": str(target), "old_string": "a", "new_string": "b"},
            )
        )
        assert result is None

    def test_multiedit_of_sh_file_never_fires(self, tmp_path):
        target = tmp_path / "new_thing.sh"
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [{"old_string": "a", "new_string": "b"}],
                },
            )
        )
        assert result is None


class TestIrreducibleLegExemptions:
    @pytest.mark.parametrize(
        "basename",
        ["invoking-shell-bash4-probe.sh", "claude-machine-local.sh"],
    )
    def test_irreducible_leg_basenames_exempt(self, tmp_path, basename):
        target = tmp_path / "lib" / basename
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "#!/bin/bash\n"})
        )
        assert result is None

    def test_irreducible_leg_basename_case_insensitive(self, tmp_path):
        target = tmp_path / "CLAUDE-MACHINE-LOCAL.SH"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "#!/bin/bash\n"})
        )
        assert result is None


class TestFixtureVendorCarveOut:
    @pytest.mark.parametrize("segment", ["tests", "fixtures", "vendor", "node_modules"])
    def test_carveout_segment_exempt(self, tmp_path, segment):
        target = tmp_path / segment / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        assert result is None

    def test_carveout_is_full_segment_not_substring(self, tmp_path):
        # "vendor-scripts" contains "vendor" as a substring but is not the
        # path segment "vendor" -- must still fire.
        target = tmp_path / "vendor-scripts" / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        _advisory_text(result)


class TestPassesThroughOnNonMatch:
    def test_non_write_tool_passes_through(self, tmp_path):
        target = tmp_path / "thing.sh"
        assert (
            guard.check(_payload("Read", {"file_path": str(target)}))
            is None
        )

    def test_py_extension_passes_through(self, tmp_path):
        target = tmp_path / "thing.py"
        assert (
            guard.check(_payload("Write", {"file_path": str(target), "content": "x = 1\n"}))
            is None
        )

    def test_content_only_sh_mention_does_not_fire(self, tmp_path):
        """A .sh string appearing only in file CONTENT, not the target
        path, must not trigger this path-keyed guard."""
        target = tmp_path / "notes.md"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": str(target), "content": "see coordinator/lib/thing.sh\n"},
            )
        )
        assert result is None

    def test_empty_file_path_passes_through(self):
        assert guard.check(_payload("Write", {"file_path": "", "content": "x"})) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None


class TestEscapeHatch:
    def test_non_trivial_punt_suppresses(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "COORDINATOR_NEW_SH_PUNT", "genuinely a third irreducible bash leg"
        )
        target = tmp_path / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        assert result is None

    def test_trivial_punt_still_advises_with_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_NEW_SH_PUNT", "1")
        target = tmp_path / "thing.sh"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "echo hi\n"})
        )
        text = _advisory_text(result)
        assert "COORDINATOR_NEW_SH_PUNT" in text
        assert "trivial" in text


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_matchers_write_only(self):
        assert guard.MATCHERS == ["Write"]

    def test_priority(self):
        assert guard.PRIORITY == 160
