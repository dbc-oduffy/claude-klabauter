"""test_cc_invoke_timeout_remedy.py — TimeoutExpired remedy text, not an install accusation.

Spec backlink: pln-the-claim-index-the-commit-gat-5d33ee § C5 / AC7

Prior text (BOTH `cc_invoke()` and `cc_invoke_bare()` TimeoutExpired branches, cc_invoke.py)
read "Verify CLAUDE_KLABAUTER_ROOT (<path>) and coordinator_core installation" on every timeout — wrong
on a demonstrably healthy engine, and independently reproduced on coverage.gate
and ceremony.wsc_tail (neither lock-related). This module asserts, at BOTH call sites:
  1. the emitted text never mentions CLAUDE_KLABAUTER_ROOT or "installation" (the misleading half), and
  2. the emitted text DOES contain the COMPUTED ceiling value itself — a text-only fix that
     omits the number it actually resolved to must fail this, per the plan's own negative spec.

REVISED 2026-08-21 (PM ruling: no EM, from this repo or any sibling, may raise a timeout
dial). The client ceiling was `max(FLOOR, engine_budget + MARGIN)` where FLOOR
(`CC_INVOKE_TIMEOUT_SECS`) and MARGIN (`CC_INVOKE_CLIENT_MARGIN_SECS`) were both unbounded
env reads — a sibling EM ran the floor at 460 and got a 460s client wait on a shared box,
and the margin was guarded by nothing. Both reads are deleted; the ceiling is now
`engine_budget(op) + _CLIENT_START_MARGIN_SECS`, and the remedy text names no knob at all.
Every fixture here sets both retired variables absurdly high, so each assertion doubles as
proof the environment cannot reach the number.

Fixture: engine_budget(30) + margin(2) = 32, against retired knobs set to 460 and 9999.

Also covers the coordinator:code-reviewer [P3] finding (2026-08-08, sidecar
coordinatorcode-reviewer-6c2abba0.md): `append-goal-event.py` hand-built a third, divergent
"engine timeout after Ns" message with no ceiling derivation. `is_timeout_error` is the shared
discriminator that caller now gates on; `TestAppendGoalEventUsesSharedTimeoutMessage` exercises
that call site directly.

The sibling [P2] case covered the same install-blame regression in
`coordinator/bin/review-coverage-gate.py`. That script and its whole review-coverage verdict
surface were removed by K-001 (`55e64be13`), so the regression has no site left to recur at and
its coverage is deliberately gone rather than repointed.

`TestRegistryReadTimeoutDistinguishedFromAbsentKey` covers plan
pln-the-ceremony-tail-stops-lying-b58fb3 § C1 (AC1/AC2a/AC3/AC3b): before this fix,
`_machine_local_get` caught `subprocess.TimeoutExpired` in the same arm as `OSError` and
returned `None` for both — the identical value a genuinely absent registry key yields — so
`_state1_remediation_message` told the operator to `git clone` / `machine-local set` a repo
that was already cloned and registered, just slow to answer under this machine's declared
50-70 concurrent-LLM load norm. This is a DIFFERENT timeout concept from the rest of this
module: `is_timeout_error`/`_TIMEOUT_MESSAGE_PREFIX` above discriminate an IPC engine
timeout (the cc_invoke()/cc_invoke_bare() transport); `_RegistryReadTimeout` and
`_REGISTRY_READ_TIMEOUT_TOKEN` discriminate a resolver-rung subprocess registry-read
timeout (`_machine_local_get`) — deliberately NOT the same seam (AC3).

Run: python3 -m pytest coordinator/bin/tests/test_cc_invoke_timeout_remedy.py -q
"""
from __future__ import annotations

import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import pytest

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
import engine_bootstrap as _engine_bootstrap_mod  # noqa: E402  (import after path setup)

_OP = "session.boot_sweep"

#: The independent second copy of the client start margin. Deliberately a literal, not
#: an import of `_mod._CLIENT_START_MARGIN_SECS` -- importing it would make this file
#: agree with any value whatsoever and assert nothing. Lowering the constant is fine
#: (a client wait ratchets down); raising it fails here on purpose, because the margin
#: is the one term of the ceiling nobody outside the engine may grow.
_PINNED_MARGIN_SECS = 2

#: Second independent copy of the no-budget fallback, same rationale.
_PINNED_NO_BUDGET_FALLBACK_SECS = 10

