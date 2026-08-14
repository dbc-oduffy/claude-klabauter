"""bin/tests/test_percolate_parse_dryrun_parity.py

Purpose: DR-076 cross-platform invocation-parity guard for
coordinator/bin/percolate-parse-dryrun.py — the /percolate skill's
dry-run-parse assembler (DoE-claude coordinator/skills/percolate/SKILL.md,
plan 2026-07-24-computed-skills-b8-review-ci-cluster.md chunk b8-C5).

This assembler had NO test file at all as of the 2026-07-25 DR-076 audit
(unlike parallel-review-gate-decision.py, which had unit tests but zero
platform assertions). This file closes the parity gap only — it does not
attempt to backfill unit coverage of the assembler's own parse/gate logic,
which is out of scope for this dispatch.

See docs/wiki/cross-platform-invocation-parity.md — the canonical shape is
a `#!/usr/bin/env python3`-shebang entrypoint plus a co-located `.cmd`
sibling, never a bareword-through-a-shell. The repo-wide guards
(coordinator_core/test_bin_launcher_parity.py,
coordinator/bin/tests/test_no_bin_polyglot_invariant.py) only assert
`.cmd`-twin *existence* and deliberately skip `.py`-suffixed files in one
case — this is exactly why this assembler slipped through both. This suite
asserts existence AND shebang-correctness AND that the `.cmd` actually
launches THIS file, not merely that some `.cmd` exists.

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
_CLI = os.path.join(_BIN_DIR, "percolate-parse-dryrun.py")


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
        cmd_path = _CLI[: -len(".py")] + ".cmd"
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
        cmd_path = _CLI[: -len(".py")] + ".cmd"
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
