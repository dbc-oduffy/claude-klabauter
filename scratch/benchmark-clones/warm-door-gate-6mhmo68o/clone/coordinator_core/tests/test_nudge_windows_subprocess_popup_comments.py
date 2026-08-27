"""
coordinator_core.tests.test_nudge_windows_subprocess_popup_comments — regression net
for improvement-queue 2026-06-22-subprocess-popup-write-guard-denies-edit.yaml.

Placement note: the dispatch brief for this fix asked for
coordinator_core/write_guards/tests/test_nudge_windows_subprocess_popup_comments.py,
but coordinator_core/write_guards has no tests/ subdirectory (all engine-side tests
live flat under coordinator_core/tests/, per that directory's existing convention —
see test_hooks_roundtrip.py). Per the brief's own fallback instruction ("if
write_guards has no tests/ dir, put it in the nearest existing test location and say
where"), this file lives here instead.

Bug being regression-tested: the nudge_windows_subprocess_popup guard
(coordinator_core/write_guards/...) previously scanned RAW file content with
comment-unaware regexes, so a bare-call TOKEN merely mentioned in a `#` comment
(e.g. an explanatory bash comment quoting `python -c '...'` in backticks) was
denied even though no real subprocess call exists. The fix adds a quote-aware
per-line comment stripper (`_strip_line_comments`) that runs AFTER the
allowlist-marker check but BEFORE the detection regexes.

This suite parametrizes 12 cases against the guard. Cases 1-4 are the false
positives being fixed (comment-only mentions); cases 5-9 are true positives that
must still deny (including case 9's quote-awareness proof); cases 10-12 are
pre-existing escapes (allowlist markers, extension gate) that must keep working.

Spec backlink: state/improvement-queue/2026-06-22-subprocess-popup-write-guard-denies-edit.yaml
Guards under test:
    coordinator_core/write_guards/nudge_windows_subprocess_popup.py
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    check as _wg_check,
)


def _wg_denied(file_path: str, content: str, tool_name: str = "Write") -> bool:
    """Thin wrapper over the write_guards copy's check() envelope-or-None shape."""
    payload = {
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "content": content,
            "new_string": content,
        },
    }
    return _wg_check(payload) is not None


# ---------------------------------------------------------------------------
# The 12 required cases: (case_id, file_path, content, expected_deny)
# ---------------------------------------------------------------------------
_CASES = [
    (
        "1-sh-comment-python-c",
        "scripts/note.sh",
        "# Zero-spawn on purpose -- probing with `python -c 'import os; os.pathsep'` would\n"
        "# add a process spawn to a hot path.\n",
        False,
    ),
    (
        "2-sh-comment-powershell-exe",
        "scripts/note.sh",
        "# do not run powershell.exe here, it's just documentation\n",
        False,
    ),
    (
        "3-py-comment-token",
        "scripts/note.py",
        "# example: python3 -c 'print(1)' spawns a console\n",
        False,
    ),
    (
        "4-sh-comment-backticks-real-world",
        "scripts/note.sh",
        "# Zero-spawn on purpose -- probing with `python -c 'import os; os.pathsep'` would\n"
        "# add a process spawn to a hot path.\n",
        False,
    ),
    (
        "5-sh-real-bare-call",
        "scripts/run.sh",
        "python3 -c 'import x'\n",
        True,
    ),
    (
        "6-sh-real-bare-call-trailing-comment",
        "scripts/run.sh",
        "python3 -c 'import x'  # explanatory comment\n",
        True,
    ),
    (
        "7-py-subprocess-run-powershell-no-suppression",
        "scripts/run.py",
        'import subprocess\n'
        'subprocess.run(["powershell.exe", "-Command", "dir"])\n',
        True,
    ),
    (
        "8-ps1-bare-powershell-no-windowstyle",
        "scripts/run.ps1",
        "powershell.exe -Command Get-Process\n",
        True,
    ),
    (
        "9-sh-quoted-hash-before-real-call",
        "scripts/run.sh",
        'echo "a#b"; python3 -c \'x\'\n',
        True,
    ),
    (
        "10-sh-real-call-env-suppressed-marker",
        "scripts/run.sh",
        "python3 -c 'import x'  # popup-safe-env-suppressed\n",
        False,
    ),
    (
        "11-sh-real-call-last-resort-marker",
        "scripts/run.sh",
        "python3 -c 'import x'  # popup-intentional-last-resort\n",
        False,
    ),
    (
        "12-md-extension-gate",
        "docs/note.md",
        "python3 -c 'import x'\n",
        False,
    ),
]


@pytest.mark.parametrize("case_id,file_path,content,expected_deny", _CASES, ids=[c[0] for c in _CASES])
def test_write_guards_copy(case_id, file_path, content, expected_deny):
    assert _wg_denied(file_path, content) is expected_deny, (
        f"write_guards copy mismatch on {case_id}: expected deny={expected_deny}"
    )