#: Huge values for both retired knobs. Every fixture below sets them, so any test that
#: passes proves the ceiling and the message ignore the environment -- an assertion that
#: would silently evaporate if the fixture left them unset.
_RETIRED_KNOBS_SET_ABSURDLY_HIGH = {
    "CC_INVOKE_TIMEOUT_SECS": "460",
    "CC_INVOKE_CLIENT_MARGIN_SECS": "9999",
}

_ENGINE_BUDGET = 30
_COMPUTED_CEILING = _ENGINE_BUDGET + _PINNED_MARGIN_SECS  # 32


@pytest.fixture(autouse=True)
def _isolate_op_timeout_state():
    """Restore cc_invoke's op-budget memoization to its cold state around every test.

    Same seam and same reason as the sibling fixture in `test_cc_invoke_py.py` — see that
    docstring. This module needs it for a leak of its own: the degraded-branch class
    patches `_OP_TIMEOUTS_STATE` but not `_OP_TIMEOUTS_BREADCRUMB_SHOWN`, so resolving a
    ceiling on the "error" branch sets the once-per-process breadcrumb flag and
    `patch.object` never restores it. Left alone, that silences the breadcrumb the
    sibling module asserts IS emitted.

    Deliberately does NOT clear the retired dials: every fixture here sets them absurdly
    high on purpose, so that each assertion doubles as proof the environment cannot reach
    the number.
    """
    _mod._reset_op_timeout_cache()
    yield
    _mod._reset_op_timeout_cache()


def _environ_read_keys(node: ast.AST):
    """Every key expression `node` reads out of the process environment.

    Covers `os.environ.get(K)` / `os.environ.get(K, d)` and `os.environ[K]` — the two
    shapes a timeout dial could re-enter through. Writes are a different guard's job
    (`test_no_client_timeout_dial_raises.py`); nothing in this module writes one.
    """
    def _is_environ(value: ast.AST) -> bool:
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            return True
        return isinstance(value, ast.Name) and value.id == "environ"

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_environ(node.func.value)
        and node.args
    ):
        return [node.args[0]]
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        return [node.slice]
    return []


