"""test_query_records.py — colocated coverage for coordinator/bin/query-records.py.

Scope: CLI-layer behavior only — argument parsing, `--status` composition
into `--where`, `--where` operator-grammar pass-through parity, and the
fail-loud contract for unported `.js` flags. Does NOT exercise the live
`records.query` engine op (that's `coordinator_core/ops/tests/
test_records_query.py`'s job) — `route_mutation` is monkeypatched here so
these tests run with no CLAUDE_KLABAUTER_ROOT / repo dependency.

Spec backlink: pln-python-ize-claude-klabauter-bin-oracles--218413 § A2

Converted from a hand-rolled runner (`query-records.test.py`) to a pytest-collectable
module — the module was already unittest.TestCase-shaped, so this is a rename plus removal
of the `unittest.main()` harness (pytest collects the TestCase classes directly).
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("query-records.py")
_spec = importlib.util.spec_from_file_location("query_records_cli", _MODULE_PATH)
qr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["query_records_cli"] = qr
_spec.loader.exec_module(qr)


class WhereOperatorGrammarParityTests(unittest.TestCase):
    """`--where`'s operator characters must reach the op's params dict
    byte-verbatim — this trampoline does not parse or rewrite the grammar,
    the engine does (coordinator_core/ops/records_query.py._parse_where)."""

    def setUp(self) -> None:
        self._captured_params: dict | None = None
        self._captured_repo_root: str | None = None

        def _fake_route_mutation(op, params, repo_root, legacy_fn):
            self._captured_params = params
            self._captured_repo_root = repo_root
            return {"records": ""}

        self._orig_route_mutation = qr.route_mutation
        self._orig_resolve_repo_root = qr._resolve_repo_root
        qr.route_mutation = _fake_route_mutation
        qr._resolve_repo_root = lambda: "/fake/repo/root"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        qr.route_mutation = self._orig_route_mutation
        qr._resolve_repo_root = self._orig_resolve_repo_root

    def _run(self, argv: list[str]) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return qr.main(argv)

    def test_operator_grammar_pass_through_verbatim(self) -> None:
        for operator, clause in (
            ("=", "status=open"),
            ("!=", "status!=open"),
            ("<", "created<2026-01-01"),
            (">", "created>2026-01-01"),
            ("<=", "created<=2026-01-01"),
            (">=", "created>=2026-01-01"),
        ):
            with self.subTest(operator=operator):
                rc = self._run(["--type", "debt", "--where", clause])
                self.assertEqual(rc, 0)
                self.assertEqual(self._captured_params["where"], clause)

    def test_status_flag_composes_and_conjunct_onto_empty_where(self) -> None:
        rc = self._run(["--type", "debt", "--status", "open"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._captured_params["where"], "status=open")

    def test_status_flag_composes_and_conjunct_onto_existing_where(self) -> None:
        rc = self._run(["--type", "debt", "--where", "severity=P1", "--status", "open"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._captured_params["where"], "severity=P1 AND status=open")

    def test_since_and_older_than_reach_params_snake_case(self) -> None:
        rc = self._run(
            ["--type", "handoff", "--since", "14d", "--older-than", "6d"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self._captured_params["since"], "14d")
        self.assertEqual(self._captured_params["older_than"], "6d")

    def test_format_defaults_to_markdown_list(self) -> None:
        rc = self._run(["--type", "handoff"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._captured_params["format"], "markdown-list")

    def test_root_overrides_resolved_repo_root(self) -> None:
        # query-records.py's `--root` handling is `os.path.abspath(args.root)`
        # (coordinator/bin/query-records.py) -- repo_root is a filesystem path
        # fed straight into git-touching ops, legitimately OS-native, not a
        # POSIX-contract surface. Expect the native-normalized form.
        rc = self._run(["--type", "handoff", "--root", "/tmp/some-other-repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self._captured_repo_root, os.path.abspath("/tmp/some-other-repo")
        )

    def test_missing_type_without_list_schemas_fails_loud(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--where", "status=open"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_limit_reaches_params_including_the_zero_form(self) -> None:
        """`--limit` forwards, and `--limit 0` survives as 0, not as "omitted".

        Pinned because the module docstring named `--limit` in its UNSUPPORTED
        list from 2026-08-14 (when the parser gained it, 632ce6533) to
        2026-08-20, and a claude-klabauter-em session read the docstring instead of
        measuring — then told example-cockpit-repo-em the flag "is not exposed on
        our trampoline" and "caps at the op's default 50", filed it, and
        offered to build what already existed. Cockpit's own measurement
        through this CLI contradicted us and was right.

        The zero case is the one worth an assertion of its own: `--limit 0`
        means UNLIMITED here, so a `if args.limit:` truthiness guard would
        silently restore the op's default 50 — which is precisely the symptom
        we wrongly reported.
        """
        self._run(["--type", "lesson", "--limit", "0"])
        assert self._captured_params is not None
        self.assertEqual(self._captured_params["limit"], 0)

        self._run(["--type", "lesson", "--limit", "7"])
        assert self._captured_params is not None
        self.assertEqual(self._captured_params["limit"], 7)

    def test_limit_omitted_sends_no_limit_key(self) -> None:
        """Omitting the flag must not synthesize one — the op's own default
        (50) is what applies, and a trampoline-side default would shadow it."""
        self._run(["--type", "lesson"])
        assert self._captured_params is not None
        self.assertNotIn("limit", self._captured_params)

    def test_limit_is_not_in_the_unported_flag_list(self) -> None:
        """The docstring's two lists must not contradict the parser.

        `_UNPORTED_FLAGS` is the machine-readable half of the same claim the
        prose got wrong; asserting the flag is absent from it keeps a future
        edit from re-adding `--limit` to the rejected set while the parser
        still serves it.
        """
        self.assertNotIn("--limit", qr._UNPORTED_FLAGS)

    def test_status_value_containing_and_fails_loud(self) -> None:
        # Review: code-reviewer — Finding 1: a status value that itself
        # contains " AND " must fail loud, not silently compose into a
        # second where-clause conjunct.
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--type", "debt", "--status", "open AND urgent"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIsNone(self._captured_params)


class OutputSerializationTests(unittest.TestCase):
    """Review: code-reviewer — Finding 2: `--format json`/markdown-list
    stdout serialization had zero test coverage (every prior test's fake
    `route_mutation` returned `{"records": ""}` unconditionally)."""

    def setUp(self) -> None:
        self._orig_route_mutation = qr.route_mutation
        self._orig_resolve_repo_root = qr._resolve_repo_root
        qr._resolve_repo_root = lambda: "/fake/repo/root"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        qr.route_mutation = self._orig_route_mutation
        qr._resolve_repo_root = self._orig_resolve_repo_root

    def _install_fake(self, records) -> None:
        def _fake_route_mutation(op, params, repo_root, legacy_fn):
            return {"records": records}

        qr.route_mutation = _fake_route_mutation

    def _run(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            rc = qr.main(argv)
        return rc, stdout.getvalue()

    def test_format_json_writes_json_dumps_plus_trailing_newline(self) -> None:
        self._install_fake([{"path": "state/debt-backlog/x.yaml", "status": "open"}])
        rc, out = self._run(["--type", "debt", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            '[{"path": "state/debt-backlog/x.yaml", "status": "open"}]\n',
        )

    def test_format_markdown_list_writes_records_string_verbatim(self) -> None:
        self._install_fake("- state/debt-backlog/x.yaml\n- state/debt-backlog/y.yaml\n")
        rc, out = self._run(["--type", "debt"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "- state/debt-backlog/x.yaml\n- state/debt-backlog/y.yaml\n")

    def test_format_markdown_list_appends_missing_trailing_newline(self) -> None:
        self._install_fake("- state/debt-backlog/x.yaml")
        rc, out = self._run(["--type", "debt"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "- state/debt-backlog/x.yaml\n")


class ListSchemasTests(unittest.TestCase):
    """Review: code-reviewer — Finding 3: `--list-schemas` had zero test
    coverage despite being one of two novel capabilities the module
    docstring calls out."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = qr.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_list_schemas_success_prints_sorted_type_names(self) -> None:
        import types

        fake_cc_invoke = types.ModuleType("cc_invoke")
        fake_cc_invoke._resolve_claude_klabauter_root = lambda: "/fake/claude-klabauter/root"

        fake_records_query_ops = types.ModuleType(
            "coordinator_core.ops.records_query"
        )
        fake_records_query_ops._TYPE_TO_GLOB = {"debt": "*", "handoff": "*", "audit": "*"}

        fake_coordinator_core_ops = types.ModuleType("coordinator_core.ops")
        fake_coordinator_core_ops.records_query = fake_records_query_ops
        fake_coordinator_core = types.ModuleType("coordinator_core")
        fake_coordinator_core.ops = fake_coordinator_core_ops

        orig_modules = {
            k: sys.modules.get(k)
            for k in (
                "cc_invoke",
                "coordinator_core",
                "coordinator_core.ops",
                "coordinator_core.ops.records_query",
            )
        }
        sys.modules["cc_invoke"] = fake_cc_invoke
        sys.modules["coordinator_core"] = fake_coordinator_core
        sys.modules["coordinator_core.ops"] = fake_coordinator_core_ops
        sys.modules["coordinator_core.ops.records_query"] = fake_records_query_ops
        try:
            rc, out, _ = self._run(["--list-schemas"])
        finally:
            for k, v in orig_modules.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertEqual(rc, 0)
        self.assertEqual(out, "audit\ndebt\nhandoff\n")

    def test_list_schemas_claude_klabauter_root_resolution_failure_exits_3(self) -> None:
        import types

        fake_cc_invoke = types.ModuleType("cc_invoke")

        def _raise():
            raise RuntimeError("CLAUDE_KLABAUTER_ROOT not set")

        fake_cc_invoke._resolve_claude_klabauter_root = _raise

        orig = sys.modules.get("cc_invoke")
        sys.modules["cc_invoke"] = fake_cc_invoke
        try:
            rc, out, err = self._run(["--list-schemas"])
        finally:
            if orig is None:
                sys.modules.pop("cc_invoke", None)
            else:
                sys.modules["cc_invoke"] = orig
        self.assertEqual(rc, 3)
        self.assertIn("CLAUDE_KLABAUTER_ROOT resolution failed", err)

    def test_list_schemas_engine_import_failure_exits_3(self) -> None:
        import types

        fake_cc_invoke = types.ModuleType("cc_invoke")
        fake_cc_invoke._resolve_claude_klabauter_root = lambda: "/fake/claude-klabauter/root"

        orig_cc_invoke = sys.modules.get("cc_invoke")
        orig_ops_records_query = sys.modules.get("coordinator_core.ops.records_query")
        sys.modules["cc_invoke"] = fake_cc_invoke
        # Force the in-function import to fail regardless of whether the real
        # coordinator_core package is importable from this process's sys.path.
        sys.modules["coordinator_core.ops.records_query"] = None
        try:
            rc, out, err = self._run(["--list-schemas"])
        finally:
            if orig_cc_invoke is None:
                sys.modules.pop("cc_invoke", None)
            else:
                sys.modules["cc_invoke"] = orig_cc_invoke
            if orig_ops_records_query is None:
                sys.modules.pop("coordinator_core.ops.records_query", None)
            else:
                sys.modules["coordinator_core.ops.records_query"] = orig_ops_records_query
        self.assertEqual(rc, 3)
        self.assertIn("not importable", err)


