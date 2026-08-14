"""test_plan_tasks_stamp_shebang.py — `plan-tasks-stamp` follows the
ratified `coordinator/bin/` convention (`e167d08d1`): NO shebang, NO exec
bit (mode 100644), invoked as `python3 <path>` on POSIX and via the
co-located `.cmd`/`.ps1` launcher on Windows.

History this closes: the file was created with the exec bit set and no
shebang at all -- an ENOEXEC OSError under the entrypoint gate's
no-interpreter-resolved direct-exec branch. `efa4bd3a0` patched around it
by adding a shebang and pinning direct exec, which stood as a standing
exception to the ratified convention. That exception itself then caused a
second live breakage: a bulk shebang-strip left the file exec-bit-set with
no shebang, reproducing the exact ENOEXEC failure. The exception is
removed at its cause here: the exec bit is dropped, so the entrypoint
gate's `_run_one_entrypoint` takes its `sys.executable` fallback and no
shebang is needed, collapsing the file back onto every compliant sibling
(e.g. `coordinator/bin/append-integrator-dispositions.py`).

Spec backlink: the four-coordinator-bin-entrypoints bug backlog item filed
under state/bug-backlog/.

Run:
    python3 -m pytest coordinator/bin/tests/test_plan_tasks_stamp_shebang.py -v
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "plan-tasks-stamp"


class TestConventionCompliant(unittest.TestCase):
    def test_no_shebang_first_line(self):
        first_line = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        self.assertNotEqual(
            first_line,
            "#!/usr/bin/env python3",
            "plan-tasks-stamp should follow the ratified coordinator/bin/ "
            "convention (e167d08d1): no shebang, invoked as `python3 <path>`.",
        )

    def test_not_executable(self):
        import stat

        mode = _SCRIPT.stat().st_mode
        self.assertFalse(
            mode & stat.S_IXUSR,
            "plan-tasks-stamp should carry mode 100644 (no exec bit) per "
            "the ratified coordinator/bin/ convention (e167d08d1).",
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
