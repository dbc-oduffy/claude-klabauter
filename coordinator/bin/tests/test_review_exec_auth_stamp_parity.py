"""bin/tests/test_review_exec_auth_stamp_parity.py

Purpose: DR-076 cross-platform invocation-parity guard for
coordinator/bin/review-exec-auth-stamp — the CLI trampoline over
`coordinator_core.review_assemble.exec_auth_stamp` (the mutating assembler
that collapses `/review`'s ordinal-narrated execution-authorization stamp
sequence into one named op; plan
2026-07-24-computed-skills-b8-review-ci-cluster.md chunk C6).

This assembler had NO test file at all as of the 2026-07-25 DR-076 audit.
This file closes the parity gap only — it does not attempt to backfill
unit coverage of `stamp`'s own mutation logic, which is out of scope for
this dispatch.

This entrypoint is a BARE (extensionless) file, unlike the two `.py`-suffixed
assemblers covered by sibling parity suites in this directory
(test_percolate_parse_dryrun_parity.py,
test_parallel_review_gate_decision.py's CrossPlatformParityTests class) —
its `.cmd` sibling is named `review-exec-auth-stamp.cmd` directly (no `.py`
segment to strip). The repo-wide bare-entrypoint guard
(coordinator_core/test_bin_launcher_parity.py's
`test_bare_entrypoints_have_cmd_launcher`) already covers `.cmd`-twin
*existence* for bare files like this one, but not shebang-correctness or
that the `.cmd` invokes THIS specific file rather than some other script —
this suite closes that gap for this one assembler.

Test coverage:
  T1 .cmd sibling exists, co-located with the entrypoint
  T2 line-1 shebang is `#!/usr/bin/env python3`
  T3 the `.cmd` body invokes THIS entrypoint's filename (parity of
     behaviour, not merely of file existence)
"""
from __future__ import annotations

import os
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "review-exec-auth-stamp")


class CrossPlatformParityTests(unittest.TestCase):
    """DR-076 cross-platform invocation-parity guard, scoped to THIS
    assembler.

    NEGATIVE SPEC: does not re-implement the repo-wide `.cmd`-existence
    sweep (coordinator_core/test_bin_launcher_parity.py) — deliberately
    narrow to one assembler, matching the "targeted per-assembler coverage
    instead" instruction. Widening the repo-wide guards' globs is out of
    scope here; a separate, uncommitted attempt at that is red elsewhere
    in this tree.
    """

    def test_cmd_sibling_exists(self):
        cmd_path = _CLI + ".cmd"
        self.assertTrue(
            os.path.isfile(cmd_path),
            f"missing co-located Windows launcher: {cmd_path}",
        )

    def test_shebang_is_python3(self):
        with open(_CLI, encoding="utf-8") as fh:
            first_line = fh.readline().rstrip("\n")
        self.assertEqual(
            first_line,
            "#!/usr/bin/env python3",
            "line-1 shebang must be the DR-076 python3 shape",
        )

    def test_cmd_invokes_same_entrypoint(self):
        cmd_path = _CLI + ".cmd"
        with open(cmd_path, encoding="utf-8") as fh:
            content = fh.read()
        entrypoint_name = os.path.basename(_CLI)
        self.assertIn(
            f'"%~dp0{entrypoint_name}" %*',
            content,
            f".cmd body does not invoke {entrypoint_name} — parity broken "
            "(a .cmd that exists but launches something else is the exact "
            "defect this test guards against)",
        )


if __name__ == "__main__":
    unittest.main()
