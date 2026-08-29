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

DECLARED SPAWN COUNT: this module makes exactly THREE real engine spawns, all
in `TestDiagnosticsProbesEndToEnd` (one per `diagnostics.*` probe op), all
function-level, none at import time. Every other test here mocks
`subprocess.run` or calls a pure function in-process. The count is stated for a
spawn census to check against rather than infer; if a fourth is ever added, this
number is the thing that must change with it. The three are irreducible: they
are the only end-to-end evidence that `cc_invoke`'s failure ladder classifies a
REAL engine child correctly, which no in-process case can supply.

Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C1 / AC1 / AC9
Spec backlink: pln-a-safe-target-for-transport-fa-7ea067 § C2 / AC3 / AC6
"""
from __future__ import annotations

import contextlib
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

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: `TestLazyOpsEnvironmentLeak` spawns real `sys.executable`
# child (and grandchild) processes because the property under test is os.environ
# leakage/isolation ACROSS a real process boundary -- an in-process call shares the
# parent's os.environ by construction and cannot observe what a genuinely separate
# interpreter inherits. `TestDiagnosticsProbesEndToEnd` (see the module docstring's
# DECLARED SPAWN COUNT) separately spawns the real engine for the same
# no-mock-stands-in reason. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

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

#: Independent second copies of the two client-ceiling constants. Deliberately literals,
#: not imports of `_mod._CLIENT_START_MARGIN_SECS` / `_mod._NO_BUDGET_FALLBACK_SECS` --
#: importing them would make every assertion below agree with any value whatsoever.
#: Lowering either constant is fine (a client wait ratchets down); raising one fails here,
#: which is the whole point after the 2026-08-21 PM ruling that no EM may widen a dial.
_PINNED_CLIENT_MARGIN_SECS = 2
_PINNED_NO_BUDGET_FALLBACK_SECS = 10

#: The two dials retired on 2026-08-21. Cleared before every test below so a developer's
#: ambient shell export cannot alter a result — the vars are inert in production, and a
#: test that happens to pass only because one was unset would hide a regression.
_RETIRED_TIMEOUT_DIALS = ("CC_INVOKE_TIMEOUT_SECS", "CC_INVOKE_CLIENT_MARGIN_SECS")


@pytest.fixture(autouse=True)
def _isolate_op_timeout_state(monkeypatch):
    """Restore cc_invoke's op-budget memoization to its cold state around every test.

    `_OP_TIMEOUTS_STATE` / `_OP_TIMEOUTS_MAP` / `_OP_TIMEOUTS_BREADCRUMB_SHOWN` are
    resolved AT MOST ONCE per process by design — a facade process is short-lived, so the
    engine's `--dump-op-timeouts` probe is paid once and cached. Under a test runner that
    lifetime is the whole session, which makes every one of those globals a channel
    between unrelated tests: whether the probe spawns at all, and therefore what sits at
    index 0 of a captured-spawn list, depends on which test ran first.

    `_reset_op_timeout_cache` is the seam for exactly this, but six opportunistic `setUp`
    calls only cover the classes that remembered. Doing it here makes the whole class of
    ordering failure impossible rather than patching call sites, and it is what the
    module's own once-per-process memoization implies for a runner that reuses the
    process. Resetting on the way out as well contains a test that resolves state without
    patching the globals (a `patch.object` on `_OP_TIMEOUTS_STATE` restores that name and
    leaves the breadcrumb flag set).

    Applies to `unittest.TestCase` methods too: an autouse fixture runs around them, and
    ahead of `setUp`, so a class that sets these vars itself still wins.
    """
    for dial in _RETIRED_TIMEOUT_DIALS:
        monkeypatch.delenv(dial, raising=False)
    _mod._reset_op_timeout_cache()
    yield
    _mod._reset_op_timeout_cache()


_DUMP_PROBE_FLAG = "--dump-op-timeouts"


def _is_dump_probe(argv) -> bool:
    """True for the once-per-process engine op-budget probe spawn.

    Same predicate `_bare_run_dispatcher` already branches on — kept as one named
    function so the two cannot drift apart on what counts as the probe.
    """
    return _DUMP_PROBE_FLAG in argv


def _argv_of(entry):
    """Normalise the two capture shapes in this module to a plain argv list.

    A callback may record the bare `cmd` (`args[0]`) or the whole `(args, kwargs)`
    tuple; both are common here and neither is worth rewriting at every call site.
    """
    if isinstance(entry, tuple) and entry and isinstance(entry[0], tuple):
        return entry[0][0]
    return entry


def _invoke_argv(captured):
    """The argv of the ONE `coordinator_core.invoke` OP spawn in `captured`.

    NEVER index a captured-spawn list positionally. `_op_timeout_ceiling` resolves the
    engine's op-budget map at most once per process, so whether the `--dump-op-timeouts`
    probe spawns inside any given test depends on whether an earlier test in the run
    already populated the cache. `captured[0]` used to be the op spawn by luck of
    ordering — a cross-test dependency masquerading as a passing assertion. Once the
    autouse reset above made isolation real, every test pays the probe first and every
    positional index shifts by one.

    Selecting by predicate is correct under either ordering, and makes the next change to
    spawn order a non-event rather than a suite-wide breakage.
    """
    matches = [
        argv
        for argv in (_argv_of(entry) for entry in captured)
        if "coordinator_core.invoke" in argv and not _is_dump_probe(argv)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one coordinator_core.invoke op spawn, got {len(matches)}; "
            f"captured argvs: {[_argv_of(e) for e in captured]}"
        )
    return matches[0]


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
        self.assertIn("COORDINATOR_ENGINE_ROOT environment variable", msg)
        self.assertIn(".claude-klabauter-live-root pointer file", msg)
        self.assertIn("repos.claude_klabauter", msg)
        self.assertIn("git clone https://github.com/dbc-oduffy/claude-klabauter", msg)
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

        call_args = _invoke_argv(captured_calls)  # the op spawn, selected by predicate
        # Review: cross-slice (DR-148) — cc_invoke.py now uses sys.executable, not "python3".
        self.assertEqual(call_args[0], sys.executable)
        self.assertEqual(call_args[1], "-m")
        self.assertEqual(call_args[2], "coordinator_core.invoke")
        self.assertEqual(call_args[3], "queue.append")
        # Params ride --params-file, NOT a positional argv arg — ARG_MAX-immune
        # (see cc_invoke's own docstring's Params transport note). No positional
        # params_json ever appears on argv for this call convention.
        self.assertIn("--params-file", call_args)
        self.assertNotIn("queue.append", call_args[4:])  # op itself only appears once, at index 3
        self.assertIn("--repo", call_args)
        repo_idx = call_args.index("--repo")
        self.assertEqual(call_args[repo_idx + 1], "/the/repo")

    def test_params_serialised_as_json(self) -> None:
        """State-2: params dict is serialised to compact JSON and written to --params-file."""
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

        cmd = _invoke_argv(captured_calls)
        self.assertIn("--params-file", cmd)

    def test_params_file_content_readable_during_spawn(self) -> None:
        """State-2: --params-file's content is the exact compact-JSON params, readable
        at the moment subprocess.run is invoked (before cc_invoke's finally unlinks it)."""
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_params: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                # The op-budget probe carries no --params-file; reading one off it
                # would raise before the op spawn under test ever arrived.
                return _dump_absent_proc()
            params_path = cmd[cmd.index("--params-file") + 1]
            with open(params_path, "r", encoding="utf-8") as f:
                captured_params.append(json.load(f))
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
        ):
            _mod.cc_invoke("op", {"foo": "bar", "n": 42}, "/repo")

        self.assertEqual(captured_params, [{"foo": "bar", "n": 42}])

    def test_large_params_argv_stays_bounded_and_roundtrips(self) -> None:
        """A several-thousand-path params payload never lands on argv (bounded argv
        length) and round-trips byte-equal via --params-file — the shape this facade
        exists to guarantee once Windows CreateProcess's 32767-char argv cap is in play."""
        paths = [f"some/repo/relative/path/file_{i:05d}.py" for i in range(4000)]
        big_params = {"paths": paths, "op_field": "value"}
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        captured_calls: list[Any] = []
        captured_params: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                return _dump_absent_proc()
            captured_calls.append(cmd)
            params_path = cmd[cmd.index("--params-file") + 1]
            with open(params_path, "r", encoding="utf-8") as f:
                captured_params.append(json.load(f))
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
        ):
            _mod.cc_invoke("fleet.publish", big_params, "/repo")

        cmd = _invoke_argv(captured_calls)
        argv_total_len = sum(len(str(a)) for a in cmd)
        # Well under the Windows 32767 CreateProcess cap — the whole point is the
        # payload (hundreds of KB of paths) never rides argv at all.
        self.assertLess(argv_total_len, 2000)
        for arg in cmd:
            self.assertNotIn("file_00000.py", arg)  # no path fragment ever lands on argv
        self.assertEqual(captured_params, [big_params])  # byte-equal round-trip via the file

    def test_params_file_cleaned_up(self) -> None:
        """The --params-file temp file is unlinked after cc_invoke returns (no scratch leak)."""
        success_envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_proc = unittest.mock.Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = success_envelope
        mock_proc.stderr = ""

        seen_paths: list[str] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if _is_dump_probe(cmd):
                return _dump_absent_proc()
            pf = cmd[cmd.index("--params-file") + 1]
            seen_paths.append(pf)
            self.assertTrue(os.path.exists(pf), "params file must exist during the spawn")
            return mock_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_run),
        ):
            _mod.cc_invoke("op", {"a": 1}, "/repo")

        self.assertEqual(len(seen_paths), 1)
        self.assertFalse(
            os.path.exists(seen_paths[0]),
            "params file must be unlinked after cc_invoke returns",
        )

    def test_claude_klabauter_root_in_subprocess_env(self) -> None:
        """State-2: subprocess env carries the ENGINE-ROOT name and PYTHONPATH.

        INVERTED 2026-08-20, and left red until then. C14 stopped
        `_build_subprocess_env` exporting the retired `CLAUDE_KLABAUTER_ROOT`, and the
        same session inverted the other window-open tests but missed this one,
        so it sat asserting an export C14 had deliberately removed. Kept rather
        than deleted, on the same reasoning as the bootstrap-carve-out tripwire:
        a test that pins WHICH name reaches a child is exactly the test that
        catches the retired name creeping back into a child environment.
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

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run", side_effect=_capture),
        ):
            _mod.cc_invoke("op", {}, "/repo")

        env = captured_envs[0]
        self.assertEqual(env.get("COORDINATOR_ENGINE_ROOT"), "/fake/mr")
        self.assertIsNone(
            env.get("CLAUDE_KLABAUTER_ROOT"),
            "cc_invoke exported the retired name into a child environment; "
            "C14 removed that export and this pin is what keeps it removed",
        )
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

    def test_unserialisable_params_raises_runtime_error_not_type_error(self) -> None:
        """A17: json.dumps(params) on an unserialisable value must not escape the
        module's documented "raises RuntimeError on ANY transport failure" contract
        as a bare TypeError — no spawn should even be attempted."""
        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch("subprocess.run") as mock_run,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke("op", {"bad": object()}, "/repo")
        self.assertNotIsInstance(ctx.exception, TypeError)
        mock_run.assert_not_called()

    def test_fdopen_failure_closes_params_fd(self) -> None:
        """A16: os.fdopen(_params_fd, ...) raising must not leak the mkstemp fd —
        the finally block unlinks the path but previously never closed the descriptor.

        Two ambient-environment sources of `os.close` calls are pinned out so this
        stays hermetic rather than machine-dependent:

        1. `tempfile.gettempdir()` is pre-warmed before `os.close` is patched: on a
           cold cache, CPython's own `tempfile._get_default_tempdir()` probes
           candidate temp dirs and calls `os.close` itself as part of that one-time,
           per-process self-test.
        2. `_capture_warm_reach` is forced to a deterministic miss. Left ambient, on
           a machine where warm dispatch is enabled it attempts a real pipe connect
           whose cleanup calls `os.close` on an unrelated fd (observed: this
           coincidentally reused the same low fd number freed by the warm-reach
           cleanup, inflating the count to 2) — same failure shape either source
           produces alone: a real, unrelated close counted as if it were the
           leak-fix's own close.

        Neither source is anywhere near `_should_pass_repo` (never reached: the
        fdopen exception fires before `cc_invoke` gets to the `_should_pass_repo`
        call), so this is not a fd-handling regression in the leak-fix path."""
        import tempfile as _tempfile

        _tempfile.gettempdir()

        closed_fds: list[int] = []
        real_close = os.close

        def _close_spy(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        def _boom_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
            # Simulate fdopen failing before it takes ownership of the fd.
            raise OSError("simulated fdopen failure")

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return _dump_absent_proc()
            raise AssertionError("op spawn must not be reached — fdopen fails first")

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch.object(_mod, "_seam_present", return_value=True),
            unittest.mock.patch.object(_mod, "_capture_warm_reach", return_value=(None, "")),
            unittest.mock.patch("subprocess.run", side_effect=_run),
            unittest.mock.patch("os.fdopen", side_effect=_boom_fdopen),
            unittest.mock.patch("os.close", side_effect=_close_spy),
        ):
            with self.assertRaises(OSError):
                _mod.cc_invoke("op", {"a": 1}, "/repo")

        self.assertEqual(len(closed_fds), 1, "fdopen failure must close the leaked fd exactly once")


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

    def test_exit_code_noncastable_refuses(self) -> None:
        """A15: a non-castable exit_code (e.g. "fail") must NOT coerce to 0-success —
        the retired bash oracle's int()/except -> 0 fallback was bug-compatibility,
        deliberately dropped. route_mutation must treat it as a refusal, mirroring
        the benign "0" case pinned by test_exit_code_string_zero_does_not_false_positive.
        """
        envelope = {"exit_code": "fail", "acted": ["a"]}

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
# The retired client dials — CC_INVOKE_TIMEOUT_SECS / CC_INVOKE_CLIENT_MARGIN_SECS
# ---------------------------------------------------------------------------

class TestRetiredTimeoutEnvKnobsAreInert(unittest.TestCase):
    """PM ruling 2026-08-21: no EM, in this repo or a sibling, may raise a timeout dial.

    This class replaces `TestTimeoutEnvGuard`, which pinned the parse-and-warn behaviour
    of `_read_positive_int_env` for `CC_INVOKE_TIMEOUT_SECS`. That reader existed only to
    serve the two client dials and is deleted along with them: the FLOOR was run at 460
    from a sibling repo and produced a 460s client wait on a shared box, and the MARGIN
    was guarded by nothing at all. A malformed value can no longer break the transport for
    the stronger reason that no value is read — so the tests here assert INERTNESS across
    the same value shapes the old parse guard covered (garbage, zero, negative, and a
    perfectly valid number), rather than a defaulting warning.
    """

    def setUp(self) -> None:
        # The per-op ceiling memoizes the --dump-op-timeouts probe in module globals.
        # Reset per-test so the dump-probe path is exercised deterministically.
        _mod._reset_op_timeout_cache()

    def _op_timeout_with_env(self, env: dict) -> int:
        """Return the `timeout=` cc_invoke passed to the op-call spawn under `env`.

        The dump surface is ABSENT, so the ceiling takes the no-budget fallback — the
        branch the retired FLOOR used to own outright.
        """
        op_proc = unittest.mock.Mock()
        op_proc.returncode = 0
        op_proc.stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        op_proc.stderr = ""

        op_call_timeouts: list[int] = []

        def _run(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0]
            if "--dump-op-timeouts" in cmd:
                return _dump_absent_proc()
            op_call_timeouts.append(kwargs.get("timeout"))
            return op_proc

        with (
            unittest.mock.patch.object(_mod, "_resolve_claude_klabauter_root", return_value="/fake/mr"),
            unittest.mock.patch("subprocess.run", side_effect=_run),
            unittest.mock.patch.dict(os.environ, env, clear=False),
        ):
            _mod.cc_invoke("op", {}, "/repo")

        self.assertEqual(len(op_call_timeouts), 1, "exactly one op-call spawn expected")
        return op_call_timeouts[0]

    def test_huge_knobs_do_not_raise_the_op_call_timeout(self) -> None:
        """The reproduced incident, as a test: a 460s floor buys a 460s wait no longer."""
        self.assertEqual(
            self._op_timeout_with_env(
                {"CC_INVOKE_TIMEOUT_SECS": "460", "CC_INVOKE_CLIENT_MARGIN_SECS": "9999"}
            ),
            _PINNED_NO_BUDGET_FALLBACK_SECS,
        )

    def test_valid_knob_value_is_equally_inert(self) -> None:
        """A well-formed 30 is not a special case — it is simply not read."""
        self.assertEqual(
            self._op_timeout_with_env({"CC_INVOKE_TIMEOUT_SECS": "30"}),
            _PINNED_NO_BUDGET_FALLBACK_SECS,
        )

    def test_malformed_knob_values_neither_raise_nor_warn(self) -> None:
        """Garbage, zero, and a negative all reach the same number, silently.

        The old reader emitted a `warn: cc_invoke: invalid ...` line for each of these.
        A warning about a variable nothing reads is noise that also implies the variable
        matters, so its absence is asserted, not merely tolerated.
        """
        import io

        for raw in ("notanumber", "0", "-5"):
            with self.subTest(raw=raw):
                _mod._reset_op_timeout_cache()
                cap_err = io.StringIO()
                with unittest.mock.patch("sys.stderr", cap_err):
                    timeout = self._op_timeout_with_env({"CC_INVOKE_TIMEOUT_SECS": raw})
                self.assertEqual(timeout, _PINNED_NO_BUDGET_FALLBACK_SECS)
                self.assertNotIn("warn", cap_err.getvalue().lower())


# ---------------------------------------------------------------------------
# AC9 — grep-gate: cc_invoke.py must not contain retired transport patterns
# ---------------------------------------------------------------------------

class TestGrepGate(unittest.TestCase):
    """AC9: cc_invoke.py contains no retired transport patterns.

    The grep-gate checks for CODE-level usage patterns (actual imports, socket opens,
    token reads, daemon-aware state machine), not bare string mentions that may appear
    in documentation/comments. Patterns are chosen to catch actual code misuse.

    Scope, reaffirmed (2026-08-18, plan C19): a Windows named-pipe transport for
    coordinator_core (C14/C15 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md)
    is a live addition, not a hypothetical one. DR-315 §3.1/§5 is explicit that this
    pipe transport is a *different shape* than what DR-215 retired — UDS (socket.AF_UNIX)
    and HTTP with auth tokens — and does not trip any of the four patterns below. Do NOT
    widen these checks to forbid a pipe/warm/daemon surface generally: that would pin the
    absence of C14/C15's authorized addition, not the absence of the DR-215-retired shape
    this gate exists to keep out. Each pattern below is DR-215/DR-315-scoped by name; if a
    future change needs a fifth pattern, name the DR clause it enforces, the same way.
    Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C19.
    """

    def _source_text(self) -> str:
        return _CC_INVOKE_PY.read_text(encoding="utf-8")

    def test_no_coordinator_core_client(self) -> None:
        """No coordinator_core.client import — the client-seam is DR-215-retired.

        Checks for the actual import forms: 'from coordinator_core.client' and
        'from coordinator_core import client'. Bare string 'coordinator_core.client'
        may appear in negative-spec comments; the import forms cannot. DR-315 §3.1
        confirms the named-pipe transport does not reinstate this seam — it is a
        different client shape entirely, not a caller of coordinator_core.client.
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
        DR-315 §5 confirms this stays forbidden even after C14/C15 land: "no AF_UNIX
        socket file exists on Windows to leak or sweep" — the added transport is a
        named pipe, a different OS primitive from a different stdlib module, and
        never opens a socket.AF_UNIX. This pin does not need to (and must not) widen
        to forbid the pipe transport itself.
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
        form 'AUTH_TOKEN'. These patterns catch actual reads, not doc mentions. DR-315
        §3.2 rules explicitly that "the two-tier token matrix and Invariant 3 stay
        vacated" under the warm engine — the restricted-DACL pipe is the mitigation
        for the new same-machine wire, not a per-partition token matrix. This pin
        stays as-is; the pipe transport carries no auth_token/AUTH_TOKEN surface.
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
        """No three-state router — cc_invoke.py's own route() stays two-state only.

        Checks for 'State-3' literal (a three-state label) and 'State3' (camelCase form).
        DR-315 §3.3 amends the shape of this claim, not its truth: cc_invoke.py's
        route() is unaffected — State-1 (seam-absent) and State-2 (seam-present) are
        unchanged, and no third router state is added here. Warmth is a property of
        what coordinator_core.invoke's own process does once seam-present dispatch
        reaches it (a pipe-first, spawn-on-FileNotFoundError decision inside the
        engine's entry path, C15) — not a state cc_invoke.py's router discriminates
        on. This pin is therefore still correct unmodified: the router genuinely
        stays two-state even though the engine process it dispatches to gains warmth.
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
        # Dump absent (the dispatcher's default) -> the no-budget fallback is the wait,
        # and no env value narrows or widens it.
        with self._mr(), unittest.mock.patch(
            "subprocess.run",
            side_effect=_bare_run_dispatcher(
                op_proc=None,
                op_side_effect=subprocess.TimeoutExpired(["python"], timeout=1),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.cc_invoke_bare("op", {}, "/repo")
        self.assertIn("timeout", str(ctx.exception))
        self.assertIn(f"after {_PINNED_NO_BUDGET_FALLBACK_SECS}s", str(ctx.exception))

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
    (budget + `_CLIENT_START_MARGIN_SECS`), pinning the arithmetic rather than only
    pass/fail. Every case sets both retired env dials absurdly high, so each assertion
    doubles as proof the environment cannot reach the number (PM ruling 2026-08-21).
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

    #: Both retired dials, set far above anything the derivation can produce.
    _RETIRED_KNOBS = {
        "CC_INVOKE_TIMEOUT_SECS": "460",
        "CC_INVOKE_CLIENT_MARGIN_SECS": "9999",
    }

    def test_override_op_resolves_budget_plus_margin(self) -> None:
        """AC1: op WITH an override -> _t = budget + MARGIN = 2 + 2 = 4.

        The probe runs unconditionally (once per process) -- no env-var gate needed.
        """
        t, _err = self._run_capture_timeout(
            "custom.op",
            self._dump_ok({"custom.op": 2, "__default__": 5}),
            dict(self._RETIRED_KNOBS),
        )
        self.assertEqual(
            t,
            2 + _PINNED_CLIENT_MARGIN_SECS,
            "override op must resolve budget+margin=4 from the engine's own map, and "
            "must not see the 460s dial set alongside it",
        )

    def test_override_less_op_resolves_default_plus_margin(self) -> None:
        """AC2: op WITHOUT an override -> map['__default__'] + MARGIN = 2 + 2 = 4."""
        t, _err = self._run_capture_timeout(
            "other.op",
            self._dump_ok({"custom.op": 99, "__default__": 2}),
            dict(self._RETIRED_KNOBS),
        )
        self.assertEqual(
            t,
            2 + _PINNED_CLIENT_MARGIN_SECS,
            "override-less op must resolve via __default__, not via any env dial",
        )

    def test_dump_absent_falls_back_silently(self) -> None:
        """AC3(a): dump surface absent (older claude-klabauter) -> no-budget fallback, no breadcrumb."""
        t, err = self._run_capture_timeout(
            "other.op", _dump_absent_proc(), dict(self._RETIRED_KNOBS)
        )
        self.assertEqual(t, _PINNED_NO_BUDGET_FALLBACK_SECS)
        self.assertNotIn("op-budget dump failed", err, "dump-absent must be silent")

    def test_dump_errored_falls_back_with_breadcrumb(self) -> None:
        """AC3(b): dump present but malformed (missing __default__) -> fallback + breadcrumb."""
        dump = self._dump_ok({"custom.op": 5})  # missing "__default__" -> error state
        t, err = self._run_capture_timeout("other.op", dump, dict(self._RETIRED_KNOBS))
        self.assertEqual(t, _PINNED_NO_BUDGET_FALLBACK_SECS)
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
            os.environ, dict(self._RETIRED_KNOBS), clear=False
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
    cc_invoke() (the NON-bare path), which once used a flat 10s client floor — strictly
    tighter than the engine's 30s. On a loaded machine the facade fired `cc_invoke: engine
    timeout after 10s (op=session.boot_sweep)` and killed the sweep well before the
    engine's own clock. These tests pin the invariant that the two clocks cannot silently
    drift back apart — now with the client clock derived from the engine's alone.
    """

    def setUp(self) -> None:
        _mod._reset_op_timeout_cache()

    def test_boot_sweep_facade_ceiling_ge_engine_budget_mocked(self) -> None:
        """cc_invoke() resolves _t = budget + MARGIN for a composite op.

        Dump reports the engine budget (30) for session.boot_sweep (via __default__,
        since boot_sweep carries no explicit override today). MARGIN=2 -> _t = 32 >= 30.
        The op-call spawn (not the dump probe) carries it. Both retired dials are set
        absurdly high and must not appear in the result.
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
            {"CC_INVOKE_TIMEOUT_SECS": "460", "CC_INVOKE_CLIENT_MARGIN_SECS": "9999"},
            clear=False,
        ):
            _mod.cc_invoke("session.boot_sweep", {}, "/repo")

        self.assertEqual(len(op_call_timeouts), 1, "exactly one op-call spawn expected")
        self.assertEqual(
            op_call_timeouts[0], engine_budget + _PINNED_CLIENT_MARGIN_SECS,
            "cc_invoke() must resolve budget+margin=30+2=32 — neither the flat 10s floor "
            "that strangled boot_sweep, nor anything the two retired dials name",
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
    """Regression for `child_env()`'s actual current contract, post-retirement
    of the COORDINATOR_CORE_LAZY_OPS leak-defence apparatus.

    Until 2026-07-28, cc_invoke.py armed lazy op registration by writing the
    flag into `os.environ` at import time, which every child spawned without
    an explicit `env=` then inherited — silently making that child's own
    `import coordinator_core.ops` skip eager registration, and failing
    collection outright for the 59 test modules that assert the registry at
    import time (commit 5943ec01 patched one such spawn site by hand;
    `child_env()` generalised the strip). 2026-07-28 moved the in-process
    channel to `sys._coordinator_core_lazy_ops`, closing the leak
    structurally. As of the `import-path-costs-nothing` sprint (C6), lazy op
    registration is unconditional — nothing reads either channel any more —
    and nothing in this tree writes `COORDINATOR_CORE_LAZY_OPS` into
    `os.environ` at all, so `child_env()` no longer has a leak to guard
    against and no longer strips anything (see its own docstring).

    What remains worth pinning: importing cc_invoke.py is still a pure,
    side-effect-free operation on `os.environ` (regression floor for any
    future arming attempt), and `child_env()` still passes an operator's own
    env values through unmodified — it is a settings-home-propagating
    passthrough, not a scrub, and must never start stripping a caller-set
    value again.
    """

    def test_import_does_not_mutate_os_environ(self) -> None:
        """Importing cc_invoke.py must leave os.environ untouched.

        Nothing in this module writes to `os.environ` at import time, for
        COORDINATOR_CORE_LAZY_OPS or anything else — asserted here as the
        regression floor for any future arming attempt."""
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); "
             "import os; before = dict(os.environ); import cc_invoke; "
             "print(dict(os.environ) == before)"],
            capture_output=True, text=True, timeout=15, env=os.environ.copy(),
            **no_console_creationflags(),
        )
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)

    def test_child_env_passes_through_operator_value_unmodified(self) -> None:
        """`child_env()` is a passthrough, not a scrub: a value the caller's
        own os.environ already carries reaches the child unchanged. Uses
        COORDINATOR_CORE_LAZY_OPS as the probe variable purely as the
        historical regression anchor for this exact leak-guard class — the
        assertion is about `child_env()`'s general non-stripping contract,
        not about that variable's own (now-inert) meaning."""
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


