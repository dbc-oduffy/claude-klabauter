"""test_cc_invoke_timeout_remedy.py — TimeoutExpired remedy text, not an install accusation.

Spec backlink: pln-the-claim-index-the-commit-gat-5d33ee § C5 / AC7

Prior text (BOTH `cc_invoke()` and `cc_invoke_bare()` TimeoutExpired branches, cc_invoke.py)
read "Verify CLAUDE_KLABAUTER_ROOT (<path>) and coordinator_core installation" on every timeout — wrong
on a demonstrably healthy, merely-busy engine, and independently reproduced on coverage.gate
and ceremony.wsc_tail (neither lock-related). This module asserts, at BOTH call sites:
  1. the emitted text never mentions CLAUDE_KLABAUTER_ROOT or "installation" (the misleading half), and
  2. the emitted text DOES contain the COMPUTED ceiling value itself (not merely the
     CC_INVOKE_TIMEOUT_SECS env-var name) — a text-only fix that names the knob without the
     number it actually resolved to must fail this, per the plan's own negative spec.

Fixture: floor(10) < engine_budget(30) + margin(10) = 40, so the computed ceiling (40) is
strictly above the floor — the case a naive "just print CC_INVOKE_TIMEOUT_SECS" fix would get
wrong (an operator raising the floor to, say, 20 would see no change, since 20 < 40).

Also covers the coordinator:code-reviewer [P2]/[P3] findings (2026-08-08, sidecar
coordinatorcode-reviewer-6c2abba0.md): `review-coverage-gate.py` re-appended the install-blame
line unconditionally after ANY RuntimeError (including the now-fixed timeout message), and
`append-goal-event.py` hand-built a third, divergent "engine timeout after Ns" message with no
ceiling derivation. `is_timeout_error` is the shared discriminator both callers now gate on;
`TestReviewCoverageGateInstallLineGating` and `TestAppendGoalEventUsesSharedTimeoutMessage`
exercise those two call sites directly.

Run: python3 -m pytest coordinator/bin/tests/test_cc_invoke_timeout_remedy.py -q
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — locate cc_invoke.py relative to this test file
# test file: coordinator/bin/tests/test_cc_invoke_timeout_remedy.py
# module:    coordinator/bin/lib/cc_invoke.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

_OP = "session.boot_sweep"
_FLOOR = 10
_MARGIN = 10
_ENGINE_BUDGET = 30
_COMPUTED_CEILING = max(_FLOOR, _ENGINE_BUDGET + _MARGIN)  # 40 — origin of the backlog's "40s"


class _ComputedCeilingFixture(unittest.TestCase):
    """Shared fixture: pins floor/margin via env, and the resolved op-budget map directly —
    bypassing `_resolve_op_timeouts`'s own engine spawn (it no-ops once state is non-None).
    """

    def setUp(self) -> None:
        self._env_patch = unittest.mock.patch.dict(
            _mod.os.environ,
            {
                "CC_INVOKE_TIMEOUT_SECS": str(_FLOOR),
                "CC_INVOKE_CLIENT_MARGIN_SECS": str(_MARGIN),
            },
        )
        self._env_patch.start()
        self._state_patch = unittest.mock.patch.object(_mod, "_OP_TIMEOUTS_STATE", "ok")
        self._map_patch = unittest.mock.patch.object(
            _mod, "_OP_TIMEOUTS_MAP", {"__default__": 5.0, _OP: float(_ENGINE_BUDGET)}
        )
        self._state_patch.start()
        self._map_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._state_patch.stop()
        self._map_patch.stop()


class TestOpTimeoutCeilingComputesFixtureValue(_ComputedCeilingFixture):
    """Sanity: the fixture actually produces the 40s ceiling the plan's backlog entry names."""

    def test_ceiling_is_40(self) -> None:
        ceiling = _mod._op_timeout_ceiling(_OP, "/fake/mr", {})
        self.assertEqual(ceiling, _COMPUTED_CEILING)


