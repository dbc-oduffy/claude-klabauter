"""test_cc_invoke_py.py — unit tests for cc_invoke.py.

Tests:
  AC1  State-1: seam absent → legacy_fn called, no spawn
  AC1  State-2 success: native path returns bare-result dict
  AC1  State-2 transport-fail: raises; legacy_fn NOT called
  AC9  grep-gate: cc_invoke.py contains no retired transport patterns
       (coordinator_core.client / UDS / AF_UNIX / auth-token / three-state)

Run: pytest coordinator/bin/tests/test_cc_invoke_py.py -v

Renamed from the hyphenated, pytest-uncollectable test-cc-invoke-py.py to
this test_* filename; dropped the unused hand-rolled fail()/ok()/FAIL_COUNT
print-reporting helpers and the __main__ TestSuite runner (pytest collects
the unittest.TestCase classes directly); test bodies unchanged.

Shell-parity apparatus retired 2026-07-26: the AC10 cross-implementation
parity classes (TestCrossImplParity, TestRouteMutationParity) compared this
module against coordinator/lib/coordinator-core-invoke.sh and
coordinator/lib/strangler-facade.sh — bash oracles retired with the bash
transport under the naked-Python mandate (CLAUDE.md § Runtime conventions;
DR-047/DR-059) and deleted from the tree. Those classes could only ever go
green by resurrecting a `.sh` implementation this repo has outlawed; do not
restore them. Every Python-side assertion the golden vectors exercised
(raise-on-empty-stdout / raise-on-import-error / raise-on-error-envelope /
raise-on-timeout for cc_invoke(); both route_mutation() refusal shapes) is
already covered standalone by TestRouteState2TransportFail and
TestRouteMutation below, so no coverage was lost in the deletion.

Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C1 / AC1 / AC9
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — locate cc_invoke.py relative to this test file
# test file: coordinator/bin/tests/test_cc_invoke_py.py
# module:    coordinator/bin/lib/cc_invoke.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_COORDINATOR_ROOT = _BIN_DIR.parent
_REPO_ROOT = _COORDINATOR_ROOT.parent
_CC_INVOKE_PY = _LIB_DIR / "cc_invoke.py"

# Ensure lib is on sys.path so we can import cc_invoke
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)


# ---------------------------------------------------------------------------
# Fake CLAUDE_KLABAUTER_ROOT factory — creates a temp dir with a coordinator_core.invoke
# package whose __main__.py reads FAKE_INVOKE_MODE env var and emits the
# corresponding golden vector response. Used for AC1 integration tests and AC10
# cross-implementation parity tests.
# ---------------------------------------------------------------------------

_FAKE_MAIN_PY = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys, time

    mode = os.environ.get("FAKE_INVOKE_MODE", "success")
    bare = "--bare" in sys.argv[1:]

    # --bare contract (mirrors coordinator-core-invoke.sh cc_invoke() / cc_invoke.py
    # cc_invoke_bare()'s documented assumption): on --bare, stdout on rc0 IS the bare
    # result object directly (server-side envelope strip) -- a JSON-RPC protocol-level
    # error ALWAYS exits nonzero on --bare, never rc0-with-error-key. Non-bare mode
    # keeps the legacy full-envelope shape (including the exit0-with-error-key case
    # cc_invoke()'s defensive envelope parse also covers).

    if mode == "success":
        result = {"out_path": "/fake/queue/entry.yaml", "status": "ok"}
        if bare:
            print(json.dumps(result))
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))
        sys.exit(0)
    elif mode == "empty":
        # exits 0, no stdout
        sys.exit(0)
    elif mode == "import_error":
        sys.stderr.write("ImportError: No module named 'coordinator_core'\\n")
        sys.exit(1)
    elif mode == "op_error_envelope":
        if bare:
            # --bare: a JSON-RPC protocol error always exits nonzero -- no stdout
            # envelope to strip, diagnostic on stderr (matches real invoke's --bare
            # contract asserted in cc_invoke.sh / cc_invoke_bare()'s docstrings).
            sys.stderr.write("op failed: bad params\\n")
            sys.exit(1)
        else:
            # non-bare: legacy shape -- exits 0, stdout has error envelope.
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "op failed: bad params"},
            }))
            sys.exit(0)
    elif mode == "timeout":
        # sleep long enough to trigger timeout
        time.sleep(999)
    elif mode == "act_refusal":
        # exits 0, transport succeeds; op-level refusal lives INSIDE the bare
        # result (exit_code!=0 + failed[...]) — route_mutation/strangle_route_mutation
        # refusal shape (1), handoff/memo _err.
        result = {"exit_code": 2, "acted": [], "failed": [{"reason": "nope"}]}
        if bare:
            print(json.dumps(result))
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))
        sys.exit(0)
    elif mode == "error_field":
        # exits 0, transport succeeds; op-level refusal lives INSIDE the bare
        # result as a bare "error" string with exit_code ABSENT — route_mutation/
        # strangle_route_mutation refusal shape (2), completion_ops/plan_ops.
        result = {"error": "plan not found"}
        if bare:
            print(json.dumps(result))
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))
        sys.exit(0)
    else:
        sys.stderr.write(f"FAKE_INVOKE_MODE unknown: {mode!r}\\n")
        sys.exit(2)
""")


def _make_fake_claude_klabauter_root(tmp_dir: str) -> str:
    """Create a fake CLAUDE_KLABAUTER_ROOT at tmp_dir with a coordinator_core.invoke package."""
    cc_dir = os.path.join(tmp_dir, "coordinator_core")
    invoke_dir = os.path.join(cc_dir, "invoke")
    os.makedirs(invoke_dir, exist_ok=True)

    (Path(cc_dir) / "__init__.py").write_text("")
    (Path(invoke_dir) / "__init__.py").write_text("")
    (Path(invoke_dir) / "__main__.py").write_text(_FAKE_MAIN_PY)

    return tmp_dir


# ---------------------------------------------------------------------------
# AC1 — State-1: seam absent → legacy_fn called, no spawn
# ---------------------------------------------------------------------------

class TestRouteState1(unittest.TestCase):
    """AC1 State-1: when find_spec returns None, route() calls legacy_fn and never spawns."""

    def test_legacy_called_when_seam_absent(self) -> None:
        """State-1: seam absent → legacy_fn() called, no subprocess spawned."""
        legacy_called: list[bool] = []

        def _legacy() -> str:
            legacy_called.append(True)
            return "legacy-result"

        # Mock _seam_present to return False (seam absent)
        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/claude-klabauter"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=False),
            unittest.mock.patch("subprocess.run") as mock_run,
        ):
            result = _mod.route("test.op", {}, "/fake/repo", _legacy)

        self.assertEqual(result, "legacy-result")
        self.assertTrue(legacy_called, "legacy_fn must be called on State-1")
        mock_run.assert_not_called()

    def test_legacy_called_when_claude_klabauter_root_unresolvable(self) -> None:
        """State-1 fallback: if CLAUDE_KLABAUTER_ROOT can't be resolved, route() goes to legacy."""
        legacy_called: list[bool] = []

        def _legacy() -> str:
            legacy_called.append(True)
            return "legacy-fallback"

        with unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("cannot resolve")
        ):
            result = _mod.route("test.op", {}, "/fake/repo", _legacy)

        self.assertEqual(result, "legacy-fallback")
        self.assertTrue(legacy_called)


