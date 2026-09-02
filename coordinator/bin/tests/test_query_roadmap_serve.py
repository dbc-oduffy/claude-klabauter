"""test_query_roadmap_serve.py -- the test surface for `coordinator/bin/
query-roadmap-serve.py`.

Pins the wire contract example-cockpit-repo's `src/lib/store/roadmaps-acquisition.ts
:: fetchRoadmapServe` parses (read at their `origin/main` @ `abcc06ad9`), not
just "it runs": ONE JSON object on stdout with no `{"records": [...]}`
envelope, carrying `roadmap_id`, `nodes[]`, `edges[]`, `roll_up`,
`critical_path[]`, `scan_incomplete` and `scan_errors[]`.

Two pins carry more weight than the rest and are called out so a later editor
does not "simplify" them away:

`critical_path` SURVIVES TO STDOUT. The spike verdict recommended dropping it
and their storage genuinely has no column for it, so the tempting edit is to
stop serving it. Their wire type declares it and their own suite pins it
round-tripping (`roadmaps-acquisition.test.ts:91/98/142`); dropping it turns
their tests red for no gain here. This suite fails loudly if it disappears.

THE NULL-ROLL_UP RULE HAS A DISCRIMINATOR, and testing only the null arm would
pass while missing the point. A roadmap we could not scan reports `roll_up:
null`; a roadmap that genuinely has no stubs keeps its real `{"total": 0}`.
Both arms are asserted, because collapsing the two is exactly the wrong-answer
class the rule exists to prevent.

`assemble_roadmap_dag` is stubbed throughout -- this suite never scans a real
stub corpus. Stubbing/`SystemExit` conventions mirror
`test_query_commit_closures.py` and `test_query_goals.py` rather than inventing
a third shape.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import importlib

query_roadmap_serve = importlib.import_module("query-roadmap-serve")


def _dag(**overrides):
    """`assemble_roadmap_dag`'s return shape -- the six keys it documents."""
    payload = {
        "nodes": [
            {
                "stub_id": "sedge-01",
                "status": "shipped",
                "sprint": 1,
                "wave": 1,
                "shipped_sha": "d" * 40,
                "roadmap_id": "sedge-2026-08-06",
            }
        ],
        "edges": [
            {"from": "sedge-01", "to": "sedge-02", "type": "blocks", "roadmap_id": "sedge-2026-08-06"}
        ],
        "roll_up": {"total": 1, "by_status": {"shipped": 1}, "pct_shipped": 100.0},
        "critical_path": ["sedge-01"],
        "scan_incomplete": False,
        "scan_errors": [],
    }
    payload.update(overrides)
    return payload


def _run(dag, roadmap_id="sedge-2026-08-06"):
    """Drive `main` with every resolver and the producer stubbed."""
    with mock.patch.object(
        query_roadmap_serve, "resolve_repo_root_or_exit", return_value="/repo/match"
    ), mock.patch.object(
        query_roadmap_serve, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
    ), mock.patch(
        "coordinator_core.ops.emit.resolvers.resolve_context"
    ), mock.patch(
        "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag", return_value=dag
    ):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = query_roadmap_serve.main(["--roadmap-id", roadmap_id])
    return exit_code, stdout.getvalue()