class TestSpawnSeamSettingsHomePropagation(unittest.TestCase):
    """AC11 (pln-the-machine-local-registry-rea-50be37 § C5): pins the actual
    spawn seam -- `child_env()` / `_build_subprocess_env()` -- not just the leaf
    `coordinator_core._settings_home.settings_home_child_env()` they delegate
    to via `_settings_home_env()`. `test_settings_home_child_env_inheritance.py`
    proves the leaf function works; nothing there proves this module's two
    call sites actually invoke it. Reverting either call site (back to a plain
    `dict(os.environ)` / `{**os.environ, ...}`) must fail every test below.
    """

    def setUp(self) -> None:
        self._env_patch = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop("COORDINATOR_SETTINGS_HOME", None)

    def test_child_env_propagates_resolved_settings_home(self) -> None:
        """child_env() must fill COORDINATOR_SETTINGS_HOME from the resolved
        settings-home root when the parent's own os.environ lacks it."""
        claude_home = tempfile.mkdtemp()
        os.environ["CLAUDE_HOME"] = claude_home
        expected = str(Path(claude_home) / ".coordinator-claude-settings")

        self.assertNotIn(
            "COORDINATOR_SETTINGS_HOME", os.environ,
            "test precondition: parent os.environ must not already carry the var",
        )

        result = _mod.child_env()

        self.assertEqual(
            result.get("COORDINATOR_SETTINGS_HOME"), expected,
            "child_env() did not propagate the resolved settings-home root -- "
            "the seam's _settings_home_env() call may have been reverted",
        )

    def test_build_subprocess_env_propagates_resolved_settings_home(self) -> None:
        """_build_subprocess_env() -- the coordinator_core.invoke spawn seam --
        must fill COORDINATOR_SETTINGS_HOME the same way child_env() does."""
        claude_home = tempfile.mkdtemp()
        os.environ["CLAUDE_HOME"] = claude_home
        expected = str(Path(claude_home) / ".coordinator-claude-settings")

        result = _mod._build_subprocess_env("/fake/claude-klabauter/root")

        self.assertEqual(
            result.get("COORDINATOR_SETTINGS_HOME"), expected,
            "_build_subprocess_env() did not propagate the resolved settings-home "
            "root -- the seam's _settings_home_env() call may have been reverted",
        )

    def test_child_env_never_overwrites_an_explicit_child_value(self) -> None:
        """(c) precedence, pinned at the seam level: an operator-scoped
        COORDINATOR_SETTINGS_HOME already in os.environ survives untouched,
        even though CLAUDE_HOME would resolve to a different root."""
        os.environ["CLAUDE_HOME"] = tempfile.mkdtemp()
        operator_scoped = str(Path(tempfile.mkdtemp()) / "operator-scoped")
        os.environ["COORDINATOR_SETTINGS_HOME"] = operator_scoped

        result = _mod.child_env()

        self.assertEqual(result.get("COORDINATOR_SETTINGS_HOME"), operator_scoped)

    def test_real_child_observes_propagated_settings_home_via_child_env(self) -> None:
        """End-to-end: a real child spawned with env=cc_invoke.child_env(),
        from a parent whose os.environ lacks COORDINATOR_SETTINGS_HOME but
        resolves one via CLAUDE_HOME, must observe the resolved value. This is
        the one assertion that would actually catch the propagation call
        sites being removed from cc_invoke.py -- a plain `dict(os.environ)`
        copy would carry no such key across to the child."""
        claude_home = tempfile.mkdtemp()
        expected = str(Path(claude_home) / ".coordinator-claude-settings")
        env = os.environ.copy()
        env.pop("COORDINATOR_SETTINGS_HOME", None)
        env["CLAUDE_HOME"] = claude_home
        script = (
            f"import sys; sys.path.insert(0, {str(_LIB_DIR)!r}); "
            f"sys.path.insert(0, {str(_REPO_ROOT)!r}); import cc_invoke; "
            "import subprocess; "
            "r = subprocess.run([sys.executable, '-c', "
            "'import os; print(os.environ.get(\"COORDINATOR_SETTINGS_HOME\"))'], "
            "env=cc_invoke.child_env(), capture_output=True, text=True); "
            "print(r.stdout.strip())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=15, env=env,
            **no_console_creationflags(),
        )
        self.assertEqual(proc.stdout.strip(), expected, proc.stderr)


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

        cmd = _invoke_argv(captured)
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

        cmd = _invoke_argv(captured)
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

        cmd = _invoke_argv(captured)
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

        cmd = _invoke_argv(captured)
        self.assertIn("--repo", cmd)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "/the/repo")

    def test_should_pass_repo_fails_open_on_import_failure(self) -> None:
        """If the OP_KEY_SCOPE table cannot be imported, fall back to today's
        behaviour (pass --repo) rather than crashing the transport."""
        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            self.assertTrue(_mod._should_pass_repo("memo.list"))

    def test_workflow_scaffold_dispatches_without_repo(self) -> None:
        """workflow.scaffold is a scope="none" op — cc_invoke() must not spawn
        --repo for it (the caller class this chunk closes out)."""
        mock_proc = self._mock_proc({})
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured.append(args[0])
            return mock_proc

        with unittest.mock.patch("subprocess.run", side_effect=_capture):
            _mod.cc_invoke("workflow.scaffold", {}, "/the/repo", _claude_klabauter_root="/fake/mr")

        cmd = _invoke_argv(captured)
        self.assertNotIn("--repo", cmd)