# ---------------------------------------------------------------------------
# W0.5 Option B+C — State-1 remediation: legacy_fn raise wrapped with the
# four-rung claude-klabauter-install-specific remediation message.
# ---------------------------------------------------------------------------

class TestRouteState1Remediation(unittest.TestCase):
    """State-1: when legacy_fn() raises, route() wraps it with claude-klabauter remediation."""

    def test_legacy_raise_wrapped_with_remediation_when_root_unresolvable(self) -> None:
        """CLAUDE_KLABAUTER_ROOT unresolvable + legacy_fn raises → RuntimeError carries the
        four-rung resolution ladder, not the bare legacy_fn message."""

        def _legacy() -> None:
            raise RuntimeError("native seam required (no bash fallback -- big-bang cutover)")

        with unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("cannot resolve")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.route("test.op", {}, "/fake/repo", _legacy)

        msg = str(ctx.exception)
        self.assertIn("test.op", msg)
        self.assertIn("CLAUDE_KLABAUTER_ROOT environment variable", msg)
        self.assertIn(".claude-klabauter-root pointer file", msg)
        self.assertIn("repos.claude_klabauter", msg)
        self.assertIn("git clone https://github.com/dbc-example-operator/claude-klabauter", msg)
        # The original legacy_fn exception is chained, not silently discarded.
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_legacy_raise_wrapped_with_remediation_when_seam_absent(self) -> None:
        """CLAUDE_KLABAUTER_ROOT resolvable but seam absent + legacy_fn raises → same wrap."""

        def _legacy() -> None:
            raise RuntimeError("native seam required (no bash fallback -- big-bang cutover)")

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/claude-klabauter"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=False),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.route("test.op", {}, "/fake/repo", _legacy)

        msg = str(ctx.exception)
        self.assertIn("test.op", msg)
        self.assertIn("/fake/claude-klabauter", msg)
        self.assertIn("Resolution ladder", msg)

    def test_legacy_success_not_wrapped(self) -> None:
        """State-1 legacy_fn() success is passed through unwrapped (no remediation noise)."""

        def _legacy() -> str:
            return "legacy-ok"

        with unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("cannot resolve")
        ):
            result = _mod.route("test.op", {}, "/fake/repo", _legacy)

        self.assertEqual(result, "legacy-ok")


# ---------------------------------------------------------------------------
# AC1 — State-2 success: native path returns bare result dict
# ---------------------------------------------------------------------------

class TestRouteState2Success(unittest.TestCase):
    """AC1 State-2: seam present, cc_invoke succeeds → bare result dict returned."""

    def test_bare_result_returned_on_success(self) -> None:
        """State-2 success: route() returns the bare result dict, not the envelope."""
        success_envelope = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"out_path": "/queue/entry.yaml", "status": "ok"},
        })
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope + "\n"
        mock_proc.stderr = ""

        legacy_called: list[bool] = []

        def _legacy() -> None:
            legacy_called.append(True)

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/claude-klabauter"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", return_value=mock_proc),
        ):
            result = _mod.route("queue.append", {"key": "val"}, "/fake/repo", _legacy)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["out_path"], "/queue/entry.yaml")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("jsonrpc", result, "wrapper must be stripped — no double-nesting")
        self.assertNotIn("result", result, "bare result dict must not re-wrap itself")
        self.assertFalse(legacy_called, "legacy_fn must NOT be called on State-2 success")

    def test_spawn_uses_correct_args(self) -> None:
        """State-2: subprocess call uses sys.executable -m coordinator_core.invoke form."""
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"x": 1}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_calls: list[Any] = []

        def _capture_run(*args: Any, **kwargs: Any) -> Any:
            captured_calls.append((args, kwargs))
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture_run),
        ):
            _mod.cc_invoke("queue.append", {"a": 1}, "/the/repo")

        self.assertEqual(len(captured_calls), 1)
        call_args = captured_calls[0][0][0]  # first positional arg = cmd list
        # Review: cross-slice (DR-148) — cc_invoke.py now uses sys.executable, not "python3".
        self.assertEqual(call_args[0], sys.executable)
        self.assertEqual(call_args[1], "-m")
        self.assertEqual(call_args[2], "coordinator_core.invoke")
        self.assertEqual(call_args[3], "queue.append")
        self.assertIn("--repo", call_args)
        repo_idx = call_args.index("--repo")
        self.assertEqual(call_args[repo_idx + 1], "/the/repo")

    def test_params_serialised_as_json(self) -> None:
        """State-2: params dict is serialised to compact JSON before spawn."""
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_calls: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured_calls.append((args, kwargs))
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
        ):
            _mod.cc_invoke("op", {"foo": "bar", "n": 42}, "/repo")

        cmd = captured_calls[0][0][0]
        params_arg = cmd[4]  # index: python3 -m coordinator_core.invoke OP PARAMS ...
        parsed = json.loads(params_arg)
        self.assertEqual(parsed, {"foo": "bar", "n": 42})

    def test_claude_klabauter_root_in_subprocess_env(self) -> None:
        """State-2: subprocess env has CLAUDE_KLABAUTER_ROOT set and PYTHONPATH prepended."""
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_envs: list[dict] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured_envs.append(kwargs.get("env", {}))
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
        ):
            _mod.cc_invoke("op", {}, "/repo")

        env = captured_envs[0]
        self.assertEqual(env.get("CLAUDE_KLABAUTER_ROOT"), "/fake/mr")
        self.assertIn("/fake/mr", env.get("PYTHONPATH", ""))

    def test_claude_klabauter_root_idempotency_in_pythonpath(self) -> None:
        """PYTHONPATH idempotency fence: claude_klabauter_root not prepended when already present.

        A regression here (flipped condition) would otherwise pass the suite silently.
        """
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_envs: list[dict] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured_envs.append(kwargs.get("env", {}))
            return mock_proc

        # Pre-populate PYTHONPATH with claude_klabauter_root to trigger the idempotency fence.
        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
            # os.pathsep, not a hardcoded ":" — the production fence in
            # _build_subprocess_env() splits/joins on os.pathsep, which is ";" on
            # Windows. A ":"-separated fixture is one opaque entry there, so the
            # fence correctly finds no match and prepends, and the assertion below
            # then reads a false duplicate. POSIX-only fixture = permanently red on
            # Windows for a fence that is actually correct.
            unittest.mock.patch.dict(
                os.environ, {"PYTHONPATH": f"/fake/mr{os.pathsep}/other/path"}
            ),
        ):
            _mod.cc_invoke("op", {}, "/repo")

        env = captured_envs[0]
        pythonpath = env.get("PYTHONPATH", "")
        parts = [p for p in pythonpath.split(os.pathsep) if p]
        self.assertEqual(
            parts.count("/fake/mr"),
            1,
            f"PYTHONPATH idempotency fail: /fake/mr appears {parts.count('/fake/mr')}x in {pythonpath!r}",
        )


# ---------------------------------------------------------------------------
# AC1 — State-2 transport-fail: raises; legacy_fn NOT called
# ---------------------------------------------------------------------------

