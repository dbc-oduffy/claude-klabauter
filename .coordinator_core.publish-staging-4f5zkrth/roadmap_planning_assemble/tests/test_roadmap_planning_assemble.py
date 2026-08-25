"""Tests for coordinator_core.roadmap_planning_assemble.

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md,
chunk C10.

Covers: brief() shape conformance to the DR-047 decision-object schema,
entry-point resolution (A/B/C/D + ambiguity), Class A's eight expressed as
glue (never a directives[].cli binding), the recommendation-forbidden PM
gates, main() CLI parsing, and the HARD structural/perf constraints (AC2/
AC3/AC4) via the real CLI subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import pytest

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

import coordinator_core.roadmap_planning_assemble as rpa  # noqa: E402

_CLI_PATH = os.path.join(_ENGINE_ROOT, "coordinator", "bin", "roadmap-planning-assemble.py")

_SCHEMA_TOP_LEVEL_KEYS = {
    "artifact",
    "preflight",
    "gates",
    "directives",
    "judgment_points",
    "decisions",
    "narration",
    "next_move",
}


class TestBriefShape(unittest.TestCase):
    def test_top_level_schema_keys(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        self.assertEqual(set(result.keys()), _SCHEMA_TOP_LEVEL_KEYS)

    def test_narration_nonempty(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        self.assertIsInstance(result["narration"], str)
        self.assertTrue(result["narration"])

    def test_decisions_echoed_verbatim(self):
        payload = {"j-p1.3-verdict": {"disposition": "keep", "resolved_at": "2026-08-21T00:00:00Z"}}
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus", decisions=payload)
        self.assertEqual(result["decisions"], payload)

    def test_decisions_default_empty_object(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        self.assertEqual(result["decisions"], {})


class TestEntryPointResolution(unittest.TestCase):
    def test_entry_point_a(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        self.assertEqual(result["artifact"]["entry_point"], "A")

    def test_entry_point_b_derives_run_id_from_stub_id(self):
        result = rpa.brief(stub_id="stub-2026-08-21-example")
        self.assertEqual(result["artifact"]["entry_point"], "B")
        self.assertEqual(result["artifact"]["run_id"], "stub-2026-08-21-example")

    def test_entry_point_c(self):
        result = rpa.brief(run_id="r1", problem_set_path="docs/problems/x.md")
        self.assertEqual(result["artifact"]["entry_point"], "C")

    def test_entry_point_d(self):
        result = rpa.brief(run_id="r1", sizing_object_path="state/sizings/x.yaml")
        self.assertEqual(result["artifact"]["entry_point"], "D")

    def test_no_entry_point_supplied_is_unresolved_with_next_move(self):
        result = rpa.brief(run_id="r1")
        self.assertIsNone(result["artifact"]["entry_point"])
        self.assertIsNotNone(result["next_move"])
        ids = [jp["id"] for jp in result["judgment_points"]]
        self.assertIn("j-entry-point-unresolved", ids)

    def test_multiple_entry_points_supplied_surfaces_judgment_point(self):
        result = rpa.brief(
            run_id="r1", input_corpus_path="docs/corpus", stub_id="stub-x"
        )
        self.assertIsNone(result["artifact"]["entry_point"])
        ids = [jp["id"] for jp in result["judgment_points"]]
        self.assertIn("j-entryD-4-resolve-competing-entries", ids)
        jp = next(j for j in result["judgment_points"] if j["id"] == "j-entryD-4-resolve-competing-entries")
        self.assertEqual({d["value"] for d in jp["dispositions"]}, {"A", "B"})

    def test_missing_run_id_without_derivable_entry_point_raises(self):
        with self.assertRaises(rpa.RoadmapPlanningAssembleError):
            rpa.brief(input_corpus_path="docs/corpus")


class TestClassAGlueNeverBoundAsCli(unittest.TestCase):
    def test_class_a_glue_directives_carry_no_cli(self):
        result = rpa.brief(stub_id="stub-x")
        glue_directives = [d for d in result["directives"] if d["id"].startswith("g-")]
        self.assertTrue(glue_directives)
        for directive in glue_directives:
            self.assertIsNone(directive["cli"])

    def test_class_a_names_never_appear_as_a_directive_cli_value(self):
        result = rpa.brief(stub_id="stub-x")
        cli_values = {d["cli"] for d in result["directives"] if d["cli"] is not None}
        self.assertTrue(cli_values.isdisjoint(set(rpa.CLASS_A_GLUE)))

    def test_derive_run_id_from_stub_id_glue_present_on_entry_b(self):
        result = rpa.brief(stub_id="stub-x")
        ids = [d["id"] for d in result["directives"]]
        self.assertIn("g-derive-run-id-from-stub-id", ids)

    def test_mechanical_directives_bind_only_the_13_op_manifest(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        cli_values = {d["cli"] for d in result["directives"] if d["cli"] is not None}
        allowed = set(rpa._SPINE_CANDIDATE_OPS.values())
        self.assertTrue(cli_values.issubset(allowed))


class TestPmGatesRecommendationForbidden(unittest.TestCase):
    def test_every_pm_gate_carries_no_recommendation_and_forbidden_reason(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        pm_gate_ids = {
            "j-p1.5.0-pm-authorize-research-depth",
            "j-p1.5.4-pm-round1",
            "j-p1.5.6-pm-round2",
        }
        found = {jp["id"] for jp in result["judgment_points"]} & pm_gate_ids
        self.assertEqual(found, pm_gate_ids)
        for jp in result["judgment_points"]:
            if jp["id"] in pm_gate_ids:
                self.assertIsNone(jp["recommendation"])
                self.assertEqual(jp["reason"], "recommendation-forbidden")

    def test_entry_c_precondition_confirm_is_recommendation_forbidden(self):
        result = rpa.brief(run_id="r1", problem_set_path="docs/problems/x.md")
        jp = next(j for j in result["judgment_points"] if j["id"] == "j-entryC-5-precondition-confirm")
        self.assertIsNone(jp["recommendation"])
        self.assertEqual(jp["reason"], "recommendation-forbidden")

    def test_advisory_judgment_points_never_manufacture_a_recommendation(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        for jp in result["judgment_points"]:
            if jp["id"] in (
                "j-p1.5.0-pm-authorize-research-depth",
                "j-p1.5.4-pm-round1",
                "j-p1.5.6-pm-round2",
            ):
                continue
            self.assertIsNone(jp["recommendation"])
            self.assertEqual(jp["reason"], "insufficient-evidence")


class TestSprintOnlyRowsExcluded(unittest.TestCase):
    def test_no_sprint_seam_only_directive_or_judgment_point(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        forbidden_ops = {"detect-pm-gate-signal", "coordinator-doc-new"}
        cli_values = {d["cli"] for d in result["directives"] if d["cli"] is not None}
        self.assertTrue(cli_values.isdisjoint(forbidden_ops))


class TestMainCli(unittest.TestCase):
    def test_help_returns_ok(self):
        self.assertEqual(rpa.main(["--help"]), rpa.EXIT_OK)

    def test_usage_error_on_unrecognized_argument(self):
        self.assertEqual(rpa.main(["--bogus"]), rpa.EXIT_USAGE)

    def test_malformed_decisions_json_is_usage_error(self):
        self.assertEqual(
            rpa.main(["--run-id", "r1", "--input-corpus", "docs/corpus", "--decisions", "{not-json"]),
            rpa.EXIT_USAGE,
        )

    def test_prints_json_decision_object_on_success(self, capsys=None):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rpa.main(["--run-id", "r1", "--input-corpus", "docs/corpus"])
        self.assertEqual(code, rpa.EXIT_OK)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(set(parsed.keys()), _SCHEMA_TOP_LEVEL_KEYS)


@unittest.skipUnless(os.path.exists(_CLI_PATH), "roadmap-planning-assemble.py CLI not found")
@pytest.mark.spawns_process
@pytest.mark.cadence
class TestRealCliStructuralAndPerf(unittest.TestCase):
    """AC1-AC4 (this chunk's own HARD non-negotiable): no module-scope
    `coordinator_core.ops` import, ≤200ms end-to-end, ≤2.0 procs/call —
    asserted structurally against the real CLI, never a stopwatch alone."""

    @staticmethod
    def _child_env():
        # `COORDINATOR_ENGINE_ROOT` is rung 1 of cc_invoke._resolve_claude_klabauter_root()'s
        # ladder (its predecessor `CLAUDE_KLABAUTER_ROOT` is retired — the dual-read
        # window closed) — pinning it to THIS tree makes the real-CLI
        # subprocess tests exercise the module under test rather than
        # whatever engine root is published to the machine-local registry
        # (this chunk's delivery is deliberately inert/unpublished on
        # landing — see the module docstring and C12's own publish-
        # allowlist job).
        env = dict(os.environ)
        env["COORDINATOR_ENGINE_ROOT"] = _ENGINE_ROOT
        return env

    def test_no_module_scope_coordinator_core_ops_import(self):
        probe = (
            "import sys, os; "
            f"sys.path.insert(0, {os.path.join(_ENGINE_ROOT, 'coordinator', 'bin', 'lib')!r}); "
            f"sys.argv = [{_CLI_PATH!r}, '--run-id', 'r1', '--input-corpus', 'docs/corpus']; "
            f"exec(open({_CLI_PATH!r}).read(), {{'__name__': '__main__', '__file__': {_CLI_PATH!r}}}); "
            "assert 'coordinator_core.ops' not in sys.modules, "
            "'coordinator_core.ops must never be imported by this assembler'"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_ENGINE_ROOT,
            env=self._child_env(),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_CONSOLE,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def _batched_stats(self):
        sys.path.insert(0, os.path.join(_ENGINE_ROOT, "coordinator_core", "benchmarks"))
        from process_time import IS_DARWIN, IS_WINDOWS, batched_process_time_ms  # noqa: PLC0415

        if not (IS_WINDOWS or IS_DARWIN):
            self.skipTest(
                "batched_process_time_ms has no spawn-count primitive on this "
                "platform (Windows job-object / Darwin kqueue only)"
            )
        cmd = [sys.executable, _CLI_PATH, "--run-id", "r1", "--input-corpus", "docs/corpus"]
        return batched_process_time_ms(cmd, k=5, env=self._child_env(), cwd=_ENGINE_ROOT)

    def test_process_time_under_budget(self):
        stats = self._batched_stats()
        # `rc` is only the LAST invocation's exit code (the primitive's own
        # documented limitation) — every earlier sample is unverified by
        # this call alone, so this assertion is necessary but not
        # sufficient; a direct `subprocess.run` smoke assertion covers the
        # single-call exit-code contract elsewhere in this module.
        self.assertEqual(stats["rc"], 0)
        self.assertLessEqual(stats["process_time_ms"], 200.0)

    def test_spawn_count_under_budget(self):
        stats = self._batched_stats()
        self.assertEqual(stats["rc"], 0)
        self.assertLessEqual(stats["procs_per_call"], 2.0)


if __name__ == "__main__":
    unittest.main()
