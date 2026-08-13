"""test_handoff_discharge_criteria.py — unit test for
`coordinator/bin/handoff-discharge-criteria.py`.

Same idiom as test_handoff_reconcile_close_terminal.py: monkeypatches the
module's own seams (`_resolve_repo_root`, `cc_invoke.route_mutation`) so this
suite asserts ONLY the CLI's own argv handling and dispatch logic — both
target forms (--criterion-id / --position), the mutual-exclusion usage
error, the missing-target usage error, and the refusal/transport exit-code
split — not the engine behind `handoff.discharge_criteria` (that op has its
own test surface under coordinator_core/).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since the CLI
module has a `.py` extension but is not on `sys.path` as an importable
package member — same load idiom used across coordinator/bin/tests/.

Spec backlink: coordinator_core/ops/handoff_discharge_criteria.py — the
pickup/workstream-complete gate interaction the CLI closes the missing-
forwarder gap for.

Run:
    pytest coordinator/bin/tests/test_handoff_discharge_criteria.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "handoff_discharge_criteria_test",
        str(_BIN_DIR / "handoff-discharge-criteria.py"),
    )
    spec = importlib.util.spec_from_loader("handoff_discharge_criteria_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingRouteMutation:
    """Stand-in for cc_invoke.route_mutation — records params, returns a
    canned result or raises a canned exception."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result: dict = {}
        self.exc: Exception | None = None

    def __call__(self, op, params, repo_root, legacy_fn):
        self.calls.append({"op": op, "params": params, "repo_root": repo_root})
        if self.exc is not None:
            raise self.exc
        return self.result


class _StubHarness(unittest.TestCase):
    def setUp(self):
        self._orig_route_mutation = _cli.cc_invoke.route_mutation
        self._orig_repo_root = _cli._resolve_repo_root
        self.addCleanup(self._restore)

        self.route_mutation = _RecordingRouteMutation()
        _cli.cc_invoke.route_mutation = self.route_mutation
        _cli._resolve_repo_root = lambda handoff_path: "/fake/repo/root"

        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        self._tmp.close()
        self.handoff_path = self._tmp.name
        self._seed_fixture_handoff()

    def _restore(self):
        _cli.cc_invoke.route_mutation = self._orig_route_mutation
        _cli._resolve_repo_root = self._orig_repo_root
        try:
            os.unlink(self.handoff_path)
        except OSError:
            pass

    def _seed_fixture_handoff(self):
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "status: open\n"
                "---\n"
                "## Acceptance criteria\n"
                "\n"
                "- AC-1 first criterion\n"
                "- [ ] AC-1 first criterion checkbox\n"
                "- AC-2 second criterion\n"
                "- [ ] AC-2 second criterion checkbox\n"
            )


class DispatchTest(_StubHarness):
    def test_success_by_criterion_id(self):
        self.route_mutation.result = {
            "exit_code": 0,
            "applied": True,
            "resolved_position": 1,
            "resolved_criterion_id": "AC-1",
            "discharge_op": "tick",
        }
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "AC-1", "", "", "", ""
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.route_mutation.calls), 1)
        call = self.route_mutation.calls[0]
        self.assertEqual(call["op"], "handoff.discharge_criteria")
        self.assertEqual(call["params"]["handoff_path"], self.handoff_path)
        self.assertEqual(call["params"]["criterion_id"], "AC-1")
        self.assertNotIn("position", call["params"])
        self.assertNotIn("met_text", call["params"])
        self.assertNotIn("unmet_text", call["params"])
        self.assertNotIn("override_reason", call["params"])

    def test_success_by_position(self):
        self.route_mutation.result = {
            "exit_code": 0,
            "applied": True,
            "resolved_position": 2,
            "resolved_criterion_id": "AC-2",
            "discharge_op": "tick",
        }
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "", "2", "", "", ""
        )
        self.assertEqual(rc, 0)
        call = self.route_mutation.calls[0]
        self.assertEqual(call["params"]["position"], 2)
        self.assertNotIn("criterion_id", call["params"])

    def test_split_forwards_met_and_unmet_text(self):
        self.route_mutation.result = {"exit_code": 0, "applied": True}
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path,
            "AC-1",
            "",
            "AC-1 met part",
            "AC-1 unmet part",
            "",
        )
        self.assertEqual(rc, 0)
        call = self.route_mutation.calls[0]
        self.assertEqual(call["params"]["met_text"], "AC-1 met part")
        self.assertEqual(call["params"]["unmet_text"], "AC-1 unmet part")

    def test_override_reason_forwarded_only_when_supplied(self):
        self.route_mutation.result = {"exit_code": 0, "applied": True}
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "AC-1", "", "", "", "not claim holder"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["override_reason"],
            "not claim holder",
        )

    def test_both_criterion_id_and_position_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "AC-1", "1", "", "", ""
        )
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_neither_criterion_id_nor_position_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "", "", "", "", ""
        )
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_missing_handoff_path_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria("   ", "AC-1", "", "", "", "")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_only_met_text_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "AC-1", "", "met only", "", ""
        )
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_only_unmet_text_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "AC-1", "", "", "unmet only", ""
        )
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_non_integer_position_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(
            self.handoff_path, "", "not-a-number", "", "", ""
        )
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_zero_position_is_usage_error(self):
        rc = _cli.cmd_discharge_criteria(self.handoff_path, "", "0", "", "", "")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.route_mutation.calls), 0)

    def test_op_refusal_returns_1(self):
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError(
            "already ticked",
            {"exit_code": 1, "applied": False, "error": "already ticked"},
        )
        rc = _cli.cmd_discharge_criteria(self.handoff_path, "AC-1", "", "", "", "")
        self.assertEqual(rc, 1)

    def test_op_usage_refusal_returns_2(self):
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError(
            "bad usage",
            {"exit_code": 2, "applied": False, "error": "bad usage"},
        )
        rc = _cli.cmd_discharge_criteria(self.handoff_path, "AC-1", "", "", "", "")
        self.assertEqual(rc, 2)

    def test_transport_failure_returns_1(self):
        self.route_mutation.exc = RuntimeError("transport down")
        rc = _cli.cmd_discharge_criteria(self.handoff_path, "AC-1", "", "", "", "")
        self.assertEqual(rc, 1)

    def test_unresolvable_repo_root_returns_1(self):
        _cli._resolve_repo_root = lambda handoff_path: None
        rc = _cli.cmd_discharge_criteria(self.handoff_path, "AC-1", "", "", "", "")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.route_mutation.calls), 0)


class ArgvParsingTest(unittest.TestCase):
    def test_parser_accepts_criterion_id_form(self):
        args = _cli._build_parser().parse_args(
            ["/x/handoff.md", "--criterion-id", "AC-3"]
        )
        self.assertEqual(args.handoff_path, "/x/handoff.md")
        self.assertEqual(args.criterion_id, "AC-3")
        self.assertEqual(args.position, "")

    def test_parser_accepts_position_form(self):
        args = _cli._build_parser().parse_args(["/x/handoff.md", "--position", "3"])
        self.assertEqual(args.position, "3")
        self.assertEqual(args.criterion_id, "")

    def test_parser_rejects_missing_handoff_path(self):
        with self.assertRaises(SystemExit):
            _cli._build_parser().parse_args(["--criterion-id", "AC-1"])


if __name__ == "__main__":
    unittest.main()