class TestRouteState2TransportFail(unittest.TestCase):
    """AC1 State-2 transport-fail: any failure raises; legacy_fn is never called."""

    def _assert_raises_not_legacy(
        self,
        proc_mock: Any,
        side_effect: Any = None,
        contains: str = "",
    ) -> RuntimeError:
        """Helper: verify route() raises RuntimeError and does not call legacy_fn."""
        legacy_called: list[bool] = []

        def _legacy() -> None:
            legacy_called.append(True)

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
        ):
            if side_effect is not None:
                with (
                    unittest.mock.patch("subprocess.run", side_effect=side_effect),
                    self.assertRaises(RuntimeError) as ctx,
                ):
                    _mod.route("op", {}, "/repo", _legacy)
            else:
                with (
                    unittest.mock.patch("subprocess.run", return_value=proc_mock),
                    self.assertRaises(RuntimeError) as ctx,
                ):
                    _mod.route("op", {}, "/repo", _legacy)

        self.assertFalse(legacy_called, "legacy_fn must NOT be called after State-2 transport fail")
        if contains:
            self.assertIn(contains, str(ctx.exception), f"exception message should contain {contains!r}")
        return ctx.exception  # type: ignore[return-value]

    def test_timeout_raises(self) -> None:
        self._assert_raises_not_legacy(
            proc_mock=None,
            side_effect=subprocess.TimeoutExpired(["python3"], timeout=10),
            contains="timeout",
        )

    def test_nonzero_exit_import_error_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "ImportError: No module named 'coordinator_core'"
        self._assert_raises_not_legacy(mock_proc, contains="engine will not import/start")

    def test_nonzero_exit_import_module_not_found_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "ModuleNotFoundError: No module named 'coordinator_core'"
        self._assert_raises_not_legacy(mock_proc, contains="engine will not import/start")

    def test_nonzero_exit_op_error_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "some op-level error"
        exc = self._assert_raises_not_legacy(mock_proc)
        # Should report exit code, not ImportError message
        self.assertIn("exited 1", str(exc))
        self.assertNotIn("engine will not import", str(exc))

    def test_empty_stdout_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        self._assert_raises_not_legacy(mock_proc, contains="empty stdout")

    def test_error_envelope_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "op failed"},
        })
        mock_proc.stderr = ""
        self._assert_raises_not_legacy(mock_proc, contains="error envelope")

    def test_missing_result_key_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "status": "ok"})
        mock_proc.stderr = ""
        self._assert_raises_not_legacy(mock_proc, contains="'result' key")

    def test_invalid_json_raises(self) -> None:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = "not-json"
        mock_proc.stderr = ""
        self._assert_raises_not_legacy(mock_proc, contains="valid JSON")


# ---------------------------------------------------------------------------
# route_mutation — mutation-aware exit_code/failed inspection (C1.5)
# ---------------------------------------------------------------------------

