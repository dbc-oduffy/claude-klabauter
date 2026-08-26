"""test_cc_invoke_rung_parity.py — warm-hit vs cold-spawn refusal parity for
`cc_invoke()`/`cc_invoke_bare()` (C2, state/dispatch-briefs/2026-08-26-the-
op-clis-dial-warm-from-the-process/C2.md).

C1 added `_try_in_process_warm_reach` (unused); C2 wires it into both public
entry points via `_capture_warm_reach`/`_apply_warm_envelope` so a warm hit
is served in-process and a spawn happens only on a genuine miss. This file
pins the rung mapping the C2 brief calls out as "most likely to go wrong
quietly": `_try_in_process_warm_reach`'s three-valued return (None / an
error envelope / a success envelope) must refuse or succeed IDENTICALLY to
the cold-spawn path's own `_raise_on_process_failure` ladder.

Tests (see the C2 brief's own rung enumeration):
  (1)  Timeout, COMPUTE_ONLY op — a warm miss (None) falls through to the
       spawn, which carries the real per-op ceiling.
  (1a) Mutating op, delivered-but-unanswered — WARM_DISPATCH_INDETERMINATE
       error envelope raises, message text intact, subprocess.run NEVER
       called (AC9a).
  (2)  Structural-pin error envelope raises StructuralPinError (by type);
       a generic error envelope raises plain RuntimeError.
  (4)  An envelope with neither `result` nor `error` raises.
  stderr parity — a warm-served op-level refusal (result dict shape, not a
       JSON-RPC error envelope) still reaches `route_mutation`'s caller with
       a non-empty `RouteMutationError.op_stderr`, matching the spawned
       path's own `_stderr_sink` contract.
  AC8  reachability — both `cc_invoke()` and `cc_invoke_bare()` reach
       `subprocess.run` ONLY when `_capture_warm_reach` reports a miss.

Every test that pins a refusal asserts it FAILS if the in-process path
SUCCEEDS where the spawned path would have refused (the C2 brief's own
requirement) — never just "raises something".

Run: pytest coordinator/bin/tests/test_cc_invoke_rung_parity.py -v
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_cc_invoke_py.py's own layout.
# test file: coordinator/bin/tests/test_cc_invoke_rung_parity.py
# module:    coordinator/bin/lib/cc_invoke.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)
from coordinator_core.ipc import STRUCTURAL_PIN_ERROR  # noqa: E402
from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE  # noqa: E402

pytestmark = pytest.mark.cadence


def _dump_absent_proc() -> Any:
    """A `--dump-op-timeouts` probe response feature-detected as "absent"
    (older-engine shape) — mirrors test_cc_invoke_py.py's own helper so the
    op-budget probe never diverts a test's own op spawn mock."""
    proc = unittest.mock.Mock()
    proc.returncode = 2
    proc.stdout = ""
    proc.stderr = "unrecognized arguments: --dump-op-timeouts"
    return proc


def _is_dump_probe(argv: Any) -> bool:
    return "--dump-op-timeouts" in argv


@pytest.fixture(autouse=True)
def _isolate_op_timeout_state():
    """Reset cc_invoke's once-per-process op-budget memoisation around every
    test — same rationale as test_cc_invoke_py.py's identical fixture."""
    _mod._reset_op_timeout_cache()
    yield
    _mod._reset_op_timeout_cache()


def _mr():
    return unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr")