class _ComputedCeilingFixture(unittest.TestCase):
    """Shared fixture: pins the resolved op-budget map directly — bypassing
    `_resolve_op_timeouts`'s own engine spawn (it no-ops once state is non-None) — and
    sets both retired env knobs absurdly high so every assertion below doubles as proof
    that neither reaches the number.
    """

    def setUp(self) -> None:
        self._env_patch = unittest.mock.patch.dict(
            _mod.os.environ, dict(_RETIRED_KNOBS_SET_ABSURDLY_HIGH)
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


class TestOpTimeoutCeilingIsEngineDerived(_ComputedCeilingFixture):
    """The ceiling is the engine's published budget plus the one hardcoded margin."""

    def test_ceiling_is_budget_plus_pinned_margin(self) -> None:
        ceiling = _mod._op_timeout_ceiling(_OP, "/fake/mr", {})
        self.assertEqual(ceiling, _COMPUTED_CEILING)

    def test_retired_knobs_cannot_raise_the_ceiling(self) -> None:
        """PM ruling: no EM, in this repo or a sibling, can widen the client wait.

        The floor knob was literally run at 460 from example-cockpit-repo and produced a 460s
        client wait; the margin knob was defended by nothing at all. Both are set here to
        values far above the derived ceiling, and the ceiling must not move.
        """
        ceiling = _mod._op_timeout_ceiling(_OP, "/fake/mr", {})
        self.assertEqual(ceiling, _ENGINE_BUDGET + _PINNED_MARGIN_SECS)
        self.assertLess(ceiling, int(_RETIRED_KNOBS_SET_ABSURDLY_HIGH["CC_INVOKE_TIMEOUT_SECS"]))

    def test_margin_constant_is_at_or_below_the_pinned_margin(self) -> None:
        """The ratchet itself: the margin may be lowered, never raised."""
        self.assertLessEqual(
            _mod._CLIENT_START_MARGIN_SECS,
            _PINNED_MARGIN_SECS,
            f"client start margin raised to {_mod._CLIENT_START_MARGIN_SECS}s, above the "
            f"pinned {_PINNED_MARGIN_SECS}s. The margin covers a measured ~59ms cold "
            f"start; an op that needs a longer wait needs a wider ENGINE budget.",
        )

    def test_no_environment_read_survives_in_the_ceiling_path(self) -> None:
        """Source-level: neither retired knob may be read anywhere in cc_invoke.py.

        A stale `export` in someone else's shell must be inert, which is a stronger
        property than "the arithmetic ignores it" — a later edit that reintroduces a
        NARROWING read is the shape the widening one grew back from (one `min` swapped
        for a `max` and the dial is live again).

        AST-based, not a text grep, so this module's own docstring — and
        `_op_timeout_ceiling`'s negative-spec, which must keep naming both knobs to say
        they are gone — is not a finding.
        """
        self.assertFalse(
            hasattr(_mod, "_read_positive_int_env"),
            "the generic positive-int env reader existed only to serve the two retired "
            "knobs; a surviving reader is a re-entry point for them",
        )
        tree = ast.parse(Path(_mod.__file__).read_text(encoding="utf-8"))
        reads = [
            (node.lineno, arg.value)
            for node in ast.walk(tree)
            for arg in _environ_read_keys(node)
            if isinstance(arg, ast.Constant) and arg.value in _RETIRED_KNOBS_SET_ABSURDLY_HIGH
        ]
        self.assertEqual(
            [], reads, f"cc_invoke.py still reads a retired client timeout dial: {reads}"
        )

    def test_the_guard_can_see_the_shape_it_forbids(self) -> None:
        """Mechanism check — without it the assertion above passes on a broken detector
        just as happily as on a clean file."""
        tree = ast.parse(
            "import os\n"
            'a = os.environ.get("CC_INVOKE_TIMEOUT_SECS", 10)\n'
            'b = os.environ["CC_INVOKE_CLIENT_MARGIN_SECS"]\n'
        )
        found = {
            arg.value
            for node in ast.walk(tree)
            for arg in _environ_read_keys(node)
            if isinstance(arg, ast.Constant)
        }
        self.assertEqual(set(_RETIRED_KNOBS_SET_ABSURDLY_HIGH), found)


class TestTimeoutExceededMessageShape(_ComputedCeilingFixture):
    """Direct unit coverage of `_timeout_exceeded_message` — the shared remedy builder."""

    def test_no_claude_klabauter_root_or_installation_language(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertNotIn("CLAUDE_KLABAUTER_ROOT", msg)
        self.assertNotIn("installation", msg)

    def test_contains_computed_ceiling_value(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn(str(_COMPUTED_CEILING), msg)
        # The derivation with real numbers — both surviving terms, no knob names.
        self.assertIn(str(_ENGINE_BUDGET), msg)
        self.assertIn(str(_PINNED_MARGIN_SECS), msg)

    def test_does_not_offer_a_busy_box_as_the_diagnosis(self) -> None:
        """Inverted 2026-08-21: this used to REQUIRE the phrase "may simply be busy".

        A timeout is a defect report, not a diagnosis (CLAUDE.md § Load norm). Offering
        "the engine may simply be busy" as the first reading handed every reader a
        no-action explanation for what is usually a real cost defect, on a box whose
        50-70 concurrent sessions make "busy" true at all times and therefore useless
        as a discriminator. The message still names the full derivation -- what it no
        longer does is pre-absolve the op.
        """
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertNotIn("busy", msg.lower())

    def test_names_no_timeout_knob_at_all(self) -> None:
        """Inverted 2026-08-21 (PM ruling). This used to REQUIRE both knob names.

        Coaching the reader on which variable to raise is what turned a timeout into a
        retry with a bigger number instead of a fix — measured all the way to a 460s
        client wait from a sibling repo. Both are also now false: the client ceiling
        reads neither, and `ipc._timeout_for` clamps ceremony ops after reading the
        dispatch var. Naming an override key in a guard-shaped message hands over the key
        regardless of the sentence around it (`docs/wiki/guard-messaging.md` § Register,
        B6).
        """
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertNotIn("CC_INVOKE_TIMEOUT_SECS", msg)
        self.assertNotIn("CC_INVOKE_CLIENT_MARGIN_SECS", msg)
        self.assertNotIn("COORDINATOR_DISPATCH_TIMEOUT_SECS", msg)

    def test_generic_op_also_carries_the_reconcile_before_retry_warning(self) -> None:
        """A client timeout stops the client, never the engine — true for every op, not
        only ceremony ops. The message that fires on the failure path is the last place a
        caller is told not to blind-retry something that may already have landed."""
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertIn("reconcile", msg.lower())

    def test_message_starts_with_prefix_invariant(self) -> None:
        # `is_timeout_error` depends on this literal-prefix invariant holding.
        msg = _mod._timeout_exceeded_message(_OP, _COMPUTED_CEILING)
        self.assertTrue(msg.startswith(_mod._TIMEOUT_MESSAGE_PREFIX))

    def test_ceremony_op_is_never_offered_the_dispatch_timeout_knob(self) -> None:
        """A ceremony breach must not be answered with a knob that cannot work.

        `ipc._timeout_for` clamps every `ceremony.*` op to `CEREMONY_BUDGET_SECS` with
        `min()` AFTER reading `COORDINATOR_DISPATCH_TIMEOUT_SECS`, so naming that var
        here would send the reader to spend a session on a door that is welded shut --
        the same failure the doe-claude-em wsc_tail incident already cost once with
        CC_INVOKE_TIMEOUT_SECS. Worse, it would advertise an escape hatch from a
        ratchet whose whole purpose is to have none. Since the 2026-08-21 PM ruling the
        generic branch names no knob either -- `test_names_no_timeout_knob_at_all` pins
        that -- so this case is no longer the exception it once was, only the branch
        that was always right.

        Negative spec: this asserts the message's CONTENT, not the ceiling arithmetic.
        A ceremony op's client-side ceiling is still `budget + margin` like any other op
        -- the budget bounds the ENGINE, and the client margin covering cold start is a
        separate concern with its own rationale.
        """
        msg = _mod._timeout_exceeded_message("ceremony.wsc_tail", _COMPUTED_CEILING)
        self.assertTrue(msg.startswith(_mod._TIMEOUT_MESSAGE_PREFIX))
        self.assertNotIn("COORDINATOR_DISPATCH_TIMEOUT_SECS", msg)
        self.assertIn("ratchet", msg.lower())

    def test_ceremony_message_carries_the_reconcile_before_retry_warning(self) -> None:
        """A timed-out ceremony may still have committed -- the budget makes this MORE
        likely to be hit, not less. A client-side timeout never aborts server-side
        execution, so the message that fires on the failure path is the last place a
        caller is told not to blind-retry a mutation."""
        msg = _mod._timeout_exceeded_message("ceremony.scoped_git_commit", _COMPUTED_CEILING)
        self.assertIn("reconcile", msg.lower())


class TestCeremonyPrefixAgreesWithEngine(unittest.TestCase):
    """Pins `cc_invoke._is_ceremony_op`'s literal against `ipc._CEREMONY_METHOD_PREFIX`.

    `_is_ceremony_op` deliberately RE-SPELLS `"ceremony."` as a literal instead of
    importing `coordinator_core.ipc.is_ceremony_method` — see `_is_ceremony_op`'s own
    docstring. That duplication is correct: `cc_invoke` is the thin client that runs
    BEFORE and INSTEAD OF loading the engine, and `ipc` pulls `asyncio` in at module
    top onto a cold path held to the 500ms brightline (CLAUDE.md § brightline). This
    test does not remove the duplication — doing so would put an engine import back on
    the client's cold path — it only makes a future edit to one literal and not the
    other fail loudly here instead of silently splitting client and engine on which
    ops are ceremony-budgeted. A test may import `coordinator_core.ipc` freely; the
    cold-path constraint binds `cc_invoke.py` itself, not test code.
    """

    def test_client_predicate_agrees_with_engine_predicate_across_cases(self) -> None:
        from coordinator_core import ipc

        for op in (
            "ceremony.wsc_tail",
            "ceremony.scoped_git_commit",
            "ceremony.",
            "session.boot_sweep",
            "coverage.gate",
            "",
            "ceremonyx.not_a_match",
        ):
            self.assertEqual(
                _mod._is_ceremony_op(op),
                ipc.is_ceremony_method(op),
                msg=f"cc_invoke/_is_ceremony_op and ipc.is_ceremony_method disagree on {op!r}",
            )


class TestTimeoutExceededMessageDegradedBranch(unittest.TestCase):
    """`_OP_TIMEOUTS_STATE != "ok"` (engine op-budget dump unavailable) — the message
    names the ceiling it actually waited on, asserts no budget number it could not read,
    and still names no knob."""

    def setUp(self) -> None:
        self._env_patch = unittest.mock.patch.dict(
            _mod.os.environ, dict(_RETIRED_KNOBS_SET_ABSURDLY_HIGH)
        )
        self._env_patch.start()
        self._state_patch = unittest.mock.patch.object(_mod, "_OP_TIMEOUTS_STATE", "error")
        self._state_patch.start()

    def test_fallback_ceiling_ignores_the_retired_knobs(self) -> None:
        self.assertEqual(
            _mod._op_timeout_ceiling(_OP, "/fake/mr", {}), _PINNED_NO_BUDGET_FALLBACK_SECS
        )

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._state_patch.stop()

    def test_names_no_knob_and_no_engine_budget_number(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _PINNED_NO_BUDGET_FALLBACK_SECS)
        self.assertNotIn("CC_INVOKE_TIMEOUT_SECS", msg)
        self.assertNotIn("CC_INVOKE_CLIENT_MARGIN_SECS", msg)
        self.assertNotIn("COORDINATOR_DISPATCH_TIMEOUT_SECS", msg)
        self.assertNotIn(str(_ENGINE_BUDGET), msg)

    def test_message_starts_with_prefix_invariant(self) -> None:
        msg = _mod._timeout_exceeded_message(_OP, _PINNED_NO_BUDGET_FALLBACK_SECS)
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


_age = _load_bin_module("append_goal_event_timeout_remedy_test", "append-goal-event.py")


class TestAppendGoalEventUsesSharedTimeoutMessage(_ComputedCeilingFixture):
    """[P3] regression: append-goal-event.py's `_cc_invoke_bare` used to hand-build its own
    "engine timeout after Ns" message with no ceiling derivation — a third, divergent
    implementation of the same TimeoutExpired branch. Must now route through cc_invoke.py's
    shared `_timeout_exceeded_message`/`_op_timeout_ceiling`, matching the fixture's
    computed ceiling exactly (not a flat fallback).
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


class TestMachineLocalGetDistinguishesTimeoutFromAbsent(unittest.TestCase):
    """`_machine_local_get` itself: OSError still collapses to None (absent-key path
    unchanged); TimeoutExpired now raises `_RegistryReadTimeout` instead of collapsing
    to the identical `None` an absent key yields."""

    def setUp(self) -> None:
        self._impl_patch = unittest.mock.patch.object(
            _mod,
            "_machine_local_impl_resolver",
            return_value=unittest.mock.Mock(
                machine_local_impl_path=lambda env_override=None: "/fake/_machine_local.py"
            ),
        )
        self._impl_patch.start()
        self._isfile_patch = unittest.mock.patch.object(_mod.os.path, "isfile", return_value=True)
        self._isfile_patch.start()

    def tearDown(self) -> None:
        self._impl_patch.stop()
        self._isfile_patch.stop()

    def test_oserror_still_returns_none(self) -> None:
        with unittest.mock.patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertIsNone(_mod._machine_local_get("repos.claude_klabauter"))

    def test_timeout_raises_registry_read_timeout(self) -> None:
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["python3"], timeout=10),
        ):
            with self.assertRaises(_mod._RegistryReadTimeout):
                _mod._machine_local_get("repos.claude_klabauter")


class TestRegistryReadTimeoutToken(unittest.TestCase):
    """AC3b: the literal token both rungs (cc_invoke.py here, claude_klabauter_root.py in C2) must
    agree on, pinned so drift fails loudly."""

    def test_token_literal(self) -> None:
        self.assertEqual(
            _mod._REGISTRY_READ_TIMEOUT_TOKEN, "machine-local registry read timed out"
        )


class TestRegistryReadTimeoutDistinguishedFromAbsentKey(unittest.TestCase):
    """AC1/AC2a/AC3: end-to-end through `route()` — a registry-read timeout at
    `_resolve_claude_klabauter_root`'s Rung 2+ (with Rung 3 self-location also missing) must reach
    `_state1_remediation_message` as a distinguishable outcome, not the generic
    clone/register text a genuinely absent key gets."""

    def _route_with_resolve_side_effect(self, exc: Exception) -> str:
        with unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", side_effect=exc
        ), self.assertRaises(RuntimeError) as ctx:
            _mod.route("some.op", {}, "/repo", legacy_fn=lambda: (_ for _ in ()).throw(
                RuntimeError("legacy stub: no bash fallback")
            ))
        return str(ctx.exception)

    def test_timeout_text_names_reader_timeout_not_clone_register(self) -> None:
        msg = self._route_with_resolve_side_effect(
            _mod._RegistryReadTimeout(
                f"{_mod._REGISTRY_READ_TIMEOUT_TOKEN} (10s bound) resolving "
                "repos.claude_klabauter, and self-location also missed."
            )
        )
        self.assertIn(_mod._REGISTRY_READ_TIMEOUT_TOKEN, msg)
        self.assertNotIn("git clone", msg)
        self.assertNotIn("machine-local set", msg)

    def test_absent_key_text_is_byte_identical_to_pre_fix(self) -> None:
        # AC2a — regression guard: the genuinely-absent-key path through
        # _state1_remediation_message(op, None) is unchanged, byte for byte.
        #
        # Repointed 2026-08-21 from the `CLAUDE_KLABAUTER_ROOT` spelling. This pin had been red on a
        # clean checkout since the engine-root rename (docs/plans/2026-08-20-an-engine-
        # root-is-not-named-for-the-repo.md § C14) moved `_state1_remediation_message` to
        # `COORDINATOR_ENGINE_ROOT` without updating it. The byte-identical property is
        # the point of the guard and is preserved; only the variable this ladder actually
        # names has changed. Unrelated to the client-dial retirement — that touched the
        # IPC-timeout ladder, and this is the machine-local registry-read ladder the
        # module docstring keeps deliberately separate.
        expected = (
            "cc_invoke: native seam unavailable for op='some.op' — claude-klabauter is a mandatory "
            "coordinator dependency in every environment (W0.5 Option B+C, 2026-07-19); there is "
            "no bash fallback under the big-bang cutover.\n"
            "  COORDINATOR_ENGINE_ROOT could not be resolved via any rung below.\n"
            "  Resolution ladder (in order):\n"
            "    1. COORDINATOR_ENGINE_ROOT environment variable\n"
            "    2. <settings-home>/machine-local/.claude-klabauter-live-root pointer file\n"
            "    3. `machine-local get repos.claude_klabauter` registry entry\n"
            "    4. coordinator_core.invoke importable from the resolved root\n"
            "  Remediation: clone claude-klabauter as a sibling repo "
            "(git clone https://github.com/dbc-oduffy/claude-klabauter) and register it — "
            "set $COORDINATOR_ENGINE_ROOT, write the settings-home pointer file, or run "
            "`machine-local set repos.claude_klabauter /path/to/claude-klabauter` — then retry. "
            "See docs/install/AGENT.md § Fail-loud claude-klabauter resolution, or run /coordinator:setup."
        )
        msg = self._route_with_resolve_side_effect(RuntimeError("generic unresolvable root"))
        self.assertEqual(msg, expected)
        self.assertIn("git clone", msg)
        self.assertIn("machine-local set", msg)

    def test_registry_read_timeout_is_a_runtimeerror_subclass(self) -> None:
        # AC3 — the sentinel is a distinct NAMED outcome, not a re-widened
        # is_timeout_error/_TIMEOUT_MESSAGE_PREFIX match.
        exc = _mod._RegistryReadTimeout("x")
        self.assertIsInstance(exc, RuntimeError)
        self.assertFalse(_mod.is_timeout_error(exc))

    def test_resolve_claude_klabauter_root_raises_registry_read_timeout_on_timeout_and_missed_self_locate(
        self,
    ) -> None:
        with unittest.mock.patch.object(
            _engine_bootstrap_mod,
            "_machine_local_get",
            side_effect=_mod._RegistryReadTimeout("registry timed out"),
        ), unittest.mock.patch.object(
            _engine_bootstrap_mod, "_walk_up_to_checkout", return_value=None
        ), unittest.mock.patch.dict(_mod.os.environ, {}, clear=False):
            # Rung 1 reads the NEW name since the engine-root rename closed the dual-read
            # window; popping only the old one left an ambient COORDINATOR_ENGINE_ROOT
            # answering before the registry rung this test is about ever ran.
            _mod.os.environ.pop(_mod._ENGINE_ROOT_NEW_VAR, None)
            _mod.os.environ.pop(_mod._ENGINE_ROOT_OLD_VAR, None)
            with unittest.mock.patch(
                "builtins.open", side_effect=OSError("no pointer file")
            ):
                with self.assertRaises(_mod._RegistryReadTimeout) as ctx:
                    _mod._resolve_claude_klabauter_root()
        self.assertIn(_mod._REGISTRY_READ_TIMEOUT_TOKEN, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