class TestRouteMutation(unittest.TestCase):
    """route_mutation: raises on op-level refusal (exit_code!=0 or non-empty failed),
    returns unchanged on op-level success. Mirrors shell strangle_route_mutation.
    """

    def test_act_success_returns_unchanged(self) -> None:
        """(a) act-success {"exit_code":0,"acted":[...],"failed":[]} -> returns unchanged."""
        envelope = {"exit_code": 0, "acted": ["a", "b"], "failed": []}

        def _legacy() -> None:
            self.fail("legacy_fn must not be called when route() succeeds")

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            result = _mod.route_mutation("memo.send", {}, "/fake/repo", _legacy)

        self.assertEqual(result, envelope)

    def test_act_refusal_raises(self) -> None:
        """(b) act-refusal {"exit_code":2,"failed":[{...}]} -> raises RuntimeError."""
        envelope = {"exit_code": 2, "acted": [], "failed": [{"reason": "nope"}]}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("memo.send", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_setup_error_raises(self) -> None:
        """(c) setup-error {"exit_code":1,...} -> raises RuntimeError."""
        envelope = {"exit_code": 1, "error": "setup failed"}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("memo.send", str(ctx.exception))
        self.assertIn("1", str(ctx.exception))

    def test_exit_code_none_with_no_failed_returns_unchanged(self) -> None:
        """A dict result with no exit_code/failed keys at all must pass through unchanged."""
        envelope = {"out_path": "/queue/entry.yaml", "status": "ok"}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            result = _mod.route_mutation("queue.append", {}, "/fake/repo", lambda: None)

        self.assertEqual(result, envelope)

    def test_transport_failure_propagates_unwrapped(self) -> None:
        """route() raising (transport failure) propagates through route_mutation unchanged."""
        with unittest.mock.patch.object(
            _mod, "route", side_effect=RuntimeError("cc_invoke: engine timeout")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("engine timeout", str(ctx.exception))

    def test_error_field_without_exit_code_raises(self) -> None:
        """Finding 1: {"error": "..."} with exit_code ABSENT (completion_ops/plan_ops
        shape) must raise — this is the shape cc_invoke's OUTER envelope-error check
        cannot see (it's nested inside a present 'result' payload), and the shape the
        shell oracle's error_field branch exists to catch.
        """
        envelope = {"error": "plan not found"}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("plan.update", {}, "/fake/repo", lambda: None)

        self.assertIn("plan.update", str(ctx.exception))
        self.assertIn("plan not found", str(ctx.exception))
        self.assertEqual(ctx.exception.result, envelope)

    def test_error_field_with_exit_code_zero_raises(self) -> None:
        """Finding 1 variant: exit_code explicitly 0 (rc-0-equivalent) alongside a
        truthy 'error' string must also raise — "absent or 0" per the shell docstring.
        """
        envelope = {"exit_code": 0, "error": "completion already closed"}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("completion.close", {}, "/fake/repo", lambda: None)

        self.assertIn("completion already closed", str(ctx.exception))

    def test_error_field_with_nonzero_exit_code_not_double_reported_wrong(self) -> None:
        """A non-zero exit_code alongside 'error' still raises (via the exit_code
        branch) and the message carries both details — no shape is masked.
        """
        envelope = {"exit_code": 1, "error": "setup failed"}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("setup failed", str(ctx.exception))
        self.assertIn("1", str(ctx.exception))

    def test_failed_truthy_with_exit_code_zero_raises(self) -> None:
        """Finding 6: {"exit_code": 0, "failed": [...]} — the standalone `or failed`
        branch — must independently raise even though exit_code is 0/absent.
        """
        envelope = {"exit_code": 0, "failed": [{"reason": "x"}]}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("memo.send", str(ctx.exception))

    def test_non_dict_result_passes_through_unchanged(self) -> None:
        """Finding 7: a non-dict route() result (State-1 legacy pass-through of a bool/
        string/None) must flow through route_mutation untouched, without raising.
        """
        for legacy_result in (None, "legacy-string-result", True, 42):
            with self.subTest(legacy_result=legacy_result):
                with unittest.mock.patch.object(_mod, "route", return_value=legacy_result):
                    result = _mod.route_mutation("queue.append", {}, "/fake/repo", lambda: None)
                self.assertEqual(result, legacy_result)

    def test_failed_non_list_truthy_raises_route_mutation_error_not_typeerror(self) -> None:
        """Finding 3: a malformed/future-drifted 'failed' that is truthy but not
        len()-able (e.g. a bool or int) must still raise RouteMutationError — never an
        uncaught TypeError from a bare len() call.
        """
        envelope = {"exit_code": 0, "failed": True}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("non-list shape", str(ctx.exception))

    def test_exit_code_string_zero_does_not_false_positive(self) -> None:
        """Finding 4: a stringly-typed exit_code "0" must NOT false-positive-raise
        (Python's "0" != 0 is True on bare cross-type comparison) — coerced to int
        first, mirroring the shell oracle's int()/except cast.
        """
        envelope = {"exit_code": "0", "acted": ["a"]}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            result = _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertEqual(result, envelope)

    def test_exit_code_string_nonzero_still_raises(self) -> None:
        """Finding 4 counterpart: a stringly-typed non-zero exit_code must still raise
        after coercion (e.g. "2" -> 2 != 0)."""
        envelope = {"exit_code": "2", "failed": []}

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertIn("memo.send", str(ctx.exception))

    def test_route_mutation_error_carries_full_result(self) -> None:
        """Finding 2: the raised exception exposes the full offending payload via
        .result, not just a string-parsed exit_code/failed-count summary — a caller
        needing structured detail (e.g. per-item 'reason' fields) isn't limited to
        string-parsing the message.
        """
        envelope = {
            "exit_code": 2,
            "failed": [{"reason": "nope"}, {"reason": "also-nope"}],
        }

        with unittest.mock.patch.object(_mod, "route", return_value=envelope):
            with self.assertRaises(_mod.RouteMutationError) as ctx:
                _mod.route_mutation("memo.send", {}, "/fake/repo", lambda: None)

        self.assertEqual(ctx.exception.result, envelope)
        self.assertEqual(ctx.exception.result["failed"], envelope["failed"])


# ---------------------------------------------------------------------------
# _seam_present — direct unit tests (F4)
# ---------------------------------------------------------------------------

class TestSeamPresent(unittest.TestCase):
    """Direct coverage for _seam_present: present→True, absent→False, sys.path clean.

    _seam_present had zero direct unit coverage previously — every call in the rest
    of the suite was mocked. These tests call _seam_present directly against a real
    fake claude_klabauter_root built by _make_fake_claude_klabauter_root.
    """

    _fake_claude_klabauter_tmpdir: tempfile.TemporaryDirectory  # type: ignore[type-arg]
    _fake_claude_klabauter_root: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._fake_claude_klabauter_tmpdir = tempfile.TemporaryDirectory(prefix="cc-invoke-seam-")
        cls._fake_claude_klabauter_root = _make_fake_claude_klabauter_root(cls._fake_claude_klabauter_tmpdir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fake_claude_klabauter_tmpdir.cleanup()

    def test_seam_present_returns_true_when_module_exists(self) -> None:
        """_seam_present returns True when coordinator_core.invoke is in claude_klabauter_root."""
        result = _mod._seam_present(self._fake_claude_klabauter_root)
        self.assertTrue(
            result,
            f"_seam_present must return True for a valid fake claude_klabauter_root: {self._fake_claude_klabauter_root!r}",
        )

    def test_seam_present_returns_false_for_nonexistent_root(self) -> None:
        """_seam_present returns False when the path has no coordinator_core.invoke.

        Also exercises the ModuleNotFoundError catch path in _seam_present.
        """
        result = _mod._seam_present("/nonexistent/path/cc_invoke_test_abc123")
        self.assertFalse(result, "_seam_present must return False for a nonexistent root")

    def test_sys_path_not_polluted_after_present_probe(self) -> None:
        """sys.path must not contain claude_klabauter_root after a successful _seam_present probe."""
        # Precondition: claude_klabauter_root not in sys.path before the call.
        self.assertNotIn(
            self._fake_claude_klabauter_root,
            sys.path,
            "test precondition: fake_claude_klabauter_root must not be in sys.path before probe",
        )
        _mod._seam_present(self._fake_claude_klabauter_root)
        self.assertNotIn(
            self._fake_claude_klabauter_root,
            sys.path,
            "sys.path must not be polluted after _seam_present(present) probe",
        )

    def test_sys_path_not_polluted_after_absent_probe(self) -> None:
        """sys.path must not contain nonexistent root after a False _seam_present probe."""
        nonexistent = "/nonexistent/path/cc_invoke_test_abc123"
        self.assertNotIn(nonexistent, sys.path,
                         "test precondition: nonexistent path must not be in sys.path before probe")
        _mod._seam_present(nonexistent)
        self.assertNotIn(
            nonexistent,
            sys.path,
            "sys.path must not be polluted after _seam_present(absent) probe",
        )

    def test_seam_present_returns_false_for_relative_path(self) -> None:
        """_seam_present returns False for a relative path (module-hijack defense).

        Fix #1 guard: a relative path must be rejected before sys.path injection —
        prepending a relative path to sys.path[0] is the module-hijack vector.
        """
        result = _mod._seam_present("relative/path/that/looks/real")
        self.assertFalse(
            result,
            "_seam_present must return False for a relative path (hijack defense-in-depth)",
        )

    def test_seam_present_returns_false_for_path_that_is_a_file(self) -> None:
        """_seam_present returns False when the path exists but is a file, not a directory.

        Fix #1 guard: os.path.isdir() must reject a valid-but-non-dir path.
        """
        import tempfile as _tf
        with _tf.NamedTemporaryFile(prefix="cc_invoke_test_", suffix=".txt") as fh:
            file_path = fh.name
            result = _mod._seam_present(file_path)
        self.assertFalse(
            result,
            f"_seam_present must return False when path is a file, not a dir: {file_path!r}",
        )


# ---------------------------------------------------------------------------
# Fix #2 — CC_INVOKE_TIMEOUT_SECS: robust parse, no raw ValueError traceback
# ---------------------------------------------------------------------------

class TestTimeoutEnvGuard(unittest.TestCase):
    """Fix #2 regression: invalid CC_INVOKE_TIMEOUT_SECS falls back to 10 without raising.

    A malformed timeout tuning knob must not break the transport — defaulting is the
    resilient choice and eliminates the raw ValueError traceback (the actual defect).
    """

    def setUp(self) -> None:
        # cc_invoke() now resolves the per-op ceiling (shared with cc_invoke_bare), which
        # memoizes the --dump-op-timeouts probe in module globals. Reset per-test so the
        # dump-probe path is exercised deterministically and cannot leak across tests.
        _mod._reset_op_timeout_cache()

    def _invoke_with_timeout_env(self, raw_val: str) -> tuple[bool, str]:
        """Invoke cc_invoke with CC_INVOKE_TIMEOUT_SECS=raw_val (subprocess mocked to succeed).

        Returns (raised: bool, stderr_output: str).
        Captures stderr to detect the clean warning line.
        """
        success_envelope = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"ok": True}
        })
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        import io
        captured_stderr = io.StringIO()

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch("subprocess.run", return_value=mock_proc),
            unittest.mock.patch.dict(os.environ, {"CC_INVOKE_TIMEOUT_SECS": raw_val}, clear=False),
            unittest.mock.patch("sys.stderr", captured_stderr),
        ):
            try:
                _mod.cc_invoke("op", {}, "/repo")
                return False, captured_stderr.getvalue()
            except Exception as exc:
                return True, str(exc)

    def test_nonnumeric_timeout_falls_back_to_default(self) -> None:
        """CC_INVOKE_TIMEOUT_SECS='notanumber' must not raise; must fall back to 10s."""
        raised, stderr_out = self._invoke_with_timeout_env("notanumber")
        self.assertFalse(raised, "cc_invoke must NOT raise on non-numeric CC_INVOKE_TIMEOUT_SECS")
        self.assertIn(
            "notanumber",
            stderr_out,
            "warning must name the offending value; got stderr: {stderr_out!r}",
        )
        self.assertIn(
            "warn",
            stderr_out.lower(),
            f"warning must appear on stderr; got: {stderr_out!r}",
        )

    def test_zero_timeout_falls_back_to_default(self) -> None:
        """CC_INVOKE_TIMEOUT_SECS='0' (non-positive) must not raise; must fall back to 10s."""
        raised, stderr_out = self._invoke_with_timeout_env("0")
        self.assertFalse(raised, "cc_invoke must NOT raise when CC_INVOKE_TIMEOUT_SECS=0")
        self.assertIn(
            "0",
            stderr_out,
            f"warning must name the offending value; got stderr: {stderr_out!r}",
        )

    def test_negative_timeout_falls_back_to_default(self) -> None:
        """CC_INVOKE_TIMEOUT_SECS='-5' (negative) must not raise; must fall back to 10s."""
        raised, stderr_out = self._invoke_with_timeout_env("-5")
        self.assertFalse(raised, "cc_invoke must NOT raise when CC_INVOKE_TIMEOUT_SECS=-5")

    def test_valid_timeout_is_used(self) -> None:
        """CC_INVOKE_TIMEOUT_SECS='30' (valid) is honored as the FLOOR on the op call.

        cc_invoke() now resolves the per-op ceiling (max(FLOOR, budget+MARGIN)) exactly
        like cc_invoke_bare(). With the dump surface ABSENT (older-claude-klabauter branch), the
        ceiling degrades to a flat FLOOR, so the op-call timeout is the valid FLOOR (30).
        The op call is isolated from the --dump-op-timeouts probe spawn, which the ceiling
        path issues first.
        """
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        op_proc.stderr = ""

        op_call_timeouts: list[int] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return _dump_absent_proc()  # older-claude-klabauter -> ceiling degrades to flat FLOOR
            op_call_timeouts.append(kwargs.get("timeout"))
            return op_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch("subprocess.run", side_effect=_run),
            unittest.mock.patch.dict(os.environ, {"CC_INVOKE_TIMEOUT_SECS": "30"}, clear=False),
        ):
            _mod.cc_invoke("op", {}, "/repo")

        self.assertEqual(len(op_call_timeouts), 1, "exactly one op-call spawn expected")
        self.assertEqual(op_call_timeouts[0], 30, "valid FLOOR must be passed to the op-call subprocess.run")