class TestRung1TimeoutComputeOnlyReachesSpawn(unittest.TestCase):
    """(1) A warm miss (None) on a COMPUTE_ONLY op falls through to the
    cold spawn, unchanged — the spawn carries the real per-op ceiling that a
    wedged warm pipe's read-deadline expiry (also surfaced as None) cannot
    provide in-process."""

    def test_cc_invoke_spawns_on_warm_miss(self) -> None:
        success_envelope = (
            '{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'
        )
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = success_envelope
        op_proc.stderr = ""

        spawned: list[Any] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                return _dump_absent_proc()
            spawned.append(cmd)
            return op_proc

        with (
            _mr(),
            unittest.mock.patch.object(_mod, "_capture_warm_reach", return_value=(None, "")),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            result = _mod.cc_invoke("diagnostics.probe", {}, "/repo")

        self.assertEqual(result, {"ok": True})
        self.assertTrue(spawned, "a warm miss (None) must reach subprocess.run")

    def test_cc_invoke_bare_spawns_on_warm_miss(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = '{"ok": true}'
        op_proc.stderr = ""

        spawned: list[Any] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                return _dump_absent_proc()
            spawned.append(cmd)
            return op_proc

        with (
            _mr(),
            unittest.mock.patch.object(_mod, "_capture_warm_reach", return_value=(None, "")),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            result = _mod.cc_invoke_bare("diagnostics.probe", {}, "/repo")

        self.assertEqual(result, {"ok": True})
        self.assertTrue(spawned, "a warm miss (None) must reach subprocess.run")


class TestRung1aIndeterminateNeverSpawns(unittest.TestCase):
    """(1a) A mutating op's delivered-but-unanswered warm dispatch returns
    the WARM_DISPATCH_INDETERMINATE error envelope, not None. This MUST
    raise a refusal and MUST NOT fall through to the spawn — spawning here
    risks a duplicate execution of a mutation that may already have
    landed. Discharges AC9a."""

    def _indeterminate_envelope(self) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": WARM_DISPATCH_INDETERMINATE,
                "message": "delivered-but-unanswered mutation (read timeout)",
            },
        }

    def test_cc_invoke_raises_and_never_spawns(self) -> None:
        def _run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "subprocess.run must NOT be called on a WARM_DISPATCH_INDETERMINATE hit"
            )

        with (
            _mr(),
            unittest.mock.patch.object(
                _mod,
                "_capture_warm_reach",
                return_value=(self._indeterminate_envelope(), ""),
            ),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke("scoped_git_commit", {}, "/repo")

        self.assertIn(
            "delivered-but-unanswered mutation (read timeout)", str(ctx.exception)
        )
        # A test that only asserts "raises something" does not discharge
        # AC9a — the negative half (no spawn) is asserted via the raising
        # `_run` above, which would have failed this test differently
        # (AssertionError from inside `_run`, not the expected RuntimeError)
        # had cc_invoke fallen through to the spawn.

    def test_cc_invoke_bare_raises_and_never_spawns(self) -> None:
        def _run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "subprocess.run must NOT be called on a WARM_DISPATCH_INDETERMINATE hit"
            )

        with (
            _mr(),
            unittest.mock.patch.object(
                _mod,
                "_capture_warm_reach",
                return_value=(self._indeterminate_envelope(), ""),
            ),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("scoped_git_commit", {}, "/repo")

        self.assertIn(
            "delivered-but-unanswered mutation (read timeout)", str(ctx.exception)
        )


class TestRung2ErrorEnvelopeClassification(unittest.TestCase):
    """(2) A structural-pin error envelope raises StructuralPinError (by
    type, matching the spawned path's rc==2 branch so a caller catching
    StructuralPinError by name keeps taking that branch on a warm hit); a
    generic error envelope raises plain RuntimeError, NOT StructuralPinError.
    Neither test returns a value."""

    def test_structural_pin_envelope_raises_structural_pin_error(self) -> None:
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": STRUCTURAL_PIN_ERROR, "message": "contract pin broken"},
        }
        with (
            _mr(),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, "")
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("must not spawn on a warm error envelope"),
            ),
        ):
            with self.assertRaises(_mod.StructuralPinError):
                _mod.cc_invoke("diagnostics.always_structural_pin", {}, "/repo")

    def test_generic_error_envelope_raises_plain_runtime_error(self) -> None:
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "op refused"},
        }
        with (
            _mr(),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, "")
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("must not spawn on a warm error envelope"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke("diagnostics.always_refuses", {}, "/repo")
            self.assertNotIsInstance(
                ctx.exception,
                _mod.StructuralPinError,
                "a generic error code must not be misclassified as a structural pin",
            )


class TestRung4MalformedEnvelope(unittest.TestCase):
    """(4) A warm-served dict carrying neither `result` nor `error` raises —
    applies the same envelope-parse rung unchanged to a warm hit."""

    def test_missing_result_and_error_raises(self) -> None:
        envelope = {"jsonrpc": "2.0", "id": 1}
        with (
            _mr(),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, "")
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("must not spawn on a warm hit at all"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                _mod.cc_invoke("some.op", {}, "/repo")


class TestStderrSinkParityOnWarmServedRefusal(unittest.TestCase):
    """A warm-served OP-LEVEL refusal (a `result` dict shaped like
    build_act_result's partial-failure envelope, not a JSON-RPC `error`
    envelope) must still reach `route_mutation`'s caller with a non-empty
    `RouteMutationError.op_stderr` — the popped `_stderr` text
    `_capture_warm_reach` recovers must not be silently dropped just
    because the response arrived warm instead of cold."""

    def test_route_mutation_op_stderr_non_empty_on_warm_refusal(self) -> None:
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"exit_code": 1, "error": "setup failed: missing config"},
        }
        stderr_text = "[op] setup failed: missing config\n"

        def _legacy() -> None:
            raise AssertionError("legacy_fn must not be called on State-2")

        with (
            _mr(),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, stderr_text)
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("must not spawn on a warm-served refusal"),
            ),
        ):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("some.mutating.op", {}, "/repo", _legacy)

        self.assertTrue(
            ctx.exception.op_stderr.strip(),
            "RouteMutationError.op_stderr must not be empty for a warm-served refusal",
        )
        self.assertIn("setup failed: missing config", ctx.exception.op_stderr)


