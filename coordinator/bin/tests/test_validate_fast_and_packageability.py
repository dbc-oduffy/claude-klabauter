#!/usr/bin/env python3
# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/tests/test_validate_fast_and_packageability.py

Purpose: Unit tests for validate-fast-and-packageability.py -- the ported
fast-test resolution ladder (mktemp-diagnostic-capture equivalent + rc==2/
126/other-nonzero/0 classification) and the packageability
loud-skip-vs-silent-pass guard, both lifted out of example-doctrine-repo's
coordinator/skills/validate/SKILL.md bash fences.

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (M3 chunk
C-VALIDATE)

Test coverage:
  fast subcommand:
    T1  skip-with-notice (resolver rc=2) -> Validation: skipped, exit 0
    T2  config-malformed (resolver rc=126) -> Validation: config-malformed, exit 1
    T3  interp-missing (resolver rc=127) -> Validation: interp-missing, exit 1
    T4  resolved + command passes (rc=0) -> Validation: 0, exit 0
    T5  resolved + command fails (rc=3) -> Validation: 3, exit 3
  packageability subcommand:
    T6  validate-install-contract.py present -> forwards exit code verbatim
    T7  validate-install-contract.py absent -> loud WARN skip, exit 0
  metachar guard + direct-exec (2026-07-29 debash pass, Group B -- the fast
  subcommand's exec path switched from `bash -c <cmd>` to a direct (no-shell)
  `shlex.split` + subprocess exec):
    T8  _fail_on_ambiguous_shell_syntax refuses pipe/chain/redirect/
        substitution syntax and passes plain quoted commands through
    T9  _run_resolved_command executes a plain argv command directly (no
        bash needed) and preserves the rc=127 "command not found" contract
        for an unresolvable first token
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "validate-fast-and-packageability.py")


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("_vfp_metachar_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run(args: list[str], env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, _CLI, *args],
        env=full_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


class FastSubcommandTest(unittest.TestCase):
    def test_t1_skip_with_notice(self) -> None:
        env = {"COORDINATOR_FAST_TEST_CMD": ""}
        # Ensure no coordinator.local.md at /tmp interferes.
        proc = _run(["fast", "--repo-root", "/tmp"], env=env)
        self.assertIn("Validation: skipped", proc.stdout)
        self.assertEqual(proc.returncode, 0)

    def test_t2_config_malformed(self) -> None:
        env = {"COORDINATOR_FAST_TEST_CMD": r'echo \"hi'}
        proc = _run(["fast", "--repo-root", "/tmp"], env=env)
        self.assertIn("Validation: config-malformed", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_t3_interp_missing(self) -> None:
        env = {
            "COORDINATOR_FAST_TEST_CMD": "python -m pytest",
            "PATH": "/nonexistent-empty-path-dir",
        }
        proc = _run(["fast", "--repo-root", "/tmp"], env=env)
        self.assertIn("Validation: interp-missing", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_t4_resolved_command_passes(self) -> None:
        env = {"COORDINATOR_FAST_TEST_CMD": "true"}
        proc = _run(["fast", "--repo-root", "/tmp"], env=env)
        self.assertIn("Validation: 0", proc.stdout)
        self.assertEqual(proc.returncode, 0)

    def test_t5_resolved_command_fails(self) -> None:
        # `exit 3` is two tokens, so (unlike the single-token `true` in T4)
        # it clears `_configured_test_cmds`' well-formedness filter and gets
        # classified Tier F (`full_test_cmd` fallback -- no separate
        # `full_test_cmd` configured, so the resolver's own rc=3 fallback
        # makes the fast string the full string too). Per PM ruling
        # 2026-08-04 (cross-repo/archive/2026-07-25-example-doctrine-repo-em-validate-
        # tier-u-shape-ruling.md's amendment), Tier F is no longer exempt
        # from the grant requirement -- the Tier-U gate now refuses this
        # BEFORE the child ever runs, still on exit 3 (the gate's own
        # refusal code), but for a different reason than the child's actual
        # exit code, which this call site never reaches.
        env = {"COORDINATOR_FAST_TEST_CMD": "exit 3"}
        proc = _run(["fast", "--repo-root", "/tmp"], env=env)
        self.assertIn("Validation: tier-u-refused", proc.stdout)
        self.assertEqual(proc.returncode, 3)

    def test_t8_diff_scoped_branch_executes_without_raising(self) -> None:
        """Regression: `run_fast` binds a LOCAL `diag = diag_buf.getvalue()`
        (the resolver's captured stderr, a str) further down the function.
        An unaliased `from coordinator_core.diff_scoped_tests import diag`
        would be shadowed function-wide, so BOTH diff-scoped diagnostic call
        sites (non-empty `diff_paths`) would raise
        `TypeError: 'str' object is not callable` -- on exactly the path
        the feature exists for. The 14 tests in
        coordinator_core/test_diff_scoped_tests.py exercise
        `diff_scoped_tests` directly and never drove this wired path, so a
        green suite there missed a broken integration entirely. This test
        drives `run_fast` in-process (monkeypatching `find_changed_test_files`
        to return a non-empty set, matching the module's own T7 in-process
        pattern) so the `if diff_paths:` branch and its diagnostic call
        actually execute here.
        """
        import importlib.util

        from coordinator_core.session.tier_u_gate import TierUGateResult

        spec = importlib.util.spec_from_file_location("_vfp_diag_regress", _CLI)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        # This repo's real fast_test_cmd carries this exact marker
        # expression -- reused here (rather than an arbitrary string) so
        # the assertion below is meaningful for the "-m survives on the
        # wired path" requirement, not just "some string survives".
        marker_expr = "-m 'not cadence and not pending_fix and not designed_red'"
        resolved_cmd = f"true {marker_expr}"

        class _FakeResolveResult:
            def __init__(self, stdout: str, returncode: int) -> None:
                self.stdout = stdout
                self.returncode = returncode

        # Bypass resolution (no coordinator.local.md / env needed) and the
        # Tier-U gate (its own classification ladder is a separate concern
        # from this file's wiring bug -- see coordinator_core/test_diff_
        # scoped_tests.py and the sidecar note on Tier F/U classification
        # for that coverage) so this test isolates the diff-scoping wiring.
        mod._resolver.resolve_fast_test_cmd = lambda repo_root: _FakeResolveResult(
            resolved_cmd + "\n", 0
        )
        mod.find_changed_test_files = lambda repo_root: ["pkg/test_changed.py"]
        mod.enforce_tier_u_gate = lambda cmd, repo_root=None: TierUGateResult(
            proceed=True, refusal_message=None
        )
        captured_cmds: list[str] = []

        def _fake_run_resolved_command(cmd: str) -> int:
            captured_cmds.append(cmd)
            return 0

        mod._run_resolved_command = _fake_run_resolved_command

        validation_result, exit_code = mod.run_fast("/tmp")

        self.assertEqual(exit_code, 0)
        self.assertEqual(validation_result, "0")
        self.assertEqual(len(captured_cmds), 1)
        # The command actually handed to the runner -- not just the
        # `append_test_paths` return value in isolation -- still carries
        # both the appended path and the untouched marker expression.
        self.assertIn("pkg/test_changed.py", captured_cmds[0])
        self.assertIn(marker_expr, captured_cmds[0])
        self.assertTrue(captured_cmds[0].startswith(resolved_cmd))


class PackageabilitySubcommandTest(unittest.TestCase):
    def test_t6_forwards_exit_code(self) -> None:
        proc = _run(["packageability", "--", "--manifest-path", "/nonexistent-manifest.json"])
        self.assertIn("Packageability: 0", proc.stdout)
        self.assertEqual(proc.returncode, 0)

    def test_t7_loud_skip_when_absent(self) -> None:
        # Simulate a partial/stale checkout (validate-install-contract.py
        # missing from an otherwise-valid bin/ dir) by monkeypatching the
        # resolved script path in-process -- exercised directly against the
        # imported module rather than a scratch-copied subprocess, so the
        # module's own coordinator/lib import resolution (relative to its
        # real on-disk location) stays intact.
        import importlib.util

        spec = importlib.util.spec_from_file_location("_vfp_under_test", _CLI)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        orig = mod._VALIDATE_INSTALL_CONTRACT
        mod._VALIDATE_INSTALL_CONTRACT = os.path.join(orig + "-does-not-exist")
        try:
            exit_code, warn = mod.run_packageability([])
        finally:
            mod._VALIDATE_INSTALL_CONTRACT = orig

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(warn)
        self.assertIn("WARN: Packageability check SKIPPED", warn)


class MetacharGuardAndDirectExecTest(unittest.TestCase):
    def test_t8_plain_quoted_command_allowed(self) -> None:
        mod = _load_cli_module()
        # Must not raise -- ordinary shell-quoting (a quoted sub-argument) is
        # not a shell metacharacter and is exactly the shape shlex.split is
        # meant to parse without a shell.
        mod._fail_on_ambiguous_shell_syntax("pytest -m 'not slow and not integration'")

    def test_t8_shell_syntax_refused(self) -> None:
        mod = _load_cli_module()
        for bad_cmd in (
            "pytest -m a && pytest -m b",
            "pytest -m a || pytest -m b",
            "pytest | tee out.log",
            "pytest -m a; echo done",
            "pytest > out.log",
            "pytest < input.txt",
            "echo `date`",
            "echo $(date)",
        ):
            with self.assertRaises(mod.AmbiguousShellSyntax, msg=bad_cmd):
                mod._fail_on_ambiguous_shell_syntax(bad_cmd)

    def test_t9_direct_exec_runs_without_shell(self) -> None:
        mod = _load_cli_module()
        cmd = f"{mod.shlex.quote(sys.executable)} -c \"import sys; sys.exit(0)\""
        self.assertEqual(mod._run_resolved_command(cmd), 0)

        cmd = f"{mod.shlex.quote(sys.executable)} -c \"import sys; sys.exit(3)\""
        self.assertEqual(mod._run_resolved_command(cmd), 3)

    def test_t9_unresolvable_first_token_returns_127(self) -> None:
        mod = _load_cli_module()
        exit_code = mod._run_resolved_command("this-binary-does-not-exist-anywhere-12345")
        self.assertEqual(exit_code, 127)


if __name__ == "__main__":
    unittest.main()