# ---------------------------------------------------------------------------
# AC9 — grep-gate: cc_invoke.py must not contain retired transport patterns
# ---------------------------------------------------------------------------

class TestGrepGate(unittest.TestCase):
    """AC9: cc_invoke.py contains no retired transport patterns.

    The grep-gate checks for CODE-level usage patterns (actual imports, socket opens,
    token reads, daemon-aware state machine), not bare string mentions that may appear
    in documentation/comments. Patterns are chosen to catch actual code misuse.
    """

    def _source_text(self) -> str:
        return _CC_INVOKE_PY.read_text(encoding="utf-8")

    def test_no_coordinator_core_client(self) -> None:
        """No coordinator_core.client import — the client-seam is DR-215-retired.

        Checks for the actual import forms: 'from coordinator_core.client' and
        'from coordinator_core import client'. Bare string 'coordinator_core.client'
        may appear in negative-spec comments; the import forms cannot.
        """
        src = self._source_text()
        self.assertNotIn(
            "from coordinator_core.client",
            src,
            "coordinator_core.client import is DR-215-retired; must not appear in cc_invoke.py",
        )
        self.assertNotIn(
            "from coordinator_core import client",
            src,
            "coordinator_core client-module import is DR-215-retired; must not appear in cc_invoke.py",
        )

    def test_no_uds_socket(self) -> None:
        """No Unix-domain socket usage — the UDS transport is DR-215-retired.

        Checks for socket.AF_UNIX (the Python socket constant that opens a UDS).
        """
        src = self._source_text()
        self.assertNotIn(
            "socket.AF_UNIX",
            src,
            "socket.AF_UNIX (Unix domain socket) is DR-215-retired; must not appear in cc_invoke.py",
        )

    def test_no_auth_token(self) -> None:
        """No auth-token read — IPC auth tokens are DR-215-retired.

        Checks for the Python variable naming convention 'auth_token' and the env-var
        form 'AUTH_TOKEN'. These patterns catch actual reads, not doc mentions.
        """
        src = self._source_text()
        self.assertNotIn(
            "auth_token",
            src,
            "auth_token variable is DR-215-retired; must not appear in cc_invoke.py",
        )
        self.assertNotIn(
            "AUTH_TOKEN",
            src,
            "AUTH_TOKEN env-var read is DR-215-retired; must not appear in cc_invoke.py",
        )

    def test_no_three_state_router(self) -> None:
        """No three-state router — command-type transport is two-state only.

        Checks for 'State-3' literal (a three-state label) and 'State3' (camelCase form).
        """
        src = self._source_text()
        self.assertNotIn(
            "State-3",
            src,
            "State-3 label implies three-state router; cc_invoke.py must be two-state only",
        )
        self.assertNotIn(
            "State3",
            src,
            "State3 label implies three-state router; cc_invoke.py must be two-state only",
        )


# ---------------------------------------------------------------------------
# cc_invoke_bare — the --bare transport promotion (Wave 1a) + DEC-1..3 budget.
#
# cc_invoke_bare is the Python promotion of coordinator-core-invoke.sh's cc_invoke:
# it spawns coordinator_core.invoke with --bare (stdout IS the bare result object,
# no envelope) and --params-file (ARG_MAX-safe), and resolves a per-op timeout
# ceiling from the engine's --dump-op-timeouts map (DEC-1..3). These tests mock
# subprocess.run, dispatching on whether "--dump-op-timeouts" is in the argv so a
# single side_effect stands in for BOTH spawns cc_invoke_bare may make.
# ---------------------------------------------------------------------------


def _dump_absent_proc() -> Any:
    """A fake --dump-op-timeouts response for an older claude-klabauter (DEC-2a, silent)."""
    p = unittest.mock.Mock()
    p.returncode = 2
    p.stdout = ""
    p.stderr = "invoke.py: error: unrecognized arguments: --dump-op-timeouts"
    return p


def _bare_run_dispatcher(op_proc: Any, dump_proc: Any | None = None,
                         op_side_effect: Any = None,
                         captured: list | None = None) -> Any:
    """Build a subprocess.run side_effect that branches on argv.

    dump call (argv contains --dump-op-timeouts) -> dump_proc (default: absent).
    op call                                      -> op_proc, or raises op_side_effect.
    If `captured` is provided, the op call's (cmd, kwargs, params_file_content) is appended.
    """
    _dump = dump_proc if dump_proc is not None else _dump_absent_proc()

    def _run(*args: Any, **kwargs: Any) -> Any:
        cmd = args[0]
        if "--dump-op-timeouts" in cmd:
            return _dump
        if captured is not None:
            params_content = None
            if "--params-file" in cmd:
                pf = cmd[cmd.index("--params-file") + 1]
                with open(pf, encoding="utf-8") as fh:
                    params_content = fh.read()
            captured.append((cmd, kwargs, params_content))
        if op_side_effect is not None:
            raise op_side_effect
        return op_proc

    return _run


