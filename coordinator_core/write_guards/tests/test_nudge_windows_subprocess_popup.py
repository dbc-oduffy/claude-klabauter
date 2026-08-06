"""Behavioral tests for coordinator_core.write_guards.nudge_windows_subprocess_popup
-- the console-popup authoring advisory, post-DR-077.

Covers DR-077 (example-doctrine-repo docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md),
routed via cross-repo memo
cross-repo/archive/2026-07-21-claude-central-em-popup-guard-fragment-scoped-false-denials-dr077.md:

  1. Whole-file detection scope for Edit/MultiEdit -- a correctly-suppressed
     call whose creationflags= line falls OUTSIDE the edit fragment must no
     longer be flagged (Case A -- was a false DENY pre-DR-077).
  2. Same call with the fragment already including creationflags= still
     clears (Case B -- boundary-luck should no longer matter either way).
  3. Docstring-only prose mentioning sys.executable / subprocess.run() must
     never fire (Case C).
  4. A genuinely unsuppressed console spawn still surfaces -- and the
     envelope is the ADVISORY shape (additionalContext), never
     permissionDecision:"deny".
  5. Fail-open: an Edit against a file_path that doesn't exist on disk, or
     whose old_string isn't present in the on-disk content, falls back to
     fragment-scoped extraction rather than raising.
  6. Size cap: a file over _MAX_WHOLE_FILE_BYTES falls back to fragment
     scoping rather than being read in full.

Grep anchors: WINDOWS-CONSOLE-POPUP DR-077
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import nudge_windows_subprocess_popup as guard


_UNSUPPRESSED_CALL = (
    'import subprocess\n'
    'subprocess.run(["powershell.exe", "-Command", "dir"])\n'
)

_SUPPRESSED_CALL = (
    'import subprocess\n'
    'subprocess.run(["powershell.exe", "-Command", "dir"],\n'
    '                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))\n'
)

_DOCSTRING_ONLY = (
    '"""Helper module.\n'
    '\n'
    'Spawns via sys.executable and subprocess.run() when needed.\n'
    '"""\n'
)


def _payload(tool_name, tool_input):
    return {"tool_name": tool_name, "tool_input": tool_input}


def _is_advisory_envelope(result: dict) -> bool:
    hso = result.get("hookSpecificOutput", {})
    return "additionalContext" in hso and "permissionDecision" not in hso


class TestCaseAWholeFileSuppressionOutsideFragment:
    """DR-077 Case A -- suppression present in the file but outside the
    edited fragment must clear, not deny."""

    def test_edit_fragment_only_unsuppressed_call_but_file_has_suppression(self, tmp_path: Path):
        target = tmp_path / "run.py"
        # The whole file (post-edit) IS suppressed -- the creationflags line
        # lives outside what the Edit fragment itself contains.
        target.write_text(
            'import subprocess\n'
            'HELPER = 1\n'
            'subprocess.run(["powershell.exe", "-Command", "dir"],\n'
            '                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))\n'
        )

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "HELPER = 1",
                "new_string": "HELPER = 2",
            },
        )

        result = guard.check(payload)
        assert result is None, (
            "whole-file reconstruction should see the out-of-fragment "
            "creationflags= suppression and clear the write"
        )

    def test_fragment_scoped_extraction_alone_would_have_denied(self, tmp_path: Path):
        """Sanity check that this really is a false-positive-fixed case: the
        OLD fragment-only extraction (what _extract_fragment returns) does
        NOT contain the suppression, proving the fix is whole-file reads,
        not a coincidence."""
        fragment = guard._extract_fragment("Edit", {"new_string": "HELPER = 2"})
        assert "creationflags" not in fragment


class TestCaseBFragmentIncludesSuppression:
    def test_fragment_already_has_creationflags_still_clears(self, tmp_path: Path):
        target = tmp_path / "run.py"
        target.write_text("HELPER = 1\n")

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "HELPER = 1",
                "new_string": _SUPPRESSED_CALL,
            },
        )

        result = guard.check(payload)
        assert result is None


