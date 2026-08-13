"""bin/tests/test_parallel_review_gate_decision.py

Purpose: Unit tests for parallel-review-gate-decision.py — the gate-decision
assembler ported from coordinator/skills/parallel-code-review/SKILL.md's
Gating Rules 1-4, Chunking algorithm, and $RESOLVER_EXIT branch table
(coordinator-claude plan 2026-07-24-computed-skills-b8-review-ci-cluster.md chunk C3).

Test coverage:
  T1  compute_gate_decision — Rule 4 (--force) wins over everything
  T2  compute_gate_decision — Rule 1 (tiny diff)
  T3  compute_gate_decision — Rule 1 (internal-only paths, >=10 lines)
  T4  compute_gate_decision — Rule 3 (plan-only)
  T5  compute_gate_decision — Rule 2 (doc-only)
  T6  compute_gate_decision — default (mixed code+docs)
  T7  compute_rule5_inputs — unreviewed_set excludes SHAs with a trail record
  T8  compute_chunks — seam nucleus stays whole with its co-toucher
  T9  compute_chunks — disjoint-by-file-scope (no file in two chunks)
  T10 resolver-branch CLI — 0/2/3 branch correctly (8-key envelope,
      decisions.resolver_branch.action); unrecognized code exits 1
  T11 gate CLI — 8-key envelope, decisions.gate mirrors compute_gate_decision,
      judgment_points empty (Rules 1-4 are mechanical, never paused)
  T12 rule5-inputs CLI — exactly one judgment_points entry
      (jp_rule5_skip_vs_narrow), no recommendation key value set (structurally
      absent from build_untrusted_gate_judgment_point's output)
  T13-T15 CrossPlatformParityTests — DR-076 invocation-parity guard for this
      specific assembler (docs/wiki/cross-platform-invocation-parity.md):
      .cmd sibling exists, line-1 shebang is python3, and the .cmd body
      actually invokes THIS entrypoint's filename (not merely that some
      .cmd file exists — the entrypoint predates this suite's
      test_no_bin_polyglot_invariant.py / test_bin_launcher_parity.py gates,
      which skip .py-suffixed files by design and so never covered this
      assembler; see the dispatch brief's audit).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: the resolver-branch/gate/rule5-inputs CLI tests
# below (T10-T12) spawn a real `sys.executable` child running
# parallel-review-gate-decision.py because the property under test is the
# CLI's real argv-parsing/8-key-envelope/exit-code contract at the process
# boundary -- an in-process call cannot observe that. `test_gate_cli_envelope`
# additionally runs against this repo's own real git history
# (`--range HEAD~1..HEAD`), exercising the gate's actual range-resolution
# path. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "parallel-review-gate-decision.py")

_spec = importlib.util.spec_from_file_location("parallel_review_gate_decision", _CLI)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


class GateDecisionTests(unittest.TestCase):
    def test_rule4_force_wins(self):
        d = _mod.compute_gate_decision(["docs/plans/x.md"], 500, force=True)
        self.assertEqual(d["rule"], "4")
        self.assertEqual(d["action"], "bypass")

    def test_rule1_tiny_diff(self):
        d = _mod.compute_gate_decision(["src/a.py"], 4, force=False)
        self.assertEqual(d["rule"], "1")
        self.assertEqual(d["action"], "skip_gate")

    def test_rule1_internal_only(self):
        d = _mod.compute_gate_decision(["tasks/foo/todo.md", "tmp/scratch.txt"], 40, force=False)
        self.assertEqual(d["rule"], "1")

    def test_rule3_plan_only(self):
        d = _mod.compute_gate_decision(["docs/plans/2026-07-24-foo.md"], 200, force=False)
        self.assertEqual(d["rule"], "3")
        self.assertEqual(d["action"], "skip_gate")

    def test_rule2_doc_only(self):
        d = _mod.compute_gate_decision(["docs/wiki/tiered-context-loading.md"], 200, force=False)
        self.assertEqual(d["rule"], "2")
        self.assertEqual(d["action"], "skip_code_semantics")

    def test_default_mixed(self):
        d = _mod.compute_gate_decision(["src/a.ts", "docs/wiki/b.md"], 1000, force=False)
        self.assertEqual(d["rule"], "default")
        self.assertEqual(d["action"], "run_default")

    def test_rule5_inputs_unreviewed_set(self):
        scope_shas = ["sha1", "sha2", "sha3"]
        seam_files = ["src/shared.py"]
        trail_records = [{"sha": "sha1", "workstream": "ws-a"}]
        result = _mod.compute_rule5_inputs(scope_shas, seam_files, trail_records)
        self.assertEqual(result["unreviewed_set"], ["sha2", "sha3"])
        self.assertEqual(result["unreviewed_count"], 2)
        self.assertEqual(result["commit_count"], 3)
        self.assertEqual(result["seam_file_count"], 1)
        self.assertEqual(result["workstream_coverage"], {"ws-a": True})

    def test_chunk_seam_nucleus_stays_whole(self):
        scope_files = ["shared/seam.py", "shared/companion.py", "other/lonely.py"]
        seam_manifest = [
            ("shared/seam.py", "session-a"),
            ("shared/seam.py", "session-b"),
            ("shared/companion.py", "session-a"),
        ]
        result = _mod.compute_chunks(scope_files, seam_manifest, target_size=25)
        self.assertEqual(result["seam_nucleus_count"], 1)
        nucleus_chunk = [c for c in result["chunks"].values() if "shared/seam.py" in c][0]
        self.assertIn("shared/companion.py", nucleus_chunk)

    def test_chunk_disjoint_by_file_scope(self):
        scope_files = [f"pkg/f{i}.py" for i in range(30)]
        seam_manifest = [("pkg/f0.py", "s1"), ("pkg/f0.py", "s2")]
        result = _mod.compute_chunks(scope_files, seam_manifest, target_size=10)
        seen = []
        for files in result["chunks"].values():
            seen.extend(files)
        self.assertEqual(len(seen), len(set(seen)), "a file appeared in two chunks")
        self.assertEqual(set(seen), set(scope_files))

    def test_resolver_branch_cli(self):
        for code, expected_action in ((0, "run_full"), (2, "skip"), (3, "run_fast_fallback")):
            proc = subprocess.run(
                [sys.executable, _CLI, "resolver-branch", "--resolver-exit", str(code)],
                capture_output=True,
                text=True,
                **no_console_creationflags(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json

            envelope = json.loads(proc.stdout)
            self.assertEqual(
                set(envelope.keys()),
                {
                    "artifact",
                    "preflight",
                    "gates",
                    "directives",
                    "judgment_points",
                    "decisions",
                    "narration",
                    "next_move",
                },
            )
            self.assertEqual(envelope["decisions"]["resolver_branch"]["action"], expected_action)

        proc = subprocess.run(
            [sys.executable, _CLI, "resolver-branch", "--resolver-exit", "99"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(proc.returncode, 1)

    def test_gate_cli_envelope(self):
        import json

        proc = subprocess.run(
            [sys.executable, _CLI, "gate", "--range", "HEAD~1..HEAD", "--force"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
            cwd=_BIN_DIR,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["decisions"]["gate"]["rule"], "4")
        self.assertEqual(envelope["decisions"]["gate"]["action"], "bypass")
        self.assertEqual(envelope["judgment_points"], [])

    def test_rule5_inputs_cli_judgment_point(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            shas_file = os.path.join(td, "shas.txt")
            seam_file = os.path.join(td, "seam.txt")
            trail_dir = os.path.join(td, "trail")
            os.makedirs(trail_dir, exist_ok=True)
            with open(shas_file, "w", encoding="utf-8") as fh:
                fh.write("sha1\nsha2\n")
            with open(seam_file, "w", encoding="utf-8") as fh:
                fh.write("src/shared.py\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    _CLI,
                    "rule5-inputs",
                    "--scope-shas-file",
                    shas_file,
                    "--seam-files-file",
                    seam_file,
                    "--review-trail-dir",
                    trail_dir,
                ],
                capture_output=True,
                text=True,
                **no_console_creationflags(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            envelope = json.loads(proc.stdout)
            jps = envelope["judgment_points"]
            self.assertEqual(len(jps), 1)
            self.assertEqual(jps[0]["id"], "jp_rule5_skip_vs_narrow")
            self.assertIsNone(jps[0]["recommendation"])
            dispositions = {d["value"] for d in jps[0]["dispositions"]}
            self.assertEqual(dispositions, {"narrow", "skip"})


class CrossPlatformParityTests(unittest.TestCase):
    """DR-076 cross-platform invocation-parity guard, scoped to THIS
    assembler. See docs/wiki/cross-platform-invocation-parity.md — the
    canonical shape is a `#!/usr/bin/env python3`-shebang entrypoint plus a
    co-located `.cmd` sibling, never a bareword-through-a-shell. The
    repo-wide guards (coordinator_core/test_bin_launcher_parity.py,
    coordinator/bin/tests/test_no_bin_polyglot_invariant.py) only assert
    `.cmd`-twin *existence*; neither asserts the shebang is intact on THIS
    file nor that the `.cmd` actually launches THIS file's basename rather
    than some other script. This class closes that specific gap.

    NEGATIVE SPEC: does not re-implement the repo-wide `.cmd`-existence
    sweep — this class is deliberately narrow to one assembler, matching
    the "targeted per-assembler coverage instead" instruction (widening the
    repo-wide guards' globs is explicitly out of scope here; a separate,
    uncommitted attempt at that is red elsewhere in this tree).
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
