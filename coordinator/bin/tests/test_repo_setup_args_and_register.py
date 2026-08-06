"""test_repo_setup_args_and_register.py — unit coverage for
`coordinator/bin/repo-setup-args-and-register.py`, the naked-Python port of
the residual bash logic previously embedded in example-doctrine-repo's
`coordinator/skills/repo-setup/SKILL.md` (2026-07-23 debash campaign,
chunk C-REPOSETUP).

Covers the four subcommands' pure-logic seams (arg extraction, repo-key
derivation, target-root validation, exec-summary path-fallback ladder) plus
the fail-loud branches (missing dir, non-worktree dir, unresolvable
machine-local CLI). The `register-repo` subcommand's machine-local calls are
monkeypatched at the `coordinator_core.install._shared` import seam so this
suite never mutates the real machine-local registry.

Loaded by file path (`importlib.util.spec_from_file_location`) since the
module lives at a hyphenated filename outside any package.

Run:
    pytest coordinator/bin/tests/test_repo_setup_args_and_register.py -v
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_PATH = _BIN_DIR / "repo-setup-args-and-register.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "repo_setup_args_and_register_test", str(_MODULE_PATH)
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_cli = _load_module()


class TestExtractRootArg(unittest.TestCase):
    def test_root_flag(self):
        self.assertEqual(_cli.extract_root_arg("--root /x/y"), "/x/y")

    def test_target_alias(self):
        self.assertEqual(_cli.extract_root_arg("--target /x/y"), "/x/y")

    def test_embedded_in_larger_string(self):
        self.assertEqual(
            _cli.extract_root_arg("some prefix --root /a/b trailing words"),
            "/a/b",
        )

    def test_last_occurrence_wins_greedy(self):
        # Mirrors sed -En's greedy .* — matches the LAST --root/--target.
        self.assertEqual(
            _cli.extract_root_arg("--root /first --root /second"),
            "/second",
        )

    def test_absent_returns_empty(self):
        self.assertEqual(_cli.extract_root_arg("no flags here"), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(_cli.extract_root_arg(""), "")


class TestDeriveRepoKey(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(_cli.derive_repo_key("MyRepo"), "myrepo")

    def test_dashes_collapse_to_underscore(self):
        self.assertEqual(_cli.derive_repo_key("claude-klabauter"), "claude_klabauter")

    def test_runs_of_non_alnum_collapse_to_single_underscore(self):
        self.assertEqual(_cli.derive_repo_key("My-Repo Name!!Test"), "my_repo_name_test")

    def test_leading_trailing_underscores_stripped(self):
        self.assertEqual(_cli.derive_repo_key("_leading_and_trailing_"), "leading_and_trailing")

    def test_matches_cross_repo_memo_receiver_key_shape(self):
        # cross-repo-memo's _receiver_repo_key resolves "--to foo-em" via
        # shortname.replace("-", "_") -> repos.<underscored>; derive_repo_key
        # must produce the same underscored form for a plain hyphenated name.
        self.assertEqual(_cli.derive_repo_key("foo-bar"), "foo_bar")


class TestResolveTargetRoot(unittest.TestCase):
    def test_defaults_to_cwd_when_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            parser = _cli.build_parser()
            args = parser.parse_args(["resolve-target-root", "--arguments", "", "--cwd", tmp])
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf[0], os.path.abspath(tmp))

    def test_uses_explicit_root_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            parser = _cli.build_parser()
            args = parser.parse_args(
                ["resolve-target-root", "--arguments", f"--root {tmp}", "--cwd", "/tmp"]
            )
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf[0], os.path.abspath(tmp))

    def test_fails_loud_on_missing_dir(self):
        parser = _cli.build_parser()
        args = parser.parse_args(
            ["resolve-target-root", "--arguments", "--root /definitely/not/a/real/path/xyz"]
        )
        rc = args.func(args)
        self.assertEqual(rc, 1)

    def test_fails_loud_on_non_worktree_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No `git init` — plain dir, not inside any work tree once HOME/CI
            # ancestry is excluded. Guard against the rare case a temp dir
            # happens to sit inside a parent git repo by asserting the git
            # probe itself, not a filesystem assumption.
            is_worktree = _cli._is_git_worktree(tmp)
            if is_worktree:
                self.skipTest("temp dir unexpectedly resolves inside a git work tree")
            parser = _cli.build_parser()
            args = parser.parse_args(["resolve-target-root", "--arguments", f"--root {tmp}"])
            rc = args.func(args)
            self.assertEqual(rc, 1)


class TestResolveExecSummaryGenerator(unittest.TestCase):
    def test_prints_coordinator_root_copy_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            gen = bin_dir / "generate-exec-summary.py"
            gen.write_text("# stub\n")
            parser = _cli.build_parser()
            args = parser.parse_args(["resolve-exec-summary-generator", "--coordinator-root", tmp])
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf[0], str(gen))

    def test_falls_back_to_claude_klabauter_sibling_copy(self):
        with tempfile.TemporaryDirectory() as coord_tmp, tempfile.TemporaryDirectory() as claude_klabauter_tmp:
            # coordinator-root copy deliberately absent.
            claude_klabauter_gen_dir = Path(claude_klabauter_tmp) / "coordinator" / "bin"
            claude_klabauter_gen_dir.mkdir(parents=True)
            claude_klabauter_gen = claude_klabauter_gen_dir / "generate-exec-summary.py"
            claude_klabauter_gen.write_text("# stub\n")
            parser = _cli.build_parser()
            args = parser.parse_args(
                ["resolve-exec-summary-generator", "--coordinator-root", coord_tmp]
            )
            with mock.patch.dict(os.environ, {"REPO_CLAUDE_KLABAUTER": claude_klabauter_tmp}, clear=False):
                buf = []
                with mock.patch(
                    "builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))
                ):
                    rc = args.func(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf[0], str(claude_klabauter_gen))

    def test_exits_1_when_unresolvable(self):
        with tempfile.TemporaryDirectory() as coord_tmp, tempfile.TemporaryDirectory() as home_tmp, tempfile.TemporaryDirectory() as settings_tmp:
            parser = _cli.build_parser()
            args = parser.parse_args(
                [
                    "resolve-exec-summary-generator",
                    "--coordinator-root",
                    coord_tmp,
                    "--settings-home",
                    settings_tmp,
                ]
            )
            with mock.patch.dict(os.environ, {"CLAUDE_HOME": home_tmp}, clear=False):
                # Isolate from this machine's REAL claude-klabauter-root registration
                # (both env-var and pointer-file rungs) so the fallback
                # ladder genuinely bottoms out — otherwise this test would
                # spuriously pass/fail depending on the running machine's
                # own machine-local registry state.
                os.environ.pop("REPO_CLAUDE_KLABAUTER", None)
                os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
                rc = args.func(args)
            self.assertEqual(rc, 1)


class TestRegisterRepo(unittest.TestCase):
    def test_already_registered_short_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _cli.build_parser()
            args = parser.parse_args(["register-repo", "--path", tmp])
            fake_shared = mock.MagicMock()
            fake_shared.resolve_machine_local_cli.return_value = ["machine-local"]
            fake_shared.ml_get.return_value = "/already/there"
            with mock.patch.dict(sys.modules, {"coordinator_core.install._shared": fake_shared}):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            fake_shared.ml_set.assert_not_called()

    def test_check_only_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _cli.build_parser()
            args = parser.parse_args(["register-repo", "--path", tmp, "--check-only"])
            fake_shared = mock.MagicMock()
            fake_shared.resolve_machine_local_cli.return_value = ["machine-local"]
            fake_shared.ml_get.return_value = ""
            with mock.patch.dict(sys.modules, {"coordinator_core.install._shared": fake_shared}):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            fake_shared.ml_set.assert_not_called()

    def test_registers_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _cli.build_parser()
            args = parser.parse_args(["register-repo", "--path", tmp])
            fake_shared = mock.MagicMock()
            fake_shared.resolve_machine_local_cli.return_value = ["machine-local"]
            fake_shared.ml_get.return_value = ""
            fake_shared.ml_set.return_value = True
            with mock.patch.dict(sys.modules, {"coordinator_core.install._shared": fake_shared}):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            expected_key = f"repos.{_cli.derive_repo_key(os.path.basename(os.path.abspath(tmp)))}"
            fake_shared.ml_set.assert_called_once()
            called_args, called_kwargs = fake_shared.ml_set.call_args
            self.assertEqual(called_args[0], expected_key)
            self.assertEqual(called_args[1], os.path.abspath(tmp))

    def test_unresolvable_machine_local_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _cli.build_parser()
            args = parser.parse_args(["register-repo", "--path", tmp])
            fake_shared = mock.MagicMock()
            fake_shared.resolve_machine_local_cli.return_value = None
            with mock.patch.dict(sys.modules, {"coordinator_core.install._shared": fake_shared}):
                rc = args.func(args)
            self.assertEqual(rc, 2)
            fake_shared.ml_set.assert_not_called()


class TestWhoamiStatus(unittest.TestCase):
    def test_ready_when_importable(self):
        parser = _cli.build_parser()
        args = parser.parse_args(["whoami-status", "--python", sys.executable])
        fake_probe = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=fake_probe) as run_mock:
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf[0], "whoami_status: ready")
            run_mock.assert_called_once()

    def test_would_install_under_check_only(self):
        parser = _cli.build_parser()
        args = parser.parse_args(["whoami-status", "--python", sys.executable, "--check-only"])
        fake_probe = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=fake_probe):
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertEqual(buf[0], "whoami_status: would-install")

    def test_installed_on_successful_pip(self):
        parser = _cli.build_parser()
        args = parser.parse_args(["whoami-status", "--python", sys.executable])
        probe_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        pip_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=[probe_fail, pip_ok]):
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertEqual(buf[0], "whoami_status: installed")

    def test_failed_on_pip_error_never_exits_nonzero(self):
        parser = _cli.build_parser()
        args = parser.parse_args(["whoami-status", "--python", sys.executable])
        probe_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        pip_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", side_effect=[probe_fail, pip_fail]):
            buf = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(" ".join(str(x) for x in a))):
                rc = args.func(args)
        # Never halts the caller — matches the original's never-block contract.
        self.assertEqual(rc, 0)
        self.assertEqual(buf[0], "whoami_status: failed")
        self.assertIn("boom", buf[1])


if __name__ == "__main__":
    unittest.main()