class TestShouldPassRepoFailOpenDiagnostics(unittest.TestCase):
    """AC restated: each TERMINAL fail-open branch in `_should_pass_repo`
    emits exactly one stderr diagnostic per process; the ambient-import
    `except Exception: pass` branch (a normal miss with a working retry
    behind it, not an unresolved scope) emits nothing.
    Spec backlink: state/dispatch-briefs/2026-08-06-orient-assemble-reader-repo-scope/C10.md
    """

    def setUp(self) -> None:
        _mod._SHOULD_PASS_REPO_FAIL_OPEN_EMITTED.clear()

    def tearDown(self) -> None:
        _mod._SHOULD_PASS_REPO_FAIL_OPEN_EMITTED.clear()

    def test_root_resolution_failed_emits_diagnostic(self) -> None:
        import io

        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ), unittest.mock.patch.object(
            _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("no root")
        ):
            fake_err = io.StringIO()
            with unittest.mock.patch("sys.stderr", fake_err):
                result = _mod._should_pass_repo("memo.list")

        self.assertTrue(result)
        out = fake_err.getvalue()
        self.assertIn("root-resolution-failed", out)
        self.assertIn("memo.list", out)

    def test_root_not_isabs_or_isdir_emits_diagnostic(self) -> None:
        import io

        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            fake_err = io.StringIO()
            with unittest.mock.patch("sys.stderr", fake_err):
                result = _mod._should_pass_repo("memo.list", claude_klabauter_root="not-absolute")

        self.assertTrue(result)
        out = fake_err.getvalue()
        self.assertIn("root-not-isabs-or-isdir", out)
        self.assertIn("memo.list", out)

    def test_post_injection_import_failed_emits_diagnostic(self) -> None:
        import io

        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            fake_err = io.StringIO()
            with unittest.mock.patch("sys.stderr", fake_err):
                result = _mod._should_pass_repo("memo.list", claude_klabauter_root=os.getcwd())

        self.assertTrue(result)
        out = fake_err.getvalue()
        self.assertIn("post-injection-import-failed", out)
        self.assertIn("memo.list", out)

    def test_dedup_one_emission_per_process_per_branch(self) -> None:
        import io

        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            fake_err = io.StringIO()
            with unittest.mock.patch("sys.stderr", fake_err):
                _mod._should_pass_repo("memo.list", claude_klabauter_root="not-absolute")
                _mod._should_pass_repo("queue.append", claude_klabauter_root="not-absolute")

        out = fake_err.getvalue()
        self.assertEqual(out.count("root-not-isabs-or-isdir"), 1)

    def test_ambient_branch_emits_nothing_even_when_terminal_reached(self) -> None:
        """The first except-Exception:pass around the ambient import must
        never itself emit -- only the three branches after it may."""
        import io

        with unittest.mock.patch.dict(
            sys.modules, {"coordinator_core.op_scopes": None}
        ):
            fake_err = io.StringIO()
            with unittest.mock.patch("sys.stderr", fake_err):
                _mod._should_pass_repo("memo.list", claude_klabauter_root=os.getcwd())

        out = fake_err.getvalue()
        # Exactly one diagnostic line (the terminal post-injection branch),
        # never a second one for the ambient miss that preceded it.
        self.assertEqual(len(out.strip().splitlines()), 1)


