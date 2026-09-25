"""test_coordinator_doc_new_spinoff_ordering_edge.py -- `--gated-open` on
`--type spinoff` writes an ORDERING edge (`blocked_by`), never the
predecessor:none-by-design lineage edge schema rule A3a-3 forces on every
spinoff kind.

Purpose: state/cross-repo/inbox/2026-09-25-doe-claude-em-doe-issues-92-95-
engine-asks.md ask 1 -- an ordered spinoff chain (architecture-audit Step 4)
previously carried its order only in prose, because `--predecessor` is
refused for --type spinoff (A3a-3) and --gated-open was handoff-scoped only.
`blocked_by` is schema-permitted on ANY kind (handoff.schema.json) and is
already read for readiness by `reconcile.gate_eval.derive_readiness` and
`ops.handoff_children.blocked_by_dependents` regardless of kind -- this
change only widens `_scaffold_spinoff`/the CLI type-gate to expose it,
reusing `_scaffold_handoff`'s existing blocked_by/derive_readiness wiring
rather than inventing a second mechanism.

FAST TIER ONLY: no subprocess spawn, no git spawn -- same idiom as
`test_coordinator_doc_new_spinoff_resolvable_fields.py`.

Run:
    python3 -m pytest coordinator/bin/tests/test_coordinator_doc_new_spinoff_ordering_edge.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_spinoff_ordering_edge_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_spinoff_ordering_edge_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_A_UUID = "bc1ca482-6b06-4943-ab49-92c9b35482ad"


def _patched_spinoff(**kwargs):
    with mock.patch.object(
        _cli, "_resolve_session_id", return_value=_A_UUID
    ), mock.patch.object(
        _cli, "_resolve_session_display_name", return_value=None
    ), mock.patch.object(
        _cli, "_resolve_spinoff_workstream", return_value=None
    ):
        return _cli._scaffold_spinoff(title="t", branch="b", **kwargs)


class ScaffoldSpinoffGatedOpenTest(unittest.TestCase):
    def test_gated_open_writes_blocked_by_not_predecessor(self):
        content = _patched_spinoff(gated_open="hnd-earlier-spinoff-abc123")
        self.assertIn("blocked_by:", content)
        self.assertIn("  - \"hnd-earlier-spinoff-abc123\"", content)
        # A3a-3: the lineage edge stays none-by-design regardless of the
        # ordering edge -- --gated-open must never leak onto predecessor.
        self.assertIn("predecessor: none", content)

    def test_omitted_gated_open_emits_no_blocked_by_key(self):
        content = _patched_spinoff()
        self.assertNotIn("blocked_by:", content)
        self.assertIn("predecessor: none", content)

    def test_blank_gated_open_refused_fail_loud(self):
        with self.assertRaises(SystemExit) as ctx:
            _patched_spinoff(gated_open="   ")
        self.assertEqual(ctx.exception.code, 1)

    def test_gated_open_derives_awaiting_gate_readiness(self):
        """An unresolved blocker (no corpus at scaffold time) derives
        awaiting_gate/pickup_ready:false via the same derive_readiness
        evaluator _scaffold_handoff already uses -- the readiness readers
        (gate_eval, handoff_children.blocked_by_dependents) that already
        honor blocked_by on any kind now see it on spinoff too."""
        content = _patched_spinoff(gated_open="hnd-earlier-spinoff-abc123")
        self.assertIn("deployment_state: awaiting_gate", content)
        self.assertIn("pickup_ready: false", content)


class ScaffoldSpinoffGatedOpenUnresolvableEngineTest(unittest.TestCase):
    def test_engine_unresolvable_with_gated_open_refuses_fail_loud(self):
        with mock.patch.object(_cli, "_derive_readiness", None):
            with self.assertRaises(SystemExit) as ctx:
                _patched_spinoff(gated_open="hnd-earlier-spinoff-abc123")
        self.assertEqual(ctx.exception.code, 1)

    def test_engine_unresolvable_without_gated_open_still_degrades(self):
        """No-flag path must not gain an engine dependency it never had --
        same posture as _scaffold_handoff's own no-flag leg."""
        with mock.patch.object(_cli, "_derive_readiness", None):
            content = _patched_spinoff()
        self.assertIn("deployment_state: ready_to_fire", content)
        self.assertIn("pickup_ready: true", content)


if __name__ == "__main__":
    unittest.main()