class TestArgparse(unittest.TestCase):
    def test_help_states_honesty_disclosures(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("scan_incomplete", text)
        self.assertIn("deployment_state", text)

    def test_missing_roadmap_id_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.main([])

        self.assertEqual(ctx.exception.code, 2)

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.main(["--roadmap-id", "x", "--bogus"])

        self.assertEqual(ctx.exception.code, 2)

    def test_no_list_mode_is_offered(self):
        """Negative-spec: no `--all`/`--list` batch mode (overengineering
        finding 3 -- ship the contract the committed caller uses)."""
        for flag in ("--all", "--list", "--roadmap-ids"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    query_roadmap_serve.main([flag])
                self.assertEqual(ctx.exception.code, 2)


class TestWireShape(unittest.TestCase):
    def test_stdout_is_one_object_with_the_contract_keys(self):
        exit_code, out = _run(_dag())

        self.assertEqual(exit_code, 0)
        printed = json.loads(out)
        self.assertIsInstance(printed, dict, "stdout must be ONE object, not a list or envelope")
        self.assertNotIn("records", printed, "no `records` envelope -- stdout IS the record")
        self.assertEqual(
            sorted(printed),
            [
                "critical_path",
                "edges",
                "nodes",
                "roadmap_id",
                "roll_up",
                "scan_errors",
                "scan_incomplete",
            ],
        )

    def test_roadmap_id_is_echoed(self):
        _, out = _run(_dag(), roadmap_id="qsub-2026-07-10")
        self.assertEqual(json.loads(out)["roadmap_id"], "qsub-2026-07-10")

    def test_critical_path_survives_to_stdout(self):
        """Deliberate deviation from the spike verdict's "drop it" -- their
        wire type declares it and their suite pins it. See module docstring."""
        _, out = _run(_dag(critical_path=["sedge-01", "sedge-06"]))
        self.assertEqual(json.loads(out)["critical_path"], ["sedge-01", "sedge-06"])

    def test_nodes_and_edges_are_passed_through_verbatim(self):
        dag = _dag()
        _, out = _run(dag)
        printed = json.loads(out)
        self.assertEqual(printed["nodes"], dag["nodes"])
        self.assertEqual(printed["edges"], dag["edges"])


class TestUnsubstantiatedZeroRule(unittest.TestCase):
    def test_unscannable_and_empty_serves_null_roll_up(self):
        _, out = _run(
            _dag(
                nodes=[],
                edges=[],
                roll_up={"total": 0, "by_status": {}, "pct_shipped": None},
                critical_path=[],
                scan_incomplete=True,
                scan_errors=["state/handoffs unreadable"],
            )
        )
        printed = json.loads(out)
        self.assertIsNone(printed["roll_up"], "a zero we cannot substantiate must not be expressible")
        self.assertTrue(printed["scan_incomplete"])

    def test_genuinely_empty_roadmap_keeps_its_real_zero(self):
        """The discriminator. Without this, nulling every zero would pass the
        test above while destroying the distinction the rule exists to draw."""
        _, out = _run(
            _dag(
                nodes=[],
                edges=[],
                roll_up={"total": 0, "by_status": {}, "pct_shipped": None},
                critical_path=[],
                scan_incomplete=False,
                scan_errors=[],
            )
        )
        printed = json.loads(out)
        self.assertEqual(printed["roll_up"], {"total": 0, "by_status": {}, "pct_shipped": None})

    def test_partial_scan_that_found_nodes_keeps_its_roll_up(self):
        """A short count is still a real count; `scan_incomplete` is what
        flags it. Nulling here would discard data we actually have."""
        _, out = _run(
            _dag(
                roll_up={"total": 1, "by_status": {"shipped": 1}, "pct_shipped": 100.0},
                scan_incomplete=True,
                scan_errors=["archive/handoffs/2026-05 unreadable"],
            )
        )
        printed = json.loads(out)
        self.assertEqual(printed["roll_up"]["total"], 1)
        self.assertTrue(printed["scan_incomplete"])


class TestFailurePaths(unittest.TestCase):
    def test_producer_exception_exits_1_and_prints_nothing_to_stdout(self):
        """Never a silent empty-but-well-formed exit 0 -- cockpit's caller
        cannot tell that from a genuinely empty roadmap."""
        with mock.patch.object(
            query_roadmap_serve, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_roadmap_serve, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context"
        ), mock.patch(
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            side_effect=RuntimeError("corpus unreadable"),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = query_roadmap_serve.main(["--roadmap-id", "x"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("query-roadmap-serve: ", stderr.getvalue())
        self.assertIn("corpus unreadable", stderr.getvalue())

    def test_repo_root_resolution_failure_propagates_its_exit_code(self):
        with mock.patch.object(
            query_roadmap_serve, "resolve_repo_root_or_exit", return_value=1
        ):
            exit_code = query_roadmap_serve.main(["--roadmap-id", "x"])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