class TestAC8ReachabilityOnlyOnWarmMiss(unittest.TestCase):
    """AC8 (jointly with C1/C3): the two op spawn sites in this module
    (cc_invoke, cc_invoke_bare) are reachable only on a warm miss or with
    warm disabled — asserted directly, not left implied by the rung tests
    above."""

    def test_cc_invoke_never_spawns_on_a_warm_hit(self) -> None:
        envelope = {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}
        with (
            _mr(),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, "")
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("cc_invoke must not spawn on a warm hit"),
            ),
        ):
            result = _mod.cc_invoke("some.op", {}, "/repo")
        self.assertEqual(result, {"x": 1})

    def test_cc_invoke_bare_never_spawns_on_a_warm_hit(self) -> None:
        envelope = {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}
        with (
            _mr(),
            unittest.mock.patch.object(
                _mod, "_capture_warm_reach", return_value=(envelope, "")
            ),
            unittest.mock.patch(
                "subprocess.run",
                side_effect=AssertionError("cc_invoke_bare must not spawn on a warm hit"),
            ),
        ):
            result = _mod.cc_invoke_bare("some.op", {}, "/repo")
        self.assertEqual(result, {"x": 1})

    def test_cc_invoke_spawns_only_when_warm_reports_a_miss(self) -> None:
        """Reachability the OTHER direction: a miss (None) DOES reach the
        spawn — pinning both halves of "reachable only on a miss" in one
        place rather than trusting the rung-1 test above to imply it."""
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = '{"jsonrpc": "2.0", "id": 1, "result": {"y": 2}}'
        op_proc.stderr = ""
        spawned: list[Any] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                return _dump_absent_proc()
            spawned.append(cmd)
            return op_proc

        with (
            _mr(),
            unittest.mock.patch.object(_mod, "_capture_warm_reach", return_value=(None, "")),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            result = _mod.cc_invoke("some.op", {}, "/repo")

        self.assertEqual(result, {"y": 2})
        self.assertTrue(spawned)


if __name__ == "__main__":
    unittest.main()
