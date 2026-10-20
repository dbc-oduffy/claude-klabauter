"""test_plan_tasks_stamp_shebang.py -- `plan-tasks-stamp` carries the
python3 shebang that its 100755 index mode requires.

Rule history: e167d08d1 stripped the shebang and exec bit from bin entries;
a1f0c262c2 applied that to this file. The rule then moved: every extensionless
`coordinator/bin/` entry is indexed 100755
(test_bin_extensionless_exec_bit, 70ec8f5fd8), and an exec-bit file without a
shebang is the ENOEXEC failure this file's history started from. So the
shebang is required; the exec bit itself is pinned by the sibling ratchet.

Run:
    python3 -m pytest coordinator/bin/tests/test_plan_tasks_stamp_shebang.py -v
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "plan-tasks-stamp"


class TestConventionCompliant(unittest.TestCase):
    def test_shebang_first_line(self):
        first_line = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(
            first_line,
            "#!/usr/bin/env python3",
            "plan-tasks-stamp is indexed 100755 and needs its python3 shebang "
            "(test_bin_extensionless_exec_bit).",
        )

    def test_help_starts_cleanly_via_python3_invocation(self):
        # The sanctioned invocation shape: `python3 <path> --help`, NOT a
        # direct exec relying on a shebang + exec bit.
        completed = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("plan-tasks-stamp", completed.stdout)

    def test_non_help_invocation_still_fails_loudly_on_missing_args(self):
        completed = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