class TestCcInvokeBare(unittest.TestCase):
    """cc_invoke_bare: --bare transport contract + shared fail-closed ladder."""

    def setUp(self) -> None:
        _mod._reset_op_timeout_cache()

    def _mr(self) -> Any:
        return unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr")

    def test_bare_success_returns_result_dict(self) -> None:
        """--bare: stdout IS the bare result dict — returned directly, no envelope strip."""
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"exit_code": 0, "acted": ["a"], "failed": []}) + "\n"
        op_proc.stderr = ""

        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            result = _mod.cc_invoke_bare("memo.send", {"k": "v"}, "/repo")

        self.assertEqual(result, {"exit_code": 0, "acted": ["a"], "failed": []})
        self.assertNotIn("jsonrpc", result, "--bare result must be bare (no envelope wrapper)")
        self.assertNotIn("result", result, "bare result must not re-wrap itself")

    def test_bare_spawn_uses_bare_and_params_file(self) -> None:
        """Op spawn carries --bare + --params-file; params serialised to the temp file."""
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"ok": True})
        op_proc.stderr = ""

        captured: list = []
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc, captured=captured)
        ):
            _mod.cc_invoke_bare("queue.append", {"foo": "bar", "n": 42}, "/the/repo")

        self.assertEqual(len(captured), 1, "expected exactly one op spawn")
        cmd, _kwargs, params_content = captured[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:3], ["-m", "coordinator_core.invoke"])
        self.assertEqual(cmd[3], "queue.append")
        self.assertIn("--bare", cmd)
        self.assertIn("--params-file", cmd)
        self.assertIn("--repo", cmd)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "/the/repo")
        # params-file content must be the serialised params, NOT on argv.
        self.assertIsNotNone(params_content)
        self.assertEqual(json.loads(params_content), {"foo": "bar", "n": 42})
        # params JSON must NOT appear as a bare argv element (ARG_MAX-immune contract).
        self.assertNotIn(json.dumps({"foo": "bar", "n": 42}, separators=(",", ":")), cmd)

    def test_bare_empty_stdout_raises(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = ""
        op_proc.stderr = ""
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("empty stdout", str(ctx.exception))

    def test_bare_nonzero_import_error_raises(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 1
        op_proc.stdout = ""
        op_proc.stderr = "ModuleNotFoundError: No module named 'coordinator_core'"
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("engine will not import/start", str(ctx.exception))

    def test_bare_nonzero_op_error_raises(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 1
        op_proc.stdout = ""
        op_proc.stderr = "some op-level failure"
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("exited 1", str(ctx.exception))
        self.assertNotIn("engine will not import", str(ctx.exception))

    def test_bare_invalid_json_raises(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = "not json {{"
        op_proc.stderr = ""
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_bare_non_dict_result_raises(self) -> None:
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = "[1, 2, 3]"
        op_proc.stderr = ""
        with self._mr(), unittest.mock.patch(
            "subprocess.run", side_effect=_bare_run_dispatcher(op_proc)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("not a JSON object", str(ctx.exception))

    def test_bare_timeout_raises(self) -> None:
        with self._mr(), unittest.mock.patch.dict(
            os.environ, {"CC_INVOKE_TIMEOUT_SECS": "1"}, clear=False
        ), unittest.mock.patch(
            "subprocess.run",
            side_effect=_bare_run_dispatcher(
                op_proc=None,
                op_side_effect=subprocess.TimeoutExpired(["python"], timeout=1),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("timeout", str(ctx.exception))
        self.assertIn("after 1s", str(ctx.exception))

    def test_bare_params_file_cleaned_up(self) -> None:
        """The --params-file temp file is unlinked after the spawn (no scratch leak)."""
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"ok": True})
        op_proc.stderr = ""

        seen_paths: list[str] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return _dump_absent_proc()
            pf = cmd[cmd.index("--params-file") + 1]
            seen_paths.append(pf)
            self.assertTrue(os.path.exists(pf), "params file must exist during the spawn")
            return op_proc

        with self._mr(), unittest.mock.patch("subprocess.run", side_effect=_run):
            _mod.cc_invoke_bare("op", {"a": 1}, "/repo")

        self.assertEqual(len(seen_paths), 1)
        self.assertFalse(
            os.path.exists(seen_paths[0]),
            "params file must be unlinked after cc_invoke_bare returns",
        )


class TestCcInvokeBareOpTimeout(unittest.TestCase):
    """DEC-1..3: per-op timeout ceiling resolved from the engine's --dump-op-timeouts map.

    Each test asserts the exact `timeout=` value passed to the op-call subprocess.run
    (max(FLOOR, budget+MARGIN)), pinning the arithmetic rather than only pass/fail.
    """

    def setUp(self) -> None:
        _mod._reset_op_timeout_cache()

    def _mr(self) -> Any:
        return unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr")

    def _dump_ok(self, payload: dict) -> Any:
        p = unittest.mock.Mock()
        p.returncode = 0
        p.stdout = json.dumps(payload)
        p.stderr = ""
        return p

    def _run_capture_timeout(self, op: str, dump_proc: Any, env: dict) -> tuple[int, str]:
        """Invoke cc_invoke_bare; return (op_timeout_passed, stderr_captured)."""
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"ok": True})
        op_proc.stderr = ""

        captured_timeouts: list[int] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return dump_proc
            captured_timeouts.append(kwargs.get("timeout"))
            return op_proc

        import io
        cap_err = io.StringIO()
        with self._mr(), unittest.mock.patch.dict(
            os.environ, env, clear=False
        ), unittest.mock.patch("subprocess.run", side_effect=_run), unittest.mock.patch(
            "sys.stderr", cap_err
        ):
            _mod.cc_invoke_bare(op, {}, "/repo")

        self.assertEqual(len(captured_timeouts), 1)
        return captured_timeouts[0], cap_err.getvalue()

    def test_override_op_resolves_budget_plus_margin(self) -> None:
        """AC1: op WITH an override -> _t = max(FLOOR, budget+MARGIN) = max(1, 2+1) = 3.

        The probe now runs unconditionally (once per process) -- no env-var gate needed.
        """
        t, _err = self._run_capture_timeout(
            "custom.op",
            self._dump_ok({"custom.op": 2, "__default__": 5}),
            {
                "CC_INVOKE_TIMEOUT_SECS": "1",
                "CC_INVOKE_CLIENT_MARGIN_SECS": "1",
            },
        )
        self.assertEqual(t, 3, "override op must resolve max(1, 2+1)=3, not flat FLOOR")

    def test_override_less_op_resolves_default_plus_margin(self) -> None:
        """AC2: op WITHOUT an override -> map['__default__']+MARGIN = max(1, 2+1) = 3."""
        t, _err = self._run_capture_timeout(
            "other.op",
            self._dump_ok({"custom.op": 99, "__default__": 2}),
            {
                "CC_INVOKE_TIMEOUT_SECS": "1",
                "CC_INVOKE_CLIENT_MARGIN_SECS": "1",
            },
        )
        self.assertEqual(t, 3, "override-less op must resolve via __default__, not flat FLOOR")

    def test_dump_absent_flat_floor_silent(self) -> None:
        """AC3(a): dump surface absent (older claude-klabauter) -> flat FLOOR, no breadcrumb."""
        t, err = self._run_capture_timeout(
            "other.op",
            _dump_absent_proc(),
            {
                "CC_INVOKE_TIMEOUT_SECS": "1",
                "CC_INVOKE_CLIENT_MARGIN_SECS": "1",
            },
        )
        self.assertEqual(t, 1, "dump-absent must fall back to flat FLOOR (1)")
        self.assertNotIn("op-budget dump failed", err, "dump-absent must be silent")

    def test_dump_errored_flat_floor_with_breadcrumb(self) -> None:
        """AC3(b): dump present but malformed (missing __default__) -> flat FLOOR + breadcrumb."""
        dump = self._dump_ok({"custom.op": 5})  # missing "__default__" -> error state
        t, err = self._run_capture_timeout(
            "other.op",
            dump,
            {
                "CC_INVOKE_TIMEOUT_SECS": "1",
                "CC_INVOKE_CLIENT_MARGIN_SECS": "1",
            },
        )
        self.assertEqual(t, 1, "dump-errored must still fall back to flat FLOOR (1)")
        self.assertIn("op-budget dump failed", err, "dump-errored must emit the breadcrumb")

    def test_dump_probe_runs_without_env_var(self) -> None:
        """F3 (retired gate): the probe spawns unconditionally -- no
        CC_INVOKE_OP_TIMEOUT_DUMP_ENABLED needed -- and resolves the op-specific budget.
        """
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"ok": True})
        op_proc.stderr = ""

        dump_spawned = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                dump_spawned.append(cmd)
                return self._dump_ok({"custom.op": 2, "__default__": 5})
            return op_proc

        import io
        cap_err = io.StringIO()
        with self._mr(), unittest.mock.patch.dict(
            os.environ,
            {"CC_INVOKE_TIMEOUT_SECS": "1", "CC_INVOKE_CLIENT_MARGIN_SECS": "1"},
            clear=False,
        ), unittest.mock.patch("subprocess.run", side_effect=_run), unittest.mock.patch(
            "sys.stderr", cap_err
        ):
            os.environ.pop("CC_INVOKE_OP_TIMEOUT_DUMP_ENABLED", None)
            _mod.cc_invoke_bare("custom.op", {}, "/repo")

        self.assertEqual(len(dump_spawned), 1, "dump probe must spawn unconditionally")
        self.assertNotIn("op-budget dump failed", cap_err.getvalue())


class TestCcInvokeCompositeOpTimeout(unittest.TestCase):
    """B2 regression: cc_invoke() (NON-bare) must resolve the per-op ceiling, so a
    composite op's facade timeout is never TIGHTER than its engine-side budget.

    session.boot_sweep runs five sequential git-heavy sweeps under the engine's
    DISPATCH_TIMEOUT_SECS budget (default 30s). It routes route_mutation -> route ->
    cc_invoke() (the NON-bare path), which previously used a flat CC_INVOKE_TIMEOUT_SECS
    floor (default 10s) — strictly tighter than the engine's 30s. On a loaded machine the
    facade fired `cc_invoke: engine timeout after 10s (op=session.boot_sweep)` and killed
    the sweep well before the engine's own clock. These tests pin the invariant that the
    two clocks cannot silently drift back apart.
    """

    def setUp(self) -> None:
        _mod._reset_op_timeout_cache()

    def test_boot_sweep_facade_ceiling_ge_engine_budget_mocked(self) -> None:
        """cc_invoke() resolves _t = max(FLOOR, budget+MARGIN) for a composite op.

        Dump reports the engine budget (30) for session.boot_sweep (via __default__,
        since boot_sweep carries no explicit override today). FLOOR=10, MARGIN=10 ->
        _t = max(10, 30+10) = 40 >= 30. The op-call spawn (not the dump probe) carries it.
        """
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        op_proc.stderr = ""

        engine_budget = 30
        dump = unittest.mock.Mock()
        dump.returncode = 0
        dump.stdout = json.dumps({"__default__": engine_budget})
        dump.stderr = ""

        op_call_timeouts: list[int] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return dump
            op_call_timeouts.append(kwargs.get("timeout"))
            return op_proc

        with unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"
        ), unittest.mock.patch("subprocess.run", side_effect=_run), unittest.mock.patch.dict(
            os.environ,
            {"CC_INVOKE_TIMEOUT_SECS": "10", "CC_INVOKE_CLIENT_MARGIN_SECS": "10"},
            clear=False,
        ):
            _mod.cc_invoke("session.boot_sweep", {}, "/repo")

        self.assertEqual(len(op_call_timeouts), 1, "exactly one op-call spawn expected")
        self.assertEqual(
            op_call_timeouts[0], 40,
            "cc_invoke() must resolve max(FLOOR, budget+MARGIN)=max(10,30+10)=40, "
            "NOT the flat 10s floor that strangled boot_sweep",
        )
        self.assertGreaterEqual(
            op_call_timeouts[0], engine_budget,
            "INVARIANT: a composite op's facade timeout must be >= its engine budget",
        )

    def test_boot_sweep_facade_ceiling_ge_engine_budget_real(self) -> None:
        """End-to-end anti-drift: read BOTH clocks from the REAL engine and assert
        facade-ceiling >= engine-budget for session.boot_sweep.

        This is the guard that catches a future edit nudging either clock: the facade
        ceiling is resolved by spawning the real `coordinator_core.invoke
        --dump-op-timeouts`, and the engine budget is read from the same module the
        engine dispatches under. Skips (never fails) where the engine is not importable
        from this checkout, so the suite stays green off the claude-klabauter tree.
        """
        try:
            from coordinator_core.ipc import _timeout_for
        except Exception as exc:  # ImportError or engine-load failure off-tree
            self.skipTest(f"coordinator_core.ipc not importable here: {exc!r}")

        # claude-klabauter repo root (contains coordinator_core/) — the CLAUDE_KLABAUTER_ROOT the real
        # --dump-op-timeouts spawn resolves the engine from.
        claude_klabauter_root = str(_COORDINATOR_ROOT.parent)
        if not (Path(claude_klabauter_root) / "coordinator_core" / "invoke" / "__main__.py").exists():
            self.skipTest(f"coordinator_core.invoke not present under {claude_klabauter_root!r}")

        engine_budget = _timeout_for("session.boot_sweep")

        env = _mod._build_subprocess_env(claude_klabauter_root)
        _mod._reset_op_timeout_cache()
        ceiling = _mod._op_timeout_ceiling("session.boot_sweep", claude_klabauter_root, env)

        self.assertGreaterEqual(
            ceiling, engine_budget,
            f"INVARIANT DRIFT: facade ceiling ({ceiling}s) is tighter than the engine "
            f"budget ({engine_budget}s) for session.boot_sweep — the two clocks drifted apart",
        )


