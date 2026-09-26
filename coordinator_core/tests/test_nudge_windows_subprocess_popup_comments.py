
from __future__ import annotations

import pytest

from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    check as _wg_check,
)


def _wg_denied(file_path: str, content: str, tool_name: str = "Write") -> bool:
    payload = {
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "content": content,
            "new_string": content,
        },
    }
    return _wg_check(payload) is not None


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
