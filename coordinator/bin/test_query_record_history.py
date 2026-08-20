"""test_query_record_history.py — colocated coverage for
coordinator/bin/query-record-history.py.

Scope: CLI-layer behavior only — argument parsing, `--limit` client-side
slicing, `--format json`'s bare-array shape (AC7's tested contract), and the
`--type`-required/unknown-type fail-loud paths. Does NOT exercise the live
`records.history` engine op (that's `coordinator_core/ops/tests/
test_record_history.py`'s job) — `cc_invoke.route` and `_resolve_repo_root`
are monkeypatched on the already-imported subject module, mirroring
`test_query_records.py`'s own pattern, so these tests run with no
engine-root / repo dependency and never spawn `coordinator_core.invoke`.

Test coverage:
  T1  `--type` absent: exit 2, stderr names it's required
  T2  op success, default format (markdown-list): one heading line per
      record + one line per event; route() called with
      op="records.history", params={"record_type", "root"}
  T3  op success, `--format json`: bare JSON array on stdout, not the full
      `{"record_type", "root", "records"}` envelope
  T4  op failure (unknown type / any exception): exit 2, message on stderr
  T5  `--limit` slices the fetched records client-side
  T6  `--limit 0` returns an empty array (`is not None`, not truthiness —
      distinguishable from no `--limit` given, since there is no
      server-side default to silently restore)
  T7  `--root` overrides the resolved repo_root passed to route() and used
      as the `root` param
  T8  empty result set: exit 0, not a failure (empty-result exit-code
      contract, matching query-records.py / query-work-state.py)

Spec backlink: docs/plans/2026-08-20-a-time-axis-for-any-record-type.md § C3 (AC7)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("query-record-history.py")
_spec = importlib.util.spec_from_file_location("query_record_history_cli", _MODULE_PATH)
qrh = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["query_record_history_cli"] = qrh
_spec.loader.exec_module(qrh)

_SAMPLE_RECORDS = [
    {
        "path": "state/sizings/a.yaml",
        "created_at": "2026-08-01T00:00:00+01:00",
        "created_by": "alice",
        "events": [
            {
                "sha": "abc123",
                "author": "alice",
                "committed_at": "2026-08-02T00:00:00+01:00",
                "changes": {"status": {"from": "draft", "to": "routed"}},
            }
        ],
    },
    {
        "path": "state/sizings/b.yaml",
        "created_at": "2026-08-03T00:00:00+01:00",
        "created_by": "bob",
        "events": [],
    },
]


class QueryRecordHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._captured: dict = {}

        def _fake_route(op, params, repo_root, legacy_fn):
            self._captured["op"] = op
            self._captured["params"] = params
            self._captured["repo_root"] = repo_root
            return self._route_result

        self._route_result: dict = {"records": []}
        self._orig_route = qrh.cc_invoke.route
        self._orig_resolve_repo_root = qrh._resolve_repo_root
        self._orig_hint = qrh._supported_types_hint
        qrh.cc_invoke.route = _fake_route
        qrh._resolve_repo_root = lambda: "/fake/repo/root"
        qrh._supported_types_hint = lambda: "decision, sizing-object"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        qrh.cc_invoke.route = self._orig_route
        qrh._resolve_repo_root = self._orig_resolve_repo_root
        qrh._supported_types_hint = self._orig_hint

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = qrh.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_type_absent_exits_2_and_names_requirement(self) -> None:
        code, out, err = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("--type is required", err)
        self.assertIn("decision, sizing-object", err)
        self.assertEqual(out, "")

    def test_op_success_default_markdown_list(self) -> None:
        self._route_result = {"records": _SAMPLE_RECORDS}
        code, out, err = self._run(["--type", "sizing-object", "--root", "/repo"])
        self.assertEqual(code, 0)
        expected_root = os.path.abspath("/repo")
        self.assertEqual(self._captured["op"], "records.history")
        self.assertEqual(
            self._captured["params"], {"record_type": "sizing-object", "root": expected_root}
        )
        self.assertEqual(self._captured["repo_root"], expected_root)
        self.assertIn("## state/sizings/a.yaml", out)
        self.assertIn("abc123", out)
        self.assertIn("## state/sizings/b.yaml", out)
        self.assertIn("(no events)", out)

    def test_op_success_format_json_emits_bare_array(self) -> None:
        self._route_result = {"records": _SAMPLE_RECORDS}
        code, out, err = self._run(
            ["--type", "sizing-object", "--format", "json", "--root", "/repo"]
        )
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed, _SAMPLE_RECORDS)
        self.assertIsInstance(parsed, list)

    def test_op_failure_returns_exit_2_and_prints_stderr(self) -> None:
        def _raising_route(op, params, repo_root, legacy_fn):
            raise RuntimeError("unsupported record type 'bogus'; supported: decision, ...")

        qrh.cc_invoke.route = _raising_route
        code, out, err = self._run(["--type", "bogus", "--root", "/repo"])
        self.assertEqual(code, 2)
        self.assertIn("unsupported record type", err)
        self.assertEqual(out, "")

    def test_limit_slices_records_client_side(self) -> None:
        self._route_result = {"records": _SAMPLE_RECORDS}
        code, out, err = self._run(
            ["--type", "sizing-object", "--format", "json", "--limit", "1", "--root", "/repo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), _SAMPLE_RECORDS[:1])

    def test_limit_zero_returns_empty_array_not_default(self) -> None:
        self._route_result = {"records": _SAMPLE_RECORDS}
        code, out, err = self._run(
            ["--type", "sizing-object", "--format", "json", "--limit", "0", "--root", "/repo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_root_flag_overrides_resolved_repo_root(self) -> None:
        code, out, err = self._run(["--type", "decision", "--root", "/some/repo"])
        self.assertEqual(code, 0)
        expected = os.path.abspath("/some/repo")
        self.assertEqual(self._captured["repo_root"], expected)
        self.assertEqual(self._captured["params"]["root"], expected)

    def test_no_root_flag_falls_back_to_resolved_repo_root(self) -> None:
        code, out, err = self._run(["--type", "decision"])
        self.assertEqual(code, 0)
        self.assertEqual(self._captured["repo_root"], "/fake/repo/root")

    def test_empty_result_set_exits_0_not_failure(self) -> None:
        self._route_result = {"records": []}
        code, out, err = self._run(
            ["--type", "decision", "--format", "json", "--root", "/repo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "[]")


class SupportedTypesHintTests(unittest.TestCase):
    """T9/T10 — the supported-type hint must actually be reachable.

    `_supported_types_hint` catches bare `Exception` and returns "" on any
    failure, which is correct for a best-effort hint and also means an
    unbound name inside it degrades to a permanently empty hint that every
    message-text assertion still passes. T9 therefore checks name binding
    statically rather than asserting on stderr: it is the only leg that
    distinguishes "the hint could not resolve the engine on this box" from
    "the hint can never work anywhere".
    """

    def test_hint_references_no_unbound_global(self) -> None:
        import ast
        import builtins

        source = _MODULE_PATH.read_text(encoding="utf-8")
        fn = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.name == "_supported_types_hint"
        )
        bound = {
            alias.asname or alias.name.split(".")[0]
            for node in ast.walk(fn)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        bound |= {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        referenced = {
            node.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        unbound = {
            name
            for name in referenced - bound
            if not hasattr(builtins, name) and not hasattr(qrh, name)
        }
        self.assertEqual(unbound, set())

    def test_missing_type_message_carries_the_hint_when_available(self) -> None:
        original = qrh._supported_types_hint
        qrh._supported_types_hint = lambda: "decision, sizing-object"
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                code = qrh.main([])
        finally:
            qrh._supported_types_hint = original
        self.assertEqual(code, 2)
        self.assertIn("supported: decision, sizing-object", err.getvalue())


if __name__ == "__main__":
    unittest.main()