class TestTimeoutExceededMessageShape(_ComputedCeilingFixture):
    """Direct unit coverage of `_timeout_exceeded_message` — the shared remedy builder."""

    def test_no_claude_klabauter_root_or_installation_language(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertNotIn("CLAUDE_KLABAUTER_ROOT", msg)
        self.assertNotIn("installation", msg)

    def test_contains_computed_ceiling_value(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn(str(_COMPUTED_CEILING), msg)
        # Not just the env-var name — the actual derivation with real numbers.
        self.assertIn(str(_FLOOR), msg)
        self.assertIn(str(_ENGINE_BUDGET), msg)
        self.assertIn(str(_MARGIN), msg)

    def test_states_engine_may_be_busy(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn("busy", msg.lower())

    def test_states_floor_not_ceiling_semantics(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn("CC_INVOKE_TIMEOUT_SECS", msg)
        self.assertIn("FLOOR", msg)

    def test_names_engine_budget_knob_on_ok_branch(self) -> None:
        """The FLOOR sentence alone misleads on the engine-budget-bound branch (the
        doe-claude-em wsc_tail incident: CC_INVOKE_TIMEOUT_SECS=300 changed nothing
        because the binding term was the engine's own op budget). The message must name
        COORDINATOR_DISPATCH_TIMEOUT_SECS as the knob that raises that budget."""
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn("COORDINATOR_DISPATCH_TIMEOUT_SECS", msg)

    def test_message_starts_with_prefix_invariant(self) -> None:
        # `is_timeout_error` depends on this literal-prefix invariant holding.
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertTrue(msg.startswith(_mod._TIMEOUT_MESSAGE_PREFIX))


class TestTimeoutExceededMessageDegradedBranch(unittest.TestCase):
    """`_OP_TIMEOUTS_STATE != "ok"` (engine op-budget dump unavailable) — the message must
    still name both knobs without asserting a budget number it could not read."""

    def setUp(self) -> None:
        self._env_patch = unittest.mock.patch.dict(
            _mod.os.environ,
            {
                "CC_INVOKE_TIMEOUT_SECS": str(_FLOOR),
                "CC_INVOKE_CLIENT_MARGIN_SECS": str(_MARGIN),
            },
        )
        self._env_patch.start()
        self._state_patch = unittest.mock.patch.object(_mod, "_OP_TIMEOUTS_STATE", "error")
        self._state_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._state_patch.stop()

    def test_names_both_knobs_without_engine_budget_number(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _FLOOR)
        self.assertIn("CC_INVOKE_TIMEOUT_SECS", msg)
        self.assertIn("COORDINATOR_DISPATCH_TIMEOUT_SECS", msg)
        self.assertNotIn(str(_ENGINE_BUDGET), msg)

    def test_message_starts_with_prefix_invariant(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _FLOOR)
        self.assertTrue(msg.startswith(_mod._TIMEOUT_MESSAGE_PREFIX))


class TestBothCallSitesEmitFixedRemedy(_ComputedCeilingFixture):
    """End-to-end (mocked subprocess): both `cc_invoke()` and `cc_invoke_bare()` TimeoutExpired
    branches must emit the fixed text — the duplication is exactly why one site fixed and the
    other left is how this survived the 2026-08-07 remedy-ladder rework (plan C5 body).
    """

    def _patched(self):
        return (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_build_subprocess_env", return_value={}),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(["python3"], timeout=_COMPUTED_CEILING),
            ),
        )

    def test_cc_invoke_site(self) -> None:
        p1, p2, p3 = self._patched()
        with p1, p2, p3, self.assertRaises(RuntimeError) as ctx:
            _mod.cc_invoke(_OP, {}, "/repo")
        msg = str(ctx.exception)
        self.assertNotIn("CLAUDE_KLABAUTER_ROOT", msg)
        self.assertNotIn("installation", msg)
        self.assertIn(str(_COMPUTED_CEILING), msg)

    def test_cc_invoke_bare_site(self) -> None:
        p1, p2, p3 = self._patched()
        with p1, p2, p3, self.assertRaises(RuntimeError) as ctx:
            _mod.cc_invoke_bare(_OP, {}, "/repo")
        msg = str(ctx.exception)
        self.assertNotIn("CLAUDE_KLABAUTER_ROOT", msg)
        self.assertNotIn("installation", msg)
        self.assertIn(str(_COMPUTED_CEILING), msg)


class TestIsTimeoutError(unittest.TestCase):
    """Direct unit coverage of the shared discriminator both fixed call sites gate on."""

    def test_true_for_timeout_message(self) -> None:
        exc = RuntimeError(_mod._timeout_exceeded_message(_OP, 40))
        self.assertTrue(_mod.is_timeout_error(exc))

    def test_false_for_engine_wont_start_message(self) -> None:
        exc = RuntimeError(
            "cc_invoke: engine will not import/start (op=session.boot_sweep, rc=1)\n"
            "  ImportError — verify CLAUDE_KLABAUTER_ROOT and coordinator_core installation:"
        )
        self.assertFalse(_mod.is_timeout_error(exc))

    def test_false_for_generic_runtimeerror(self) -> None:
        self.assertFalse(_mod.is_timeout_error(RuntimeError("some other failure")))

    def test_false_for_non_runtimeerror(self) -> None:
        self.assertFalse(_mod.is_timeout_error(ValueError("not even a RuntimeError")))


def _load_bin_module(name: str, filename: str):
    """Load a bin/ CLI module by file path — mirrors the idiom used by the sibling
    archive-stamp-cli tests (extensionless/hyphenated entrypoints aren't importable by
    dotted name)."""
    path = _BIN_DIR / filename
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_rcg = _load_bin_module("review_coverage_gate_timeout_remedy_test", "review-coverage-gate.py")


class TestReviewCoverageGateInstallLineGating(unittest.TestCase):
    """[P2] regression: review-coverage-gate.py used to unconditionally append the
    install-blame line after ANY RuntimeError from cc_invoke.route — reintroducing the
    exact bug C5 removed from cc_invoke.py itself, for coverage.gate specifically. Fails
    if that line reappears on a timeout, or if it stops appearing on a genuine
    engine-won't-start failure (the distinction is the crux of the finding).
    """

    def _run_main_with(self, route_exc: Exception) -> str:
        stderr = io.StringIO()
        with unittest.mock.patch.object(_rcg.cc_invoke, "route", side_effect=route_exc), \
                contextlib.redirect_stderr(stderr):
            exit_code = _rcg.main(["HEAD~1..HEAD"])
        self.assertEqual(exit_code, 1)
        return stderr.getvalue()

    def test_timeout_never_gets_install_line(self) -> None:
        out = self._run_main_with(
            RuntimeError(_mod._timeout_exceeded_message("coverage.gate", 40))
        )
        self.assertNotIn("Verify CLAUDE_KLABAUTER_ROOT", out)

    def test_non_timeout_still_gets_install_line(self) -> None:
        out = self._run_main_with(
            RuntimeError(
                "cc_invoke: engine will not import/start (op=coverage.gate, rc=1)\n"
                "  ImportError — verify CLAUDE_KLABAUTER_ROOT and coordinator_core installation:"
            )
        )
        self.assertIn("Verify CLAUDE_KLABAUTER_ROOT", out)


_age = _load_bin_module("append_goal_event_timeout_remedy_test", "append-goal-event.py")


class TestAppendGoalEventUsesSharedTimeoutMessage(_ComputedCeilingFixture):
    """[P3] regression: append-goal-event.py's `_cc_invoke_bare` used to hand-build its own
    "engine timeout after Ns" message with no ceiling derivation — a third, divergent
    implementation of the same TimeoutExpired branch. Must now route through cc_invoke.py's
    shared `_timeout_exceeded_message`/`_op_timeout_ceiling`, matching the fixture's
    computed ceiling exactly (not just the flat CC_INVOKE_TIMEOUT_SECS floor).
    """

    def test_timeout_message_uses_shared_computed_ceiling(self) -> None:
        with unittest.mock.patch.object(
            _age, "_resolve_claude_klabauter_root", return_value="/fake/mr"
        ), unittest.mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["python3"], timeout=_COMPUTED_CEILING),
        ), self.assertRaises(RuntimeError) as ctx:
            _age._cc_invoke_bare(_OP, {}, "/repo")
        msg = str(ctx.exception)
        self.assertNotIn("CLAUDE_KLABAUTER_ROOT", msg)
        self.assertNotIn("installation", msg)
        self.assertIn(str(_COMPUTED_CEILING), msg)
        self.assertIn(str(_ENGINE_BUDGET), msg)


if __name__ == "__main__":
    unittest.main()