class ResolveEngineRootTest(unittest.TestCase):
    """resolve_engine_root / ensure_engine_on_path — the override-first ladder
    co-located coordinator/bin entrypoints use to find the engine they import.

    Spec backlink: the coordinator-doc-new ModuleNotFoundError fix — engine-
    touching seams resolved the root through the registry-only ladder (no
    self-location rung), so an install with no repos.claude_klabauter entry and
    no pointer file had no answer but a hand-set PYTHONPATH.
    """

    @staticmethod
    def _make_checkout(root: Path) -> None:
        (root / "coordinator_core").mkdir(parents=True)
        (root / "pyproject.toml").write_text("", encoding="utf-8")

    def test_env_override_outranks_self_location(self) -> None:
        """COORDINATOR_ENGINE_ROOT wins over the checkout the script sits in —
        the whole point of rung 1, and the regression a naive swap onto
        resolve_colocated_claude_klabauter_root introduces."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            other = Path(tmp) / "other"
            self._make_checkout(own)
            self._make_checkout(other)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            with unittest.mock.patch.dict(os.environ, {"COORDINATOR_ENGINE_ROOT": str(other)}):
                self.assertEqual(_mod.resolve_engine_root(str(script)), str(other))

    def test_blank_and_nonexistent_env_fall_through_to_self_location(self) -> None:
        """An empty or stale COORDINATOR_ENGINE_ROOT is not an override — it
        must not shadow a perfectly good co-located checkout.

        Deliberate divergence from ``_resolve_claude_klabauter_root``'s rung 1, which
        returns any non-empty env value verbatim (no isdir gate): this
        function adds the gate because a stale COORDINATOR_ENGINE_ROOT
        surviving a cross-platform ~/.claude sync must fall through, not be
        honored.

        NOTE: before C14 retargeted this fixture from the retired
        ``CLAUDE_KLABAUTER_ROOT`` name, this test passed VACUOUSLY — it set a variable
        the ladder no longer read, so the assertion below was unreachable and
        would have passed even if the isdir gate it exists to pin were
        deleted outright. Retargeting to the name the ladder actually reads
        makes the assertion reachable again.

        Asserts against ``own.resolve()`` rather than ``own``: on macOS,
        ``tempfile.TemporaryDirectory()`` yields a path under ``/var/...``
        while the resolver canonicalizes through the ``/var -> /private/var``
        symlink, so the raw and resolved forms diverge unless both sides of
        the comparison are resolved. — Review: coordinator:code-reviewer
        (a46f2a6d) P3
        """
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            for bogus in ("", str(Path(tmp) / "does-not-exist")):
                with unittest.mock.patch.dict(os.environ, {"COORDINATOR_ENGINE_ROOT": bogus}):
                    self.assertEqual(_mod.resolve_engine_root(str(script)), str(own.resolve()))

    def test_self_location_is_depth_agnostic(self) -> None:
        """A helper under coordinator/bin/lib/ resolves its own checkout too —
        the fixed parents[2] probe lands on coordinator/ and misses."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            self._make_checkout(own)
            deep = own / "coordinator" / "bin" / "lib" / "helper.py"
            deep.parent.mkdir(parents=True)
            deep.write_text("", encoding="utf-8")

            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
                os.environ.pop("COORDINATOR_ENGINE_ROOT", None)
                self.assertEqual(_mod.resolve_engine_root(str(deep)), str(own.resolve()))

    def test_falls_back_to_registry_ladder_outside_any_checkout(self) -> None:
        """Published/vendored outside a claude-klabauter tree — rung 3 answers."""
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "vendored" / "x.py"
            stray.parent.mkdir(parents=True)
            stray.write_text("", encoding="utf-8")

            with unittest.mock.patch.object(
                _mod, "_resolve_claude_klabauter_root", return_value="/from/registry"
            ):
                with unittest.mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
                    os.environ.pop("COORDINATOR_ENGINE_ROOT", None)
                    self.assertEqual(
                        _mod.resolve_engine_root(str(stray)), "/from/registry"
                    )

    def test_ensure_engine_on_path_inserts_at_front_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with unittest.mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
                    os.environ.pop("COORDINATOR_ENGINE_ROOT", None)
                    self.assertEqual(_mod.ensure_engine_on_path(str(script)), str(own.resolve()))
                    self.assertEqual(sys.path[0], str(own.resolve()))
                    _mod.ensure_engine_on_path(str(script))
                    self.assertEqual(sys.path.count(str(own.resolve())), 1)
            finally:
                sys.path[:] = saved

    def test_ensure_engine_on_path_degrades_to_none_when_unresolvable(self) -> None:
        """Callers are CLIs that must still run on an engine-less install —
        a resolution miss returns None, it does not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "vendored" / "x.py"
            stray.parent.mkdir(parents=True)
            stray.write_text("", encoding="utf-8")

            with unittest.mock.patch.object(
                _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("no root")
            ):
                with unittest.mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
                    os.environ.pop("COORDINATOR_ENGINE_ROOT", None)
                    self.assertIsNone(_mod.ensure_engine_on_path(str(stray)))


class RequireEngineVariantsTest(unittest.TestCase):
    """require_engine_on_path / require_colocated_engine_on_path — the fail-loud
    wrappers C1 added around resolve_engine_root / resolve_colocated_claude_klabauter_root.

    Spec backlink: pln-every-hand-rolled-engine-root-ceafca § C2
    AC2 (agreement with the wrapped ladder), AC3 (fail-loud, no swallowing), AC8
    (order pin — env-first vs self-location-first must not collapse to the same
    resolution when they diverge).
    """

    @staticmethod
    def _make_checkout(root: Path) -> None:
        (root / "coordinator_core").mkdir(parents=True)
        (root / "pyproject.toml").write_text("", encoding="utf-8")

    @staticmethod
    @contextlib.contextmanager
    def _hermetic_env(settings_home: Path):
        """Pin COORDINATOR_SETTINGS_HOME to a tmp dir with no pointer file, and
        neutralize CLAUDE_HOME/CLAUDE_KLABAUTER_ROOT — required per the brief's Part 1
        Hermeticity note: _resolve_claude_klabauter_root reads COORDINATOR_SETTINGS_HOME /
        CLAUDE_HOME (for the .claude-klabauter-live-root pointer) and the machine-local
        repos.claude_klabauter registry, not just CLAUDE_KLABAUTER_ROOT.

        CLAUDE_HOME/CLAUDE_CONFIG_DIR are PINNED to an empty tmp subdir, not
        merely popped: machine_local_impl_resolve.claude_home() falls through
        to the real ${HOME}/.claude when both env vars are absent, so popping
        alone lets a box with coordinator-claude installed spawn a subprocess
        against the REAL ~/.claude/bin/_machine_local.py instead of provably
        having no machine-local script to find. — Review: coordinator:code-
        reviewer (a46f2a6d) P2
        """
        fake_claude_home = settings_home / "_fake_claude_home"
        fake_claude_home.mkdir(parents=True, exist_ok=True)
        with unittest.mock.patch.dict(
            os.environ,
            {
                "COORDINATOR_SETTINGS_HOME": str(settings_home),
                "CLAUDE_HOME": str(fake_claude_home),
                "CLAUDE_CONFIG_DIR": str(fake_claude_home),
            },
            clear=False,
        ):
            os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
            # C11: cc_invoke's env-root reads now also check COORDINATOR_ENGINE_ROOT
            # (dual-read rename window) — must be neutralized here too, or a real
            # session's exported COORDINATOR_ENGINE_ROOT leaks into this "unset"
            # fixture the same way an un-popped CLAUDE_KLABAUTER_ROOT would.
            os.environ.pop("COORDINATOR_ENGINE_ROOT", None)
            yield

    # -- Agreement (AC2) -----------------------------------------------------

    def test_require_engine_on_path_agrees_with_resolve_engine_root_shallow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    expected = _mod.resolve_engine_root(str(script))
                    sys.path[:] = saved
                    self.assertEqual(_mod.require_engine_on_path(str(script)), expected)
            finally:
                sys.path[:] = saved

    def test_require_engine_on_path_agrees_with_resolve_engine_root_deep(self) -> None:
        """coordinator/bin/lib/X.py — the depth case the fixed parents[2] probe misses."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            deep = own / "coordinator" / "bin" / "lib" / "helper.py"
            deep.parent.mkdir(parents=True)
            deep.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    expected = _mod.resolve_engine_root(str(deep))
                    sys.path[:] = saved
                    self.assertEqual(_mod.require_engine_on_path(str(deep)), expected)
            finally:
                sys.path[:] = saved

    def test_require_colocated_engine_on_path_agrees_with_resolve_colocated_shallow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    expected = _mod.resolve_colocated_claude_klabauter_root(str(script))
                    sys.path[:] = saved
                    self.assertEqual(
                        _mod.require_colocated_engine_on_path(str(script)), expected
                    )
            finally:
                sys.path[:] = saved

    def test_require_colocated_engine_on_path_agrees_with_resolve_colocated_deep(self) -> None:
        """coordinator/bin/lib/X.py — resolve_colocated's fixed parents[2] probe
        lands on coordinator/bin, not the checkout root, and misses; both the
        wrapper and the raw call must fall through the same way.

        MIGRATED 2026-08-21. The AC here is AGREEMENT between the wrapper and
        the raw call, never a particular root — but it was expressed as
        `assertEqual(wrapper, expected)`, which can only be evaluated in the
        regime where the fall-through RESOLVES. C6 stamp-gated the ladder both
        of these fall through to, so on a hermetic box with no registry entry
        both now raise instead, and the test could not even compute `expected`.
        It had been red ever since, carried across sessions as `pre-existing`.

        Comparing OUTCOMES rather than return values pins the property in
        either regime: it stays honest if the ladder resolves again later, and
        it fails loudly if the two ever diverge — which is the only thing this
        test was ever asserting. Deliberately compares the exception's type and
        first message line, not the type alone: two different resolution
        failures raising the same class is exactly the divergence this would
        otherwise wave through."""

        def _outcome(call):
            # Bare except is safe ONLY here: raw and wrapped both run the same
            # call graph under the same fixture, and the comparison keys on
            # exception type AND first message line precisely so two
            # different failures cannot compare equal. Lifting this helper to
            # a shared location requires narrowing the caught type first.
            try:
                return ("resolved", call())
            except Exception as exc:  # noqa: BLE001 - the class is part of the comparison
                first_line = str(exc).splitlines()[0] if str(exc) else ""
                return ("raised", type(exc).__name__, first_line)

        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            deep = own / "coordinator" / "bin" / "lib" / "helper.py"
            deep.parent.mkdir(parents=True)
            deep.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    raw = _outcome(
                        lambda: _mod.resolve_colocated_claude_klabauter_root(str(deep))
                    )
                    sys.path[:] = saved
                    wrapped = _outcome(
                        lambda: _mod.require_colocated_engine_on_path(str(deep))
                    )
                    self.assertEqual(raw, wrapped)
            finally:
                sys.path[:] = saved

    # -- Order (AC8 pin) ------------------------------------------------------

    def test_env_first_vs_self_location_first_diverge_correctly(self) -> None:
        """COORDINATOR_ENGINE_ROOT points at a DIFFERENT valid checkout than the
        script's own: require_engine_on_path (env-first) must return the ENV
        tree; require_colocated_engine_on_path (self-location-first) must
        return its OWN tree. A naive migration that swapped the two ladders
        would collapse this into agreement — this is the assertion that
        catches it."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            other = Path(tmp) / "other"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            self._make_checkout(other)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    os.environ["COORDINATOR_ENGINE_ROOT"] = str(other)
                    self.assertEqual(
                        _mod.require_engine_on_path(str(script)), str(other)
                    )
                    sys.path[:] = saved
                    self.assertEqual(
                        _mod.require_colocated_engine_on_path(str(script)),
                        str(own.resolve()),
                    )
            finally:
                sys.path[:] = saved

    # -- Failure (AC3) ----------------------------------------------------------

    def test_require_variants_raise_runtimeerror_when_every_rung_misses(self) -> None:
        """Not reachable by unsetting env and moving the script outside a
        checkout — cc_invoke's own __file__-based terminal rung always
        resolves from a live checkout. Force the miss by patching
        _resolve_claude_klabauter_root to raise, mirroring
        test_falls_back_to_registry_ladder_outside_any_checkout's precedent."""
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "vendored" / "x.py"
            stray.parent.mkdir(parents=True)
            stray.write_text("", encoding="utf-8")
            settings_home = Path(tmp) / "settings-home"

            with unittest.mock.patch.object(
                _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("no root")
            ):
                with self._hermetic_env(settings_home):
                    with self.assertRaises(RuntimeError):
                        _mod.require_engine_on_path(str(stray))
                    with self.assertRaises(RuntimeError):
                        _mod.require_colocated_engine_on_path(str(stray))
                    self.assertIsNone(_mod.ensure_engine_on_path(str(stray)))

    def test_require_variants_do_not_swallow_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "vendored" / "x.py"
            stray.parent.mkdir(parents=True)
            stray.write_text("", encoding="utf-8")
            settings_home = Path(tmp) / "settings-home"

            with unittest.mock.patch.object(
                _mod, "_resolve_claude_klabauter_root", side_effect=OSError("broken junction")
            ):
                with self._hermetic_env(settings_home):
                    with self.assertRaises(OSError):
                        _mod.require_engine_on_path(str(stray))
                    with self.assertRaises(OSError):
                        _mod.require_colocated_engine_on_path(str(stray))

    # -- sys.path (front-insert, idempotent) -----------------------------------

    def test_require_engine_on_path_inserts_at_front_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    root = _mod.require_engine_on_path(str(script))
                    self.assertEqual(sys.path[0], root)
                    _mod.require_engine_on_path(str(script))
                    self.assertEqual(sys.path.count(root), 1)
            finally:
                sys.path[:] = saved

    def test_require_colocated_engine_on_path_inserts_at_front_and_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own"
            settings_home = Path(tmp) / "settings-home"
            self._make_checkout(own)
            script = own / "coordinator" / "bin" / "x.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")

            saved = list(sys.path)
            try:
                with self._hermetic_env(settings_home):
                    root = _mod.require_colocated_engine_on_path(str(script))
                    self.assertEqual(sys.path[0], root)
                    _mod.require_colocated_engine_on_path(str(script))
                    self.assertEqual(sys.path.count(root), 1)
            finally:
                sys.path[:] = saved