class UnportedFlagFailLoudTests(unittest.TestCase):
    def _run_capture_stderr(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                qr.main(argv)
        return ctx.exception.code, stderr.getvalue()

    def test_validate_all_fails_loud_not_silent(self) -> None:
        code, stderr = self._run_capture_stderr(["--type", "debt", "--validate-all"])
        self.assertNotEqual(code, 0)
        self.assertIn("--validate-all: not ported — claude-klabauter BIG_PORT", stderr)

    def test_fleet_fails_loud(self) -> None:
        code, stderr = self._run_capture_stderr(["--type", "debt", "--fleet"])
        self.assertNotEqual(code, 0)
        self.assertIn("--fleet: not ported — claude-klabauter BIG_PORT", stderr)

    def test_key_fails_loud(self) -> None:
        code, stderr = self._run_capture_stderr(["--type", "debt", "--key", "x"])
        self.assertNotEqual(code, 0)
        self.assertIn("--key: not ported — claude-klabauter BIG_PORT", stderr)

    def test_include_unparseable_fails_loud(self) -> None:
        code, stderr = self._run_capture_stderr(["--type", "debt", "--include-unparseable"])
        self.assertNotEqual(code, 0)
        self.assertIn("--include-unparseable: not ported — claude-klabauter BIG_PORT", stderr)

    def test_show_toplevel_fails_loud(self) -> None:
        code, stderr = self._run_capture_stderr(["--type", "debt", "--show-toplevel"])
        self.assertNotEqual(code, 0)
        self.assertIn("--show-toplevel: not ported — claude-klabauter BIG_PORT", stderr)

    def test_fleet_equals_form_fails_loud(self) -> None:
        # Review: code-reviewer — Finding 4: the docstring claims a bare
        # `--fleet=1` form (split on first `=`) is caught too, but no test
        # exercised the `--flag=value` shape.
        code, stderr = self._run_capture_stderr(["--type", "debt", "--fleet=1"])
        self.assertNotEqual(code, 0)
        self.assertIn("--fleet: not ported — claude-klabauter BIG_PORT", stderr)

    def test_unported_flag_wins_even_before_missing_type_error(self) -> None:
        # Ordering: the unported-flag scan runs BEFORE argparse, so it fires
        # even on an otherwise-invalid invocation (no --type given) — the
        # unported-flag message must never be masked by a different error.
        code, stderr = self._run_capture_stderr(["--validate-all"])
        self.assertNotEqual(code, 0)
        self.assertIn("--validate-all: not ported — claude-klabauter BIG_PORT", stderr)