class TestChildEnvLeakGuard(unittest.TestCase):
    """Regression for the COORDINATOR_CORE_LAZY_OPS leak, in both of its fixes.

    cc_invoke.py needs lazy op registration armed before its 135 in-process
    trampolines do `from coordinator_core.ops.<name> import main`. Until
    2026-07-28 it armed that by writing the flag into `os.environ` at import
    time, which every child spawned without an explicit `env=` then inherited —
    silently making that child's own `import coordinator_core.ops` skip eager
    registration, and failing collection outright for the 59 test modules that
    assert the registry at import time (commit 5943ec01 patched one such spawn
    site by hand; `child_env()` generalised the strip).

    2026-07-28 moved the in-process channel to `sys._coordinator_core_lazy_ops`
    (scoping study `docs/research/2026-07-28-lazy-ops-import-side-effect-scope.md`
    § 6 (c)): a `sys` attribute is inherited by nothing, so the leak is
    structurally impossible rather than stripped per spawn site. `os.environ` is
    no longer written at all, and the environment variable survives purely as the
    OPERATOR override, read from outside the process.

    `child_env()` is kept as belt-and-braces — an operator can still legitimately
    export the variable — so its own three properties are asserted below
    alongside the two that pin the new channel.
    """

    @staticmethod
    def _clean_parent_env() -> dict:
        """A copy of this TEST process's own env with COORDINATOR_CORE_LAZY_OPS
        removed. Spawning a "fresh trampoline" child under an ambient value —
        an operator's export, or a peer test's leftover — would make that child
        think an operator set the flag, and would mask the very mutation the
        first case here asserts is absent. Stripping isolates the scenario under
        test: a genuinely fresh process importing cc_invoke.py for the first
        time, with no operator override in play."""
        env = os.environ.copy()
        env.pop("COORDINATOR_CORE_LAZY_OPS", None)
        return env

    def test_import_does_not_mutate_os_environ(self) -> None:
        """Importing cc_invoke.py must leave os.environ untouched.

        This is the whole fix: what os.environ never gains, no child can
        inherit — via subprocess, os.execv, or any other path, patched or not."""
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); import cc_invoke; "
             "import os; print(os.environ.get('COORDINATOR_CORE_LAZY_OPS'))"],
            capture_output=True, text=True, timeout=15, env=self._clean_parent_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.stdout.strip(), "None", proc.stderr)

    def test_in_process_trampoline_still_gets_lazy_behaviour(self) -> None:
        """A trampoline that imports cc_invoke.py and then coordinator_core.ops
        must still SKIP eager op registration — the ~108ms/invocation the flag
        exists for. Asserted as the observable behaviour (an empty registry),
        not as the value of whichever channel currently carries it."""
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); "
             f"sys.path.insert(0, {str(_REPO_ROOT)!r}); import cc_invoke; "
             "import coordinator_core.ops; import coordinator_core.ipc as ipc; "
             "print(len(ipc._REGISTRY))"],
            capture_output=True, text=True, timeout=60, env=self._clean_parent_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.stdout.strip(), "0", proc.stderr)

    def test_plain_spawn_after_import_does_not_leak_the_flag(self) -> None:
        """A child spawned with NO explicit `env=` after the parent imported
        cc_invoke.py must not see the flag. This is the leak itself, asserted
        without routing through child_env() — the spawn site's cooperation is
        exactly what used to be required and is no longer."""
        script = (
            f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); import cc_invoke; "
            "import subprocess; "
            "r = subprocess.run([sys.executable, '-c', "
            "'import os; print(os.environ.get(\"COORDINATOR_CORE_LAZY_OPS\"))'], "
            "capture_output=True, text=True); "
            "print(r.stdout.strip())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=15, env=self._clean_parent_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.stdout.strip(), "None", proc.stderr)

    def test_spawned_child_does_not_inherit_lazy_ops_via_child_env(self) -> None:
        """A grandchild spawned with env=cc_invoke.child_env() must NOT see
        COORDINATOR_CORE_LAZY_OPS, even though the parent process (which imported
        cc_invoke.py) has it set in its own os.environ."""
        script = (
            f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); import cc_invoke; "
            "import subprocess; "
            "r = subprocess.run([sys.executable, '-c', "
            "'import os; print(os.environ.get(\"COORDINATOR_CORE_LAZY_OPS\"))'], "
            "env=cc_invoke.child_env(), capture_output=True, text=True); "
            "print(r.stdout.strip())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=15, env=self._clean_parent_env(),
        )
        self.assertEqual(proc.stdout.strip(), "None", proc.stderr)

    def test_operator_explicit_setting_is_never_stripped(self) -> None:
        """An operator who set COORDINATOR_CORE_LAZY_OPS themselves (before
        cc_invoke.py is ever imported) keeps their own value in every child —
        child_env() must only strip the value THIS module injected via
        setdefault, never an operator's explicit choice."""
        env = os.environ.copy()
        env["COORDINATOR_CORE_LAZY_OPS"] = "0"
        script = (
            f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); import cc_invoke; "
            "import subprocess; "
            "r = subprocess.run([sys.executable, '-c', "
            "'import os; print(os.environ.get(\"COORDINATOR_CORE_LAZY_OPS\"))'], "
            "env=cc_invoke.child_env(), capture_output=True, text=True); "
            "print(r.stdout.strip())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=15, env=env,
        )
        self.assertEqual(proc.stdout.strip(), "0", proc.stderr)


