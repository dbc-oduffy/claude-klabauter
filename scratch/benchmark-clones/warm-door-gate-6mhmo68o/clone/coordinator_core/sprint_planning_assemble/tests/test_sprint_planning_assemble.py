"""Tests for coordinator_core.sprint_planning_assemble.

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md,
chunk C11.

Covers: brief() shape conformance to the DR-047 decision-object schema,
the run_id+sprint_id entry contract (no A/B/C/D resolution — that shape is
spine-only), the recommendation-forbidden PM gates, no spine-seam-only
directive/judgment point leaking in, main() CLI parsing, and the HARD
structural/perf constraints (AC2/AC3/AC4) via the real CLI subprocess.
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

import coordinator_core.sprint_planning_assemble as spa  # noqa: E402

_CLI_PATH = os.path.join(_ENGINE_ROOT, "coordinator", "bin", "sprint-planning-assemble.py")

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
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        self.assertEqual(set(result.keys()), _SCHEMA_TOP_LEVEL_KEYS)

    def test_narration_nonempty(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        self.assertIsInstance(result["narration"], str)
        self.assertTrue(result["narration"])

    def test_decisions_echoed_verbatim(self):
        payload = {"j-p1.3-verdict": {"disposition": "keep", "resolved_at": "2026-08-21T00:00:00Z"}}
        result = spa.brief(run_id="r1", sprint_id="sprint-1", decisions=payload)
        self.assertEqual(result["decisions"], payload)

    def test_decisions_default_empty_object(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        self.assertEqual(result["decisions"], {})

    def test_artifact_carries_run_id_and_sprint_id(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        self.assertEqual(result["artifact"]["run_id"], "r1")
        self.assertEqual(result["artifact"]["sprint_id"], "sprint-1")

    def test_preflight_seam_is_sprint(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        self.assertEqual(result["preflight"]["seam"], "sprint")


class TestEntryContract(unittest.TestCase):
    """sprint-planning is invoked once per sprint, run_id + sprint_id both
    required — no A/B/C/D entry-point resolution (that shape is
    roadmap_planning_assemble's alone; every entry-point census row is
    seam: spine)."""

    def test_missing_run_id_raises(self):
        with self.assertRaises(spa.SprintPlanningAssembleError):
            spa.brief(sprint_id="sprint-1")

    def test_missing_sprint_id_raises(self):
        with self.assertRaises(spa.SprintPlanningAssembleError):
            spa.brief(run_id="r1")

    def test_missing_both_raises(self):
        with self.assertRaises(spa.SprintPlanningAssembleError):
            spa.brief()

    def test_module_has_no_entry_point_machinery(self):
        self.assertFalse(hasattr(spa, "ENTRY_POINTS"))
        self.assertFalse(hasattr(spa, "CLASS_A_GLUE"))


class TestMechanicalDirectives(unittest.TestCase):
    def test_mechanical_directives_bind_only_the_sprint_manifest(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        cli_values = {d["cli"] for d in result["directives"] if d["cli"] is not None}
        allowed = set(spa._SPRINT_CANDIDATE_OPS.values())
        self.assertTrue(cli_values.issubset(allowed))

    def test_dispatch_cluster_scout_present(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        ids = [d["id"] for d in result["directives"]]
        self.assertIn("d-dispatch-cluster-scout", ids)

    def test_scaffold_stub_and_pm_gate_signal_present(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        ids = [d["id"] for d in result["directives"]]
        self.assertIn("d-scaffold-stub", ids)
        self.assertIn("d-pm-gate-signal", ids)


class TestPmGatesRecommendationForbidden(unittest.TestCase):
    def test_every_pm_gate_carries_no_recommendation_and_forbidden_reason(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
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

    def test_advisory_judgment_points_never_manufacture_a_recommendation(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        pm_gate_ids = {
            "j-p1.5.0-pm-authorize-research-depth",
            "j-p1.5.4-pm-round1",
            "j-p1.5.6-pm-round2",
        }
        for jp in result["judgment_points"]:
            if jp["id"] in pm_gate_ids:
                continue
            self.assertIsNone(jp["recommendation"])
            self.assertEqual(jp["reason"], "insufficient-evidence")


class TestSpineOnlyRowsExcluded(unittest.TestCase):
    def test_no_spine_seam_only_directive(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        forbidden_ops = {
            "roadmap-number-stubs",
            "audit-roadmap",
            "read-authoring-session-path",
            "stamp-deployment-state",
            "stamp-problem-set-field",
            "read-sizing-object-fields",
            "generate-stub-index-query-callout",
        }
        cli_values = {d["cli"] for d in result["directives"] if d["cli"] is not None}
        self.assertTrue(cli_values.isdisjoint(forbidden_ops))

    def test_no_entry_point_judgment_points(self):
        result = spa.brief(run_id="r1", sprint_id="sprint-1")
        ids = {jp["id"] for jp in result["judgment_points"]}
        forbidden_ids = {
            "j-entryD-4-resolve-competing-entries",
            "j-entry-point-unresolved",
            "j-entryC-5-precondition-confirm",
            "j-p2.1.5-multi-sprint-boundary",
            "j-p2.4-disjointness-fold",
            "j-residue-stub-dedup-canonicalization",
        }
        self.assertTrue(ids.isdisjoint(forbidden_ids))


class TestMainCli(unittest.TestCase):
    def test_help_returns_ok(self):
        self.assertEqual(spa.main(["--help"]), spa.EXIT_OK)

    def test_usage_error_on_unrecognized_argument(self):
        self.assertEqual(spa.main(["--bogus"]), spa.EXIT_USAGE)

    def test_usage_error_on_missing_sprint_id(self):
        self.assertEqual(spa.main(["--run-id", "r1"]), spa.EXIT_USAGE)

    def test_malformed_decisions_json_is_usage_error(self):
        self.assertEqual(
            spa.main(["--run-id", "r1", "--sprint-id", "sprint-1", "--decisions", "{not-json"]),
            spa.EXIT_USAGE,
        )

    def test_prints_json_decision_object_on_success(self, capsys=None):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = spa.main(["--run-id", "r1", "--sprint-id", "sprint-1"])
        self.assertEqual(code, spa.EXIT_OK)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(set(parsed.keys()), _SCHEMA_TOP_LEVEL_KEYS)


@unittest.skipUnless(os.path.exists(_CLI_PATH), "sprint-planning-assemble.py CLI not found")
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

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_no_module_scope_coordinator_core_ops_import(self):
        probe = (
            "import sys, os; "
            f"sys.path.insert(0, {os.path.join(_ENGINE_ROOT, 'coordinator', 'bin', 'lib')!r}); "
            f"sys.argv = [{_CLI_PATH!r}, '--run-id', 'r1', '--sprint-id', 'sprint-1']; "
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
        cmd = [sys.executable, _CLI_PATH, "--run-id", "r1", "--sprint-id", "sprint-1"]
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
