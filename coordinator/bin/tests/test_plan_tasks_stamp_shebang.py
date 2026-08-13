"""test_plan_tasks_stamp_shebang.py — `plan-tasks-stamp` must carry a
`#!/usr/bin/env python3` shebang as its first line (fix landed the same day
this test was added).

Defect this closes: the file's first line was a bare triple-quote (its own
docstring open), with NO shebang at all -- unlike every sibling
`coordinator/bin/` trampoline. The file is +x on disk, so the OS attempted
to `execve()` it directly rather than falling back to any interpreter,
which the kernel cannot do for a file with no shebang line -- an ENOEXEC
OSError, reproduced by the entrypoint gate
(`coordinator_core.percolate.engine.run_entrypoint_gate` /
`_run_one_entrypoint`) launching `[str(script_path), "--help"]` directly
(the no-interpreter-resolved branch) for a script whose shebang that
function itself reads to resolve an interpreter in the first place.

Spec backlink: the four-coordinator-bin-entrypoints bug backlog item filed
under state/bug-backlog/.

Run:
    python3 -m pytest coordinator/bin/tests/test_plan_tasks_stamp_shebang.py -v
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "plan-tasks-stamp"


class TestShebangPresent(unittest.TestCase):
    def test_first_line_is_python3_shebang(self):
        first_line = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "#!/usr/bin/env python3")

    def test_help_starts_cleanly_via_direct_exec(self):
        # Mirrors the gate's own no-interpreter-resolved invocation shape
        # (`_run_one_entrypoint`'s `argv = [str(script_path), "--help"]`
        # branch) -- this is the exact call shape that previously raised
        # an ENOEXEC OSError with no shebang present.
        completed = subprocess.run(
            [str(_SCRIPT), "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("plan-tasks-stamp", completed.stdout)

    def test_non_help_invocation_still_fails_loudly_on_missing_args(self):
        completed = subprocess.run(
            [str(_SCRIPT)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