# ---------------------------------------------------------------------------
# Nonzero-exit diagnosis recovery — the child's STDOUT channel
# ---------------------------------------------------------------------------


class TestNonzeroExitStdoutDiagnosis(unittest.TestCase):
    """`_raise_on_process_failure` must surface the engine's own failure text.

    The defect this pins (reported by doe-claude-em,
    `cross-repo/inbox/2026-08-07-doe-claude-em-windows-ceremony-cli-coordinator-core-import-break.md`,
    item 3): `coordinator_core.invoke` writes a PRE-dispatch failure to stderr but
    a COMPLETED-dispatch op-level error to **stdout**, exiting 1 either way. This
    ladder read stderr only, so every op-level failure surfaced as a bare
    `invoke process exited 1 (op=X) — op or dispatch error` with an empty
    `stderr:` line and the reason nowhere — it was on stdout, discarded unread.
    That is the exact string DoE reported for `ceremony.wsc_tail`, and the reason
    they could not diagnose it: the transport, not the op, was withholding it.

    Spawn-free by construction: `_raise_on_process_failure` is a pure function
    over already-captured (rc, stdout, stderr), so every case below is a direct
    in-process call — no subprocess, no mock of one (spawn ratchet, Rule 1).
    """

    OP = "ceremony.wsc_tail"
    ROOT = "/fake/claude-klabauter"

    def _raises(self, rc: int, stdout: str, stderr: str = "") -> BaseException:
        with self.assertRaises(RuntimeError) as ctx:
            _mod._raise_on_process_failure(rc, stdout, stderr, self.OP, self.ROOT)
        return ctx.exception

    @staticmethod
    def _envelope(message: str, code: int = -32601) -> str:
        return json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
        )

    def test_stdout_error_envelope_reaches_the_raised_message(self) -> None:
        """The whole point: an operator reading `str(exc)` sees WHY it failed."""
        exc = self._raises(1, self._envelope("Method not found: 'ceremony.wsc_tail'"))
        text = str(exc)
        self.assertIn("Method not found: 'ceremony.wsc_tail'", text)
        self.assertIn("-32601", text)
        self.assertIn("exited 1", text)

    def test_stdout_borne_module_not_found_classifies_as_engine_wont_start(self) -> None:
        """An import failure INSIDE the op arrives on stdout, not stderr — it must
        still route to the install-remediation rung, not the generic one."""
        exc = self._raises(
            1,
            self._envelope("ModuleNotFoundError: No module named 'coordinator_core'", code=-32603),
        )
        text = str(exc)
        self.assertIn("engine will not import/start", text)
        self.assertIn("COORDINATOR_ENGINE_ROOT", text)
        self.assertIn("No module named 'coordinator_core'", text)

    def test_non_json_stdout_is_recovered_and_capped(self) -> None:
        """A raw traceback is not a JSON envelope but is still the only diagnosis
        there is; it is surfaced, bounded so it stays readable in a terminal."""
        exc = self._raises(1, "Traceback (most recent call last):\n" + "x" * 9000)
        text = str(exc)
        self.assertIn("op stdout: Traceback", text)
        self.assertLess(len(text), _mod._OP_ERROR_DETAIL_CAP + 1000)

    def test_empty_stdout_leaves_the_message_unchanged(self) -> None:
        """No recoverable detail must not append a dangling empty line."""
        exc = self._raises(1, "", "some op-level error")
        self.assertNotIn("op error:", str(exc))
        self.assertNotIn("op stdout:", str(exc))

    def test_stderr_import_error_still_outranks_everything(self) -> None:
        """Pre-existing precedence rung 1 — unchanged, including at rc=2."""
        exc = self._raises(
            2,
            self._envelope("something else entirely"),
            "ModuleNotFoundError: No module named 'coordinator_core'",
        )
        self.assertNotIsInstance(exc, _mod.StructuralPinError)
        self.assertIn("engine will not import/start", str(exc))

    def test_structural_pin_outranks_the_stdout_import_token(self) -> None:
        """Negative-spec pin: the stdout sniff is ranked BELOW rc==2 deliberately.

        Reclassifying a structural-pin failure as an install failure would discard
        the engine's own non-self-healing/will-recur-on-retry discriminator.
        """
        exc = self._raises(2, self._envelope("no module named 'widget' in the pinned contract"))
        self.assertIsInstance(exc, _mod.StructuralPinError)
        self.assertIn("structural contract-pin failure", str(exc))
        self.assertIn("no module named 'widget'", str(exc))

    def test_engine_channel_contract_op_error_exits_one(self) -> None:
        """Pins the engine-side coupling this whole fix rests on.

        `_exit_code_for_response` is the engine's own pure/testable seam (see its
        docstring): an op-level error response exits 1, and `main()` prints that
        response to STDOUT. If either half ever moves, the ladder above needs
        revisiting rather than silently going blind again.
        """
        from coordinator_core.invoke.__main__ import _exit_code_for_response
        from coordinator_core.ipc import STRUCTURAL_PIN_ERROR

        self.assertEqual(
            _exit_code_for_response({"error": {"code": -32601, "message": "x"}}, STRUCTURAL_PIN_ERROR),
            1,
        )
        self.assertEqual(
            _exit_code_for_response({"error": {"code": STRUCTURAL_PIN_ERROR, "message": "x"}}, STRUCTURAL_PIN_ERROR),
            2,
        )
        self.assertEqual(_exit_code_for_response({"result": {}}, STRUCTURAL_PIN_ERROR), 0)