class TestCaseCDocstringOnlyProse:
    """DR-077 Case C. Per the routing memo, whole-file context "removes case A
    and MOST of case C" (not all of it) -- when the whole file also contains a
    genuinely-suppressed call elsewhere, the file-wide suppression regex
    clears the prose mention too (no scoping to the specific matched call).
    A file that is PURELY docstring prose with no code anywhere still has no
    suppression token to find, so the intent-aware check still matches the
    literal substrings and still surfaces something -- but per DR-077 part 2
    that's now an ADVISORY, not a block, which is the actual guarantee DR-077
    makes (it never promised the regex itself would stop matching prose
    outside a whole-file-context win)."""

    def test_edit_docstring_prose_in_a_file_with_suppression_elsewhere_clears(
        self, tmp_path: Path
    ):
        target = tmp_path / "helper.py"
        target.write_text(
            'import subprocess\n'
            'subprocess.run(["powershell.exe"],\n'
            '                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))\n'
            '\n'
            'OLD_DOC = "Old line."\n'
        )

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": 'OLD_DOC = "Old line."',
                "new_string": (
                    'OLD_DOC = "Spawns via sys.executable and subprocess.run() '
                    'when needed."'
                ),
            },
        )

        result = guard.check(payload)
        assert result is None, (
            "whole-file context should see the existing creationflags= "
            "suppression and clear the docstring-only edit"
        )

    def test_write_pure_prose_file_still_surfaces_but_only_as_advisory(self):
        """Residual case: a file with NO code anywhere has nothing for
        whole-file context to reveal, so the intent-aware regex still fires
        on the literal substrings -- documented here as expected, since
        DR-077's guarantee is "advisory, not deny", not "never fires on
        prose". This must NEVER be a permissionDecision:"deny"."""
        payload = _payload("Write", {"file_path": "helper.py", "content": _DOCSTRING_ONLY})
        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)


class TestTruePositiveIsAdvisoryNotDeny:
    def test_write_unsuppressed_call_still_surfaces(self):
        payload = _payload("Write", {"file_path": "run.py", "content": _UNSUPPRESSED_CALL})
        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)

    def test_edit_unsuppressed_call_whole_file_still_surfaces(self, tmp_path: Path):
        target = tmp_path / "run.py"
        target.write_text("HELPER = 1\n")

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "HELPER = 1",
                "new_string": _UNSUPPRESSED_CALL,
            },
        )

        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hso
        assert "OFFER:" in hso["additionalContext"]

    def test_multiedit_unsuppressed_call_whole_file_still_surfaces(self, tmp_path: Path):
        target = tmp_path / "run.py"
        target.write_text("A = 1\nB = 2\n")

        payload = _payload(
            "MultiEdit",
            {
                "file_path": str(target),
                "edits": [
                    {"old_string": "A = 1", "new_string": "import subprocess"},
                    {
                        "old_string": "B = 2",
                        "new_string": 'subprocess.run(["powershell.exe"])',
                    },
                ],
            },
        )

        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)


class TestFailOpen:
    def test_edit_nonexistent_file_falls_back_to_fragment(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist.py"

        payload = _payload(
            "Edit",
            {
                "file_path": str(missing),
                "old_string": "anything",
                "new_string": _UNSUPPRESSED_CALL,
            },
        )

        # Fragment-scoped fallback still contains the unsuppressed call, so
        # this should still surface -- the point is no exception, not a
        # particular verdict.
        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)

    def test_edit_old_string_absent_from_disk_falls_back_to_fragment(self, tmp_path: Path):
        target = tmp_path / "run.py"
        target.write_text("nothing matches here\n")

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "THIS STRING IS NOT IN THE FILE",
                "new_string": _UNSUPPRESSED_CALL,
            },
        )

        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)

    def test_multiedit_missing_file_falls_back_to_fragment_no_raise(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist.py"

        payload = _payload(
            "MultiEdit",
            {
                "file_path": str(missing),
                "edits": [{"old_string": "x", "new_string": _UNSUPPRESSED_CALL}],
            },
        )

        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)


class TestSizeCap:
    def test_oversized_file_falls_back_to_fragment_without_raising(self, tmp_path: Path):
        target = tmp_path / "big.py"
        # Whole file is oversized AND (deliberately) does not itself contain
        # the console-spawn tokens near the edit -- only the fragment does.
        target.write_text("x = 1\n" * (guard._MAX_WHOLE_FILE_BYTES // 4))
        assert target.stat().st_size > guard._MAX_WHOLE_FILE_BYTES

        payload = _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "x = 1",
                "new_string": _UNSUPPRESSED_CALL,
            },
        )

        result = guard.check(payload)
        assert result is not None
        assert _is_advisory_envelope(result)

    def test_read_file_safely_returns_none_over_cap(self, tmp_path: Path):
        target = tmp_path / "big.txt"
        target.write_text("a" * (guard._MAX_WHOLE_FILE_BYTES + 1))
        assert guard._read_file_safely(str(target)) is None

    def test_read_file_safely_returns_none_for_missing_file(self, tmp_path: Path):
        missing = tmp_path / "nope.txt"
        assert guard._read_file_safely(str(missing)) is None


class TestClassAndAllowlistStillWork:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_allowlist_marker_still_clears_true_positive(self):
        payload = _payload(
            "Write",
            {
                "file_path": "run.py",
                "content": _UNSUPPRESSED_CALL + "  # popup-intentional-last-resort\n",
            },
        )
        result = guard.check(payload)
        assert result is None
