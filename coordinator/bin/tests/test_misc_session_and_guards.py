"""test_misc_session_and_guards.py — unit coverage for
coordinator/bin/misc-session-and-guards.py (M3 chunk C-MISC port).

Covers the pure-logic paths that were ported off coordinator-claude instruction-file
bash fences: the claim-error peer-vs-infra classifier, the rag-freshness-gate
check-rag-state/generate-repomap branching, the ~/.claude.json example-retrieval-repo
CLI/root resolution + silent-skip contract, and the autonomous-sentinel
enable/disable fail-loud-vs-idempotent branches. The autonomous-sentinel
tests monkeypatch `_import_resolve_session_id` so this suite never requires
CLAUDE_KLABAUTER_ROOT to resolve or `coordinator_core` to be importable — same idiom
used by test_archive_stamp_cli_ship_handoff.py / test_session_claim_cli.py
for the `_import_module()` seam.

Run:
    python -m pytest coordinator/bin/tests/test_misc_session_and_guards.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "misc_session_and_guards_test", str(_BIN_DIR / "misc-session-and-guards.py")
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class ClaimClassifyTests(unittest.TestCase):
    def test_peer_contention_marker(self):
        self.assertEqual(
            _cli.classify_claim_error("ERROR: plan claim held by session abc123"),
            "peer-contention",
        )

    def test_infra_error_default(self):
        self.assertEqual(
            _cli.classify_claim_error("ERROR: cannot resolve session id"),
            "infra-error",
        )

    def test_cmd_claim_classify_always_exits_1_and_prints_verdict(self):
        stdin = io.StringIO("held by session xyz\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
            rc = _cli.main(["claim-classify"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue().strip(), "peer-contention")
        self.assertIn("STOP: plan claim error", stderr.getvalue())
        self.assertIn("held by session xyz", stderr.getvalue())

    def test_cmd_claim_classify_infra_error_path(self):
        stdin = io.StringIO("some other transport failure")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
            rc = _cli.main(["claim-classify"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue().strip(), "infra-error")


class ParseKvFlagsTests(unittest.TestCase):
    def test_order_independent(self):
        out = _cli._parse_kv_flags(
            ["--task", "do the thing", "--project-root", "/tmp/x"],
            ("--project-root", "--task", "--focus-files"),
        )
        self.assertEqual(out, {"task": "do the thing", "project-root": "/tmp/x"})

    def test_missing_value_raises(self):
        with self.assertRaises(ValueError):
            _cli._parse_kv_flags(["--task"], ("--task",))


class RagFreshnessGateTests(unittest.TestCase):
    def test_fresh_short_circuits_without_generate_repomap(self):
        fake_check = mock.Mock(returncode=0, stdout="fresh\n")
        with mock.patch.object(_cli.subprocess, "run", return_value=fake_check) as run_mock:
            rc = _cli._cmd_rag_freshness_gate([])
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()  # only check-rag-state.py, no repomap call

    def test_stale_but_generate_repomap_missing_skips_with_message(self):
        fake_check = mock.Mock(returncode=0, stdout="stale\n")
        stderr = io.StringIO()
        with mock.patch.object(_cli.subprocess, "run", return_value=fake_check), \
             mock.patch.object(Path, "is_file", return_value=False), \
             redirect_stderr(stderr):
            rc = _cli._cmd_rag_freshness_gate(["--project-root", "/proj"])
        self.assertEqual(rc, 0)
        self.assertIn("task-scoped repomap skipped", stderr.getvalue())

    def test_stale_and_generate_repomap_present_invokes_it_with_forwarded_args(self):
        fake_check = mock.Mock(returncode=0, stdout="stale\n")
        fake_gen = mock.Mock(returncode=0)
        calls = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            return fake_check if "check-rag-state.py" in cmd[1] else fake_gen

        with mock.patch.object(_cli.subprocess, "run", side_effect=_run), \
             mock.patch.object(Path, "is_file", return_value=True):
            rc = _cli._cmd_rag_freshness_gate(
                ["--project-root", "/proj", "--task", "summary", "--focus-files", "a.py,b.py"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        gen_call = calls[1]
        self.assertIn("--project-root", gen_call)
        self.assertIn("/proj", gen_call)
        self.assertIn("--task", gen_call)
        self.assertIn("summary", gen_call)
        self.assertIn("--focus-files", gen_call)
        self.assertIn("a.py,b.py", gen_call)

    def test_check_rag_state_failure_defaults_to_unknown_and_treated_as_not_fresh(self):
        fake_check = mock.Mock(returncode=1, stdout="")
        stderr = io.StringIO()
        with mock.patch.object(_cli.subprocess, "run", return_value=fake_check), \
             mock.patch.object(Path, "is_file", return_value=False), \
             redirect_stderr(stderr):
            rc = _cli._cmd_rag_freshness_gate([])
        self.assertEqual(rc, 0)
        self.assertIn("task-scoped repomap skipped", stderr.getvalue())


class RagStalenessSurveyTests(unittest.TestCase):
    def test_unresolvable_paths_silent_skip(self):
        with mock.patch.object(
            _cli, "_resolve_example_retrieval_repo_cli_and_root", return_value=(None, None)
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = _cli._cmd_rag_staleness_survey([])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_current_verdict_silent_skip(self):
        fake_proc = mock.Mock(
            returncode=0, stdout=json.dumps({"verdict": "current"})
        )
        with mock.patch.object(
            _cli, "_resolve_example_retrieval_repo_cli_and_root", return_value=("/rag/cli.py", "/proj")
        ), mock.patch.object(_cli.subprocess, "run", return_value=fake_proc):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = _cli._cmd_rag_staleness_survey([])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_stale_verdict_prints_nudge_line(self):
        fake_proc = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {"verdict": "stale", "age": "3 days", "recommendation_command": "/refresh-rag"}
            ),
        )
        with mock.patch.object(
            _cli, "_resolve_example_retrieval_repo_cli_and_root", return_value=("/rag/cli.py", "/proj")
        ), mock.patch.object(_cli.subprocess, "run", return_value=fake_proc):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = _cli._cmd_rag_staleness_survey([])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("3 days", out)
        self.assertIn("stale", out)
        self.assertIn("/refresh-rag", out)

    def test_resolve_example_retrieval_repo_cli_and_root_reads_claude_json(self):
        fake_config = {
            "mcpServers": {
                "example-retrieval-repo": {
                    "args": ["-m", "example_retrieval_repo.cli", "/some/project/root"]
                }
            }
        }
        m = mock.mock_open(read_data=json.dumps(fake_config))
        with mock.patch.object(Path, "open", m):
            cli, root = _cli._resolve_example_retrieval_repo_cli_and_root()
        self.assertEqual(cli, "example_retrieval_repo.cli")  # endswith("cli") matches
        self.assertEqual(root, "/some/project/root")

    def test_resolve_example_retrieval_repo_cli_and_root_finds_py_arg(self):
        fake_config = {
            "mcpServers": {
                "example-retrieval-repo": {
                    "args": ["/opt/rag/server.py", "/some/project/root"]
                }
            }
        }
        m = mock.mock_open(read_data=json.dumps(fake_config))
        with mock.patch.object(Path, "open", m):
            cli, root = _cli._resolve_example_retrieval_repo_cli_and_root()
        self.assertEqual(cli, "/opt/rag/server.py")
        self.assertEqual(root, "/some/project/root")

    def test_resolve_example_retrieval_repo_cli_and_root_missing_file(self):
        with mock.patch.object(Path, "open", side_effect=OSError("no such file")):
            cli, root = _cli._resolve_example_retrieval_repo_cli_and_root()
        self.assertIsNone(cli)
        self.assertIsNone(root)


class AutonomousSentinelTests(unittest.TestCase):
    def test_enable_missing_mode_flag_errors(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: "sid123"):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = _cli._cmd_autonomous_sentinel(["enable"])
        self.assertEqual(rc, 2)
        self.assertIn("--mode", stderr.getvalue())

    def test_enable_unresolvable_session_id_fails_loud(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: ""):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = _cli._cmd_autonomous_sentinel(["enable", "--mode", "autonomous"])
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve current session id", stderr.getvalue())

    def test_enable_writes_sentinel_file(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: "sid-abc"), \
             mock.patch.object(Path, "write_text") as write_mock:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = _cli._cmd_autonomous_sentinel(["enable", "--mode", "mise-en-place"])
        self.assertEqual(rc, 0)
        write_mock.assert_called_once_with("mise-en-place\n", encoding="utf-8")
        self.assertIn("autonomous-run-sid-abc", stdout.getvalue())

    def test_disable_unresolvable_session_id_is_noop_success(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: ""):
            rc = _cli._cmd_autonomous_sentinel(["disable"])
        self.assertEqual(rc, 0)

    def test_disable_removes_sentinel_missing_ok(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: "sid-abc"), \
             mock.patch.object(Path, "unlink") as unlink_mock:
            rc = _cli._cmd_autonomous_sentinel(["disable"])
        self.assertEqual(rc, 0)
        unlink_mock.assert_called_once_with(missing_ok=True)

    def test_unknown_action_errors(self):
        with mock.patch.object(_cli, "_import_resolve_session_id", return_value=lambda: "sid-abc"):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = _cli._cmd_autonomous_sentinel(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown action", stderr.getvalue())

    def test_import_failure_exits_transport_fail(self):
        with mock.patch.object(
            _cli, "_import_resolve_session_id", side_effect=RuntimeError("no claude-klabauter root")
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = _cli._cmd_autonomous_sentinel(["enable", "--mode", "autonomous"])
        self.assertEqual(rc, _cli._TRANSPORT_FAIL)
        self.assertIn("CLAUDE_KLABAUTER_ROOT resolution failed", stderr.getvalue())


class MainDispatchTests(unittest.TestCase):
    def test_no_args_usage(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = _cli.main([])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", stderr.getvalue())

    def test_unknown_subcommand(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = _cli.main(["bogus-subcommand"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subcommand", stderr.getvalue())

    def test_help_flag(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = _cli.main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