# ---------------------------------------------------------------------------
# End-to-end through a REAL coordinator_core.invoke child — the two reachable
# failure rungs, plus the positive control.
# ---------------------------------------------------------------------------


class TestDiagnosticsProbesEndToEnd(unittest.TestCase):
    """`cc_invoke` classifies a real engine child's failure correctly, not just a fake one.

    Everything above this class is spawn-free: `TestNonzeroExitStdoutDiagnosis`
    exercises the ladder as a pure function over already-captured
    `(rc, stdout, stderr)`, and the State-2 classes drive a synthetic
    `__main__.py` written by `_make_fake_claude_klabauter_root`. Neither can catch a
    disagreement between what the ladder expects a child to emit and what
    `coordinator_core.invoke` actually emits — a fake oracle agrees with itself
    by construction. These three cases close exactly that gap by firing the
    write-free `diagnostics.*` probe family (`coordinator_core/ops/
    diagnostics_probes.py`) at the real engine in this checkout.

    THREE SPAWNS, one per test, function-level — the module docstring's declared
    count. `_op_timeout_ceiling` is stubbed so the transport's own
    `--dump-op-timeouts` probe cannot add a fourth process; the timeout budget is
    not what these cases are about, and the ladder under test never reads it.

    Safe against a live dirty tree by construction, which is the whole reason the
    probe family exists: all three handlers are `COMPUTE_ONLY` and write nothing.

    Negative-spec: do not add a fourth case for the three remaining rungs.
    Stderr-borne ImportError, empty stdout, and transport-absent are unreachable
    from a REGISTERED op (the engine has already imported and started before any
    handler runs; `invoke.main` always prints a response; and transport-absent is
    a CLAUDE_KLABAUTER_ROOT resolution failure with no op involved). They stay unit-level —
    see docs/reference/transport-failure-probes.md.

    Spec backlink: pln-a-safe-target-for-transport-fa-7ea067 § C2
    """

    def _invoke(self, op: str) -> Any:
        """Route `op` through `cc_invoke` to a real engine child.

        The ENGINE root is the box's stamped published engine, not this
        checkout. This class's subject is how a transport/op failure SURFACES
        to a `cc_invoke` caller, never which tree served it -- and a source
        checkout carries no build stamp, so `ipc.py`'s dispatch-axis stamp gate
        refuses it before any handler runs and every probe here reports the
        refusal instead of the failure mode it was written to pin. The REPO
        root stays this checkout: that is the op's subject, and it is a
        different axis from which engine executes.
        """
        from engine_stamp_probe import _stamped_dispatch_root

        engine_root = _stamped_dispatch_root()
        if engine_root is None:
            pytest.skip("no stamped engine on this box — a real engine child is unreachable")
        with unittest.mock.patch.object(_mod, "_op_timeout_ceiling", return_value=120):
            return _mod.cc_invoke(op, {}, str(_REPO_ROOT), _claude_klabauter_root=engine_root)

    @pytest.mark.spawns_process
    def test_always_succeeds_returns_its_result(self) -> None:
        """Positive control: without it, a green failure-case could be green by
        accident — a harness that never reached the engine at all would satisfy
        both failure assertions below while proving nothing."""
        result = self._invoke("diagnostics.always_succeeds")

        self.assertEqual(result, {"probe": "always_succeeds", "ok": True})

    @pytest.mark.spawns_process
    def test_always_refuses_surfaces_the_stdout_borne_op_error(self) -> None:
        """rc=1 with the error envelope on the child's STDOUT — the rung the
        transport was blind to before 337c31a1e, now asserted through a real
        child rather than a reconstructed `(rc, stdout, stderr)` triple."""
        with self.assertRaises(RuntimeError) as ctx:
            self._invoke("diagnostics.always_refuses")

        exc = ctx.exception
        text = str(exc)
        self.assertNotIsInstance(exc, _mod.StructuralPinError)
        self.assertIn("exited 1 (op=diagnostics.always_refuses)", text)
        self.assertIn("op error: code=-32603", text)
        self.assertIn("message=", text)
        self.assertIn("DiagnosticsRefusal", text)
        self.assertNotIn("engine will not import/start", text)

    @pytest.mark.spawns_process
    def test_always_structural_pin_raises_structural_pin_error(self) -> None:
        """rc=2 -> `StructuralPinError`, with the handler's own message preserved
        verbatim into the envelope, and precedence intact: the structural-pin rung
        still outranks the stdout-borne ImportError rung below it."""
        with self.assertRaises(_mod.StructuralPinError) as ctx:
            self._invoke("diagnostics.always_structural_pin")

        text = str(ctx.exception)
        self.assertIn("structural contract-pin failure", text)
        self.assertIn("op=diagnostics.always_structural_pin, rc=2", text)
        self.assertIn("op error: code=-32001", text)
        self.assertIn("nothing is actually wedged", text)
        self.assertNotIn("engine will not import/start", text)