class TestRepoFlagScopeGate(unittest.TestCase):
    """Regression: commit bd0e52a36154 (DR-279) made coordinator_core.invoke exit
    non-zero when --repo is passed to a "none"-scoped op — but cc_invoke.py passed
    --repo unconditionally on every op, so every none-scoped op invoked through this
    wrapper died. Covers both spawn call sites (cc_invoke and cc_invoke_bare), plus
    the import-failure fallback.
    Spec backlink: docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md
    """

    def _mock_proc(self, result: dict[str, Any]) -> unittest.mock.Mock:
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
        mock_proc.stderr = ""
        return mock_proc

    def test_cc_invoke_omits_repo_for_none_scoped_op(self) -> None:
        """cc_invoke() must NOT spawn --repo for a "none"-scoped op (e.g. memo.list)."""
        mock_proc = self._mock_proc({})
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.append(args[0])
            return mock_proc

        with unittest.mock.patch("subprocess.run", side_effect=_capture):
            _mod.cc_invoke("memo.list", {}, "/the/repo", _claude_klabauter_root="/fake/mr")

        cmd = captured[0]
        self.assertNotIn("--repo", cmd)

    def test_cc_invoke_keeps_repo_for_repo_scoped_op(self) -> None:
        """cc_invoke() must still spawn --repo for a repo-scoped op (e.g. queue.append)."""
        mock_proc = self._mock_proc({})
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.append(args[0])
            return mock_proc

        with unittest.mock.patch("subprocess.run", side_effect=_capture):
            _mod.cc_invoke("queue.append", {}, "/the/repo", _claude_klabauter_root="/fake/mr")

        cmd = captured[0]
        self.assertIn("--repo", cmd)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "/the/repo")

    def test_cc_invoke_bare_omits_repo_for_none_scoped_op(self) -> None:
        """cc_invoke_bare() must NOT spawn --repo for a "none"-scoped op."""
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({})
        mock_proc.stderr = ""
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.append(args[0])
            return mock_proc

        with unittest.mock.patch("subprocess.run", side_effect=_capture):
            _mod.cc_invoke_bare("memo.list", {}, "/the/repo", _claude_klabauter_root="/fake/mr")

        cmd = captured[0]
        self.assertNotIn("--repo", cmd)

    def test_cc_invoke_bare_keeps_repo_for_repo_scoped_op(self) -> None:
        """cc_invoke_bare() must still spawn --repo for a repo-scoped op."""
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({})
        mock_proc.stderr = ""
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.append(args[0])
            return mock_proc

        with unittest.mock.patch("subprocess.run", side_effect=_capture):
            _mod.cc_invoke_bare("queue.append", {}, "/the/repo", _claude_klabauter_root="/fake/mr")

        cmd = captured[0]
        self.assertIn("--repo", cmd)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "/the/repo")

    def test_should_pass_repo_fails_open_on_import_failure(self) -> None:
        """If the OP_KEY_SCOPE table cannot be imported, fall back to today's
        behaviour (pass --repo) rather than crashing the transport."""
        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            self.assertTrue(_mod._should_pass_repo("memo.list"))
