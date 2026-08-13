"""
coordinator_core.tests.test_invoke_main — Subprocess regression net for
coordinator_core.invoke.__main__.

Purpose: Because main() calls os._exit, it cannot be tested in-process — any
in-process call terminates the Python interpreter.  Every test here spawns a
fresh subprocess via subprocess.run and asserts on returncode, stdout, and stderr.

Invocation shape under test:
    sys.executable -m coordinator_core.invoke <op> <params_json> [--repo <path>]

Branches covered:
  1. Happy path     — ping '{}' → exit 0, stdout JSON "ok": true, stderr empty.
  2. Invalid JSON   — ping 'not json' → _fatal_stderr → exit 1, error JSON on
                      STDERR, stdout empty.  (Also covers branch 7 — stream check.)
  3. _origin_worktree injection — worktree-scoped op (_WORKTREE_SCOPED_PROBE) run
                      inside the claude-klabauter repo succeeds in dispatching (proves injection fires).
  4. C2 regression  — none-scoped op (ping) run from a non-git temp dir exits 0,
                      proving _resolve_repo_root is NOT called for none-scoped ops.
  5. --repo honored — the same worktree-scoped probe with --repo <root> resolves the
                      repo explicitly.
  6. C3 regression  — handler internal timeout does not wedge the process; os._exit
                      fires promptly even if asyncio.to_thread work is still live.
  7. STDERR stream  — _fatal_stderr writes to STDERR, not STDOUT (explicit assertion
                      in test_invalid_params_json_writes_to_stderr).
  8. --params-file  — large (>32KB) JSON params payload read from a file instead of
                      argv (ARG_MAX-safe transport) → exit 0, dispatched normally.
  9. Mutual exclusion — positional params_json AND --params-file together → exit 1,
                      _fatal_stderr on STDERR (never both consumed).
 10. --dump-op-timeouts — no <op> required, valid JSON on stdout, includes
                      "__default__" and no per-op overrides (DEC-2 retired the three
                      ceremony.wsc_* overrides -- table is empty), "__default__"
                      live-resolves an overridden COORDINATOR_DISPATCH_TIMEOUT_SECS.
 11. _exit_code_for_response — in-process (no subprocess) unit coverage of the exit-code
                      selection helper: success (0), a generic/transient JSON-RPC error
                      (1), and a STRUCTURAL_PIN_ERROR-coded error (2). Covers the same
                      "transient → soft, contract-class → loud" distinction as
                      test_dispatch_message.py's dispatch_message-level tests, one layer
                      up — this is the function main() calls to pick os._exit()'s code.
 12. --params-file - empty stdin — EOF/empty stdin on the "-" branch fails the same
                      exit-1/"Invalid params_json" contract as malformed JSON, pinned
                      separately from the malformed-JSON case (branch 8b).
 13. --params-file - non-ASCII stdin — raw UTF-8 bytes (not text=True str input, which
                      would encode with the parent's own locale default and never
                      reproduce the mismatch) decode correctly even under a forced
                      non-UTF-8 child locale (LC_ALL=C), proving the stdin decode is
                      pinned to explicit UTF-8, not locale.getpreferredencoding().

AC cross-reference (from dispatch brief):
  AC3 (none-scope-outside-git):  test_none_scoped_outside_git_tree
  AC4 (no-hang under timeout):   test_no_hang_under_handler_timeout
  AC5 (entrypoint coverage):     all tests exercise __main__.main() via subprocess

Spec backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
Plan backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.invoke.__main__ import _exit_code_for_response
from coordinator_core.ipc import STRUCTURAL_PIN_ERROR

# Declared, not excused: main() calls os._exit, so it cannot be tested in-process --
# every test spawns a real `sys.executable -m coordinator_core.invoke` child and
# asserts on returncode/stdout/stderr, which is the entrypoint's own process-exit
# contract, not mockable. Each test spawns its own child (no shared fixture) because
# the property under test IS that fresh-process boundary.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Project root — needed for PYTHONPATH injection so subprocess can import
# coordinator_core regardless of cwd (tests may run from temp dirs).
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)

# Portable Windows console-suppression flag — resolves to CREATE_NO_WINDOW (0x08000000)
# on Windows and 0 (no-op) on macOS/Linux.  Required for every python.exe subprocess so
# the headless Bash-tool parent does not get a focus-stealing console window.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# A cheap, read-only op in WORKTREE_SCOPED_OPS, used as the vehicle for the two
# `_origin_worktree`-injection branches below. Requirements on the vehicle: it must
# be worktree-scoped (so main() resolves a repo root and injects it) and its cost
# must not scale with the repo's history or corpora — see
# `test_worktree_scoped_op_dispatches_inside_repo`'s "Vehicle note" for the
# incident behind that second requirement. The candidate path is deliberately one
# that does not exist: the handler resolves it RELATIVE to the injected repo root
# and reports it back, so the resulting error string is itself the witness that
# repo_root arrived.
_WORKTREE_SCOPED_PROBE = (
    "handoff.has_live_children",
    '{"candidate": "state/handoffs/does-not-exist-invoke-main-test.md"}',
)
_PROBE_EXPECTED_ERROR = (
    "candidate not found on disk: state/handoffs/does-not-exist-invoke-main-test.md"
)


def _make_env(**overrides: str) -> dict[str, str]:
    """Return a copy of the current environment with PYTHONPATH and any overrides.

    PYTHONPATH is prepended with _PROJECT_ROOT so that a subprocess started with
    any cwd (including a non-git temp dir) can still import coordinator_core.
    """
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else _PROJECT_ROOT
    env.update(overrides)
    return env


def _invoke(*args: str, cwd: str | Path | None = None, env: dict | None = None,
            timeout: int = 30) -> subprocess.CompletedProcess:
    """Spawn coordinator_core.invoke as a subprocess and return the CompletedProcess.

    Args:
        *args:   Arguments after ``python -m coordinator_core.invoke``.
        cwd:     Working directory for the subprocess (default: claude-klabauter repo root).
        env:     Environment dict (default: _make_env()).
        timeout: subprocess.run timeout in seconds (default: 30).
    """
    cmd = [sys.executable, "-m", "coordinator_core.invoke", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else _PROJECT_ROOT,
        env=env if env is not None else _make_env(),
        creationflags=_NO_CONSOLE,
    )


# ---------------------------------------------------------------------------
# Branch 1 — Happy path: ping '{}' → exit 0, stdout JSON, stderr empty
# ---------------------------------------------------------------------------

def test_happy_path_ping_exits_zero():
    """Branch 1: ping '{}' → exit 0, stdout is a JSON-RPC result with ok: true.

    Covers AC5 (entrypoint reachable) and the nominal dispatch path.
    stderr must be empty — no log noise leaks into the error stream on success.
    """
    result = _invoke("ping", "{}")

    assert result.returncode == 0, (
        f"ping '{{}}' must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    parsed = json.loads(result.stdout)
    assert parsed.get("jsonrpc") == "2.0", f"Expected jsonrpc=2.0; got {parsed.get('jsonrpc')!r}"
    assert "result" in parsed, f"Expected a 'result' key in response; got: {parsed}"
    assert parsed["result"].get("ok") is True, (
        f"ping result must contain ok=true; got {parsed['result']!r}"
    )
    assert "error" not in parsed, f"No 'error' key on success; got: {parsed}"

    assert result.stderr.strip() == "", (
        f"stderr must be empty on success; got {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Branch 1b — --bare: success path prints ONLY the bare `result` object
# ---------------------------------------------------------------------------

def test_bare_flag_prints_only_result_object():
    """--bare success path: stdout is json.dumps(response["result"]) with no
    jsonrpc/id envelope and no indentation -- the single-spawn transport
    contract cc_invoke relies on to consume stdout directly instead of
    spawning a second process to strip the envelope.
    """
    result = _invoke("ping", "{}", "--bare")

    assert result.returncode == 0, (
        f"ping --bare must exit 0; got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )

    parsed = json.loads(result.stdout)
    # Bare output IS the result object directly -- no envelope wrapper.
    assert "jsonrpc" not in parsed, f"--bare must omit the jsonrpc envelope; got {parsed}"
    assert "result" not in parsed, f"--bare must omit the 'result' nesting key; got {parsed}"
    assert parsed.get("ok") is True, f"--bare result must be the ping payload directly; got {parsed}"

    assert result.stderr.strip() == "", (
        f"stderr must be empty on --bare success; got {result.stderr!r}"
    )


def test_bare_flag_matches_default_result_payload():
    """--bare output must equal response["result"] from the default (non-bare) call --
    same op, same params, only the envelope differs.
    """
    default_result = _invoke("ping", "{}")
    bare_result = _invoke("ping", "{}", "--bare")

    default_parsed = json.loads(default_result.stdout)
    bare_parsed = json.loads(bare_result.stdout)
    default_result_obj = default_parsed["result"]

    # ping's payload includes a live `ts` timestamp that legitimately differs
    # between two separate subprocess spawns, so compare shape (key set) and
    # the timestamp-independent `ok` field rather than exact dict equality.
    assert set(bare_parsed.keys()) == set(default_result_obj.keys()), (
        f"--bare payload must have the same keys as the default envelope's 'result' key. "
        f"bare keys: {sorted(bare_parsed.keys())!r} default result keys: {sorted(default_result_obj.keys())!r}"
    )
    assert bare_parsed.get("ok") == default_result_obj.get("ok") is True, (
        f"--bare payload's ok field must match the default envelope's result.ok. "
        f"bare: {bare_parsed!r} default result: {default_result_obj!r}"
    )


def test_default_output_unchanged_by_bare_flag_existence():
    """Default (no --bare) invocation is byte-identical to pre-existing behavior --
    full JSON-RPC 2.0 envelope, indent=2. Adding --bare must not perturb the
    default path.
    """
    result = _invoke("ping", "{}")

    assert result.returncode == 0
    # indent=2 formatting means multi-line stdout for a non-trivial payload.
    _NL = chr(10)
    assert _NL in result.stdout.rstrip(_NL), (
        f"Default output must remain indent=2 (multi-line); got {result.stdout!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed.get("jsonrpc") == "2.0"
    assert "result" in parsed


# ---------------------------------------------------------------------------
# Branch 2 + 7 — Invalid params_json → _fatal_stderr → exit 1, STDERR not STDOUT
# ---------------------------------------------------------------------------

def test_invalid_params_json_writes_to_stderr():
    """Branch 2 + 7: ping 'not json' → exit 1; error JSON emitted to STDERR, not STDOUT.

    _fatal_stderr contract:
      - Writes a JSON-RPC 2.0 error envelope to stderr.
      - Calls os._exit(1) — exit code 1.
      - stdout is empty (no partial output before the fatal abort).

    The stream separation is the load-bearing assertion here: a caller that reads stdout
    for the result must NOT receive any fatal-error content on success reads.
    """
    result = _invoke("ping", "not json")

    assert result.returncode == 1, (
        f"Invalid params_json must exit 1; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # stdout must be EMPTY — fatal abort before any result is printed.
    assert result.stdout.strip() == "", (
        f"stdout must be empty on _fatal_stderr path; got {result.stdout!r}"
    )

    # stderr must contain a JSON-RPC 2.0 error envelope.
    assert result.stderr.strip(), "stderr must be non-empty on _fatal_stderr path"
    parsed_err = json.loads(result.stderr.strip())
    assert parsed_err.get("jsonrpc") == "2.0", (
        f"_fatal_stderr must emit a JSON-RPC 2.0 envelope; got {parsed_err.get('jsonrpc')!r}"
    )
    assert "error" in parsed_err, (
        f"_fatal_stderr envelope must contain 'error' key; got {parsed_err}"
    )
    assert parsed_err["error"]["code"] == -32603, (
        f"Expected INTERNAL_ERROR code -32603; got {parsed_err['error']['code']}"
    )
    # Informative message — must mention the params problem.
    assert "params_json" in parsed_err["error"]["message"].lower() or \
           "json" in parsed_err["error"]["message"].lower(), (
        f"Error message should reference 'params_json' or 'json'; "
        f"got {parsed_err['error']['message']!r}"
    )


def test_params_not_a_dict_writes_to_stderr():
    """Branch 2 variant: params_json that is valid JSON but not an object → exit 1, STDERR."""
    result = _invoke("ping", '"just a string"')

    assert result.returncode == 1, (
        f"Non-dict params_json must exit 1; got {result.returncode}"
    )
    assert result.stdout.strip() == "", (
        f"stdout must be empty on _fatal_stderr path; got {result.stdout!r}"
    )
    parsed_err = json.loads(result.stderr.strip())
    assert "error" in parsed_err, f"Expected error envelope on stderr; got {parsed_err}"
    assert parsed_err["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# Branch 3 — _origin_worktree injection: worktree-scoped op runs inside repo
# ---------------------------------------------------------------------------

def test_worktree_scoped_op_dispatches_inside_repo():
    """Branch 3: a worktree-scoped op run from the repo cwd dispatches.

    When main() detects an op in WORKTREE_SCOPED_OPS, it calls _resolve_repo_root()
    (which runs git rev-parse from cwd) and injects _origin_worktree into the
    JSON-RPC envelope.  dispatch_message then receives a valid repo_root.

    Behavioral assertion: the op returns a JSON-RPC response on stdout (not
    a fatal pre-dispatch error on stderr), and its own error string is the
    candidate-not-found one — which is only reachable AFTER repo_root resolved
    (the handler's own `_ORIGIN_WORKTREE_MISSING_ERROR` branch fires first when
    it did not), so this is a positive witness that injection worked, not just
    that something got dispatched.

    Vehicle note: this used to drive `coverage.gate`, which was a poor choice —
    that op's cost scales with the repo's review-trail corpus (measured 48s on
    this tree, past both the engine's own 30s dispatch timeout and this test's
    subprocess timeout), so the test measured an unrelated subsystem's runtime
    rather than invoke's argument plumbing. `handoff.has_live_children` is
    worktree-scoped the same way (`op_scopes._OP_KEY_SCOPE`, common_dir class,
    hence in WORKTREE_SCOPED_OPS) and returns in ~0.1s. Do NOT restore a
    history-walking op here; the scope class is the only property this test
    needs from its vehicle.

    Contrast with test_none_scoped_outside_git_tree: a none-scoped op never calls
    _resolve_repo_root(), so running OUTSIDE a git tree still exits 0.
    """
    # Run from the claude-klabauter repo root — git rev-parse will succeed here.
    result = _invoke(*_WORKTREE_SCOPED_PROBE, cwd=_PROJECT_ROOT)

    # returncode 0 = success result; returncode 1 = JSON-RPC error result.
    # Either means dispatch completed.
    assert result.returncode in (0, 1), (
        f"{_WORKTREE_SCOPED_PROBE[0]} must exit 0 or 1; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # stdout must be non-empty and valid JSON (dispatched, not a fatal abort).
    assert result.stdout.strip(), (
        f"stdout must contain a JSON-RPC response; got empty stdout.\n"
        f"stderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0", (
        f"stdout must be a JSON-RPC 2.0 envelope; got {parsed.get('jsonrpc')!r}"
    )
    assert "result" in parsed or "error" in parsed, (
        f"Response must have 'result' or 'error'; got {parsed}"
    )
    assert parsed["result"]["error"] == _PROBE_EXPECTED_ERROR, (
        f"handler must have seen a resolved repo_root (its own missing-worktree "
        f"branch would have fired instead); got {parsed}"
    )

    # stderr must be empty — no fatal pre-dispatch error.
    assert result.stderr.strip() == "", (
        f"stderr must be empty when dispatch succeeds; got {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Branch 4 — C2 regression: none-scoped op outside any git tree exits 0
# ---------------------------------------------------------------------------

def test_none_scoped_outside_git_tree():
    """Branch 4 (AC3 — C2 regression): ping from a non-git temp dir exits 0.

    Before C2's fix, _resolve_repo_root() was called unconditionally and would
    crash via git rev-parse when run outside any git working tree.  After C2,
    none-scoped ops skip _resolve_repo_root() entirely — repo_root stays None.

    This test verifies that behavior: ping '{}' succeeds from a directory that
    is definitively not inside any git repository.

    AC3 cross-reference: the dispatch brief calls this 'AC3 none-scope-outside-git'.
    """
    # Create a temp directory guaranteed to be outside any git repo.
    # tempfile.gettempdir() returns a system temp dir (/var/folders/... on macOS,
    # /tmp on Linux) — none of which are inside a git working tree.
    tmp_dir = tempfile.mkdtemp(dir=tempfile.gettempdir(), prefix="cc_invoke_test_")
    try:
        # Verify the temp dir is truly outside any git repo (defense-in-depth).
        probe = subprocess.run(
            ["git", "-C", tmp_dir, "rev-parse", "--git-dir"],
            capture_output=True,
            creationflags=_NO_CONSOLE,
        )
        assert probe.returncode != 0, (
            f"Setup error: temp dir {tmp_dir!r} is inside a git repo — "
            f"this invalidates the C2 regression test.  Choose a path outside all repos."
        )

        # Run ping from the non-git temp dir — must exit 0 without crashing.
        result = _invoke("ping", "{}", cwd=tmp_dir)

        assert result.returncode == 0, (
            f"ping '{{}}' from a non-git dir must exit 0 (C2 regression — none-scoped "
            f"ops must not call git rev-parse); got {result.returncode}.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

        parsed = json.loads(result.stdout)
        assert parsed["result"].get("ok") is True, (
            f"ping result must be ok=true; got {parsed['result']!r}"
        )
        assert result.stderr.strip() == "", (
            f"stderr must be empty; got {result.stderr!r}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Branch 5 — --repo explicit path is honored for a worktree-scoped op
# ---------------------------------------------------------------------------

def test_explicit_repo_flag_honored():
    """Branch 5: a worktree-scoped op with --repo <root> resolves the repo explicitly.

    When --repo is passed, _resolve_repo_root() uses Path(repo_arg).resolve()
    rather than calling git rev-parse from cwd.  The test runs from the project
    root with an explicit --repo flag and confirms:
      - stdout is a JSON-RPC 2.0 response (dispatch succeeded).
      - stderr is empty (no fatal pre-dispatch failure).

    Same vehicle, and the same reason for it, as
    `test_worktree_scoped_op_dispatches_inside_repo` — see its "Vehicle note".
    The empty-stderr assertion here is what the old `coverage.gate` vehicle could
    not satisfy at all: a dispatch that outruns the engine's 30s timeout prints a
    timeout line to stderr, so the assertion was hostage to that op's runtime.
    """
    result = _invoke(*_WORKTREE_SCOPED_PROBE, "--repo", _PROJECT_ROOT)

    assert result.returncode in (0, 1), (
        f"{_WORKTREE_SCOPED_PROBE[0]} with --repo must exit 0 or 1; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    assert result.stdout.strip(), "stdout must be non-empty (JSON-RPC response)"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert result.stderr.strip() == "", (
        f"stderr must be empty with a valid --repo; got {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Branch 6 — C3 regression: handler timeout does not wedge the process (AC4)
# ---------------------------------------------------------------------------

def test_no_hang_under_handler_timeout():
    """Branch 6 (AC4 — C3 regression): os._exit terminates promptly even when
    asyncio.to_thread work is still live after the internal timeout fires.

    Design rationale (from __main__.py):
      The manual event loop (asyncio.new_event_loop + loop.run_until_complete)
      deliberately OMITS loop.close() / shutdown_default_executor().  A handler
      that asyncio.to_thread'd and then timed out leaves a live executor thread;
      asyncio.run()'s shutdown_default_executor() would JOIN that thread and hang
      indefinitely.  os._exit() below terminates the process without that join.

    Test mechanics:
      - Set COORDINATOR_DISPATCH_TIMEOUT_SECS=0.001 so the internal wait_for
        fires almost immediately (coverage.gate does real git work > 1ms).
      - subprocess.run(..., timeout=15) — if the process hangs, TimeoutExpired
        is raised and the test fails.
      - Assert result.returncode is not None (process exited, not killed by us).
      - Assert stdout is a JSON-RPC response with the expected timeout error.

    Negative-spec: if asyncio.run() were used instead of the manual loop, this
    test would raise subprocess.TimeoutExpired because shutdown_default_executor()
    joins the live thread — blocking until the thread finishes (unbounded time).
    The 15s outer timeout is the regression guard.
    """
    env = _make_env(COORDINATOR_DISPATCH_TIMEOUT_SECS="0.001")

    # coverage.gate does real git work (git log + coverage state reads) that
    # easily exceeds 1ms — the internal wait_for timeout will fire reliably.
    #
    # Review: code-reviewer (nit) — this test is probabilistic: on very fast hardware or a
    # hot git cache, coverage.gate MIGHT complete in < 1ms and the timeout path is never
    # exercised. The test would then pass on the success path (which was always fine), not
    # the executor-drain + os._exit timeout path (the actual AC4 regression target). To make
    # it deterministic, replace coverage.gate with a test-only handler that sleeps (e.g., a
    # conftest-registered time.sleep(1) op under a 0.001s timeout) and assert unconditionally
    # that "timed out" appears in the error response. That work is deferred to avoid coupling
    # this test to test-only op registration infrastructure. The current test DOES guard the
    # primary regression: the process exits promptly (within 15s) rather than hanging forever.
    try:
        result = _invoke(
            "coverage.gate", "{}", "--repo", _PROJECT_ROOT,
            env=env,
            timeout=15,  # outer guard — process must exit well within 15s
        )
    except subprocess.TimeoutExpired:  # pragma: no cover
        raise AssertionError(
            "C3 regression: subprocess did NOT exit within 15s with "
            "COORDINATOR_DISPATCH_TIMEOUT_SECS=0.001 — os._exit hang suspected.  "
            "This is the hang that a manual loop + os._exit must prevent."
        )

    # Process exited — returncode must be set (not None).
    assert result.returncode is not None, "returncode must be set after normal exit"

    # With a 0.001s timeout, coverage.gate almost certainly times out → exit 1.
    # If by fluke the op completes in < 1ms, exit 0 is also acceptable.
    assert result.returncode in (0, 1), (
        f"Expected exit 0 or 1 after timeout; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # stdout must be valid JSON — the response was emitted and flushed before os._exit.
    assert result.stdout.strip(), "stdout must contain the JSON-RPC response"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"

    # If the op timed out, the error message must say so.
    if "error" in parsed:
        assert "timed out" in parsed["error"]["message"], (
            f"Expected 'timed out' in error message; got {parsed['error']['message']!r}"
        )


# ---------------------------------------------------------------------------
# Branch 8 -- --params-file: large (>32KB) JSON params payload via a file
# ---------------------------------------------------------------------------

def test_params_file_reads_large_payload(tmp_path):
    """Branch 8: --params-file with a >32KB JSON params payload dispatches
    successfully.

    ARG_MAX-safe transport contract from the __main__.py module docstring:
    a large params payload (e.g. ceremony.wsc_commit's resolved_state
    round-trip) can exceed argv limits, notably on Windows/msys (~32KB) --
    --params-file reads the JSON object from disk instead of argv, so it is
    immune to that limit. "ping" ignores its params entirely, so it is the
    cheapest op to exercise the read-from-file path without also depending
    on any particular op's params schema.
    """
    padding = "x" * 40_000  # comfortably over the ~32KB ARG_MAX danger zone
    payload = {"padding": padding}
    params_path = tmp_path / "large-params.json"
    params_path.write_text(json.dumps(payload), encoding="utf-8")
    assert params_path.stat().st_size > 32_000, (
        "test fixture assumption broken: params file must exceed 32KB"
    )

    result = _invoke("ping", "--params-file", str(params_path))

    assert result.returncode == 0, (
        f"ping via --params-file with a >32KB payload must exit 0; got "
        f"{result.returncode}.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed.get("jsonrpc") == "2.0"
    assert "result" in parsed, f"Expected a 'result' key in response; got: {parsed}"
    assert parsed["result"].get("ok") is True, (
        f"ping result must contain ok=true; got {parsed['result']!r}"
    )
    assert result.stderr.strip() == "", (
        f"stderr must be empty on success; got {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Branch 8b -- --params-file "-": params JSON read from stdin
# ---------------------------------------------------------------------------

def test_params_file_dash_reads_stdin():
    """`--params-file -` reads the params JSON from stdin.

    This is the quoting-immune transport, and the reason it exists is a
    SHELL failure, not an engine one: a payload carrying an apostrophe (a
    commit message saying "C1's half") ends the single-quoted argv span in
    bash, so the payload never reaches this process intact and no
    engine-side handling can recover it. Fed by a quoted heredoc, stdin has
    no interpolation and no quote sensitivity.

    The payload here therefore carries the exact byte classes that break the
    argv form -- apostrophe, parentheses, `$`, backtick, real newlines --
    and the assertion is that they arrive as written.
    """
    payload = {
        "message": "C1's half (build, not harden)\n\n$HOME and `date` verbatim\n",
    }
    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "ping",
         "--params-file", "-", "--bare"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )
    assert result.returncode == 0, (
        f"ping via --params-file - must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert json.loads(result.stdout).get("ok") is True


def test_params_file_dash_rejects_invalid_stdin_json():
    """Unparseable stdin fails loud on stderr, same contract as a file."""
    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "ping",
         "--params-file", "-"],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )
    assert result.returncode == 1
    assert "Invalid params_json" in result.stderr


def test_params_file_dash_empty_stdin_rejects_same_as_malformed():
    """Empty/EOF stdin fails the same exit-1/"Invalid params_json" contract
    as malformed JSON, distinct from the malformed-JSON case above.

    Review: code-reviewer (nit, Finding 4) — empty stdin (json.loads("")
    raises JSONDecodeError) is a plausible accidental-invocation shape (a
    caller forgets the heredoc body) that was not separately pinned.
    """
    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "ping",
         "--params-file", "-"],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )
    assert result.returncode == 1
    assert "Invalid params_json" in result.stderr


def test_params_file_dash_reads_non_ascii_stdin_as_utf8():
    """`--params-file -` decodes stdin as explicit UTF-8, not the platform
    locale codec -- reproduced even when the child's own locale is forced
    to a non-UTF-8 codec.

    Review: code-reviewer (P2, Finding 3) -- a str payload piped via
    subprocess.run(text=True, input=<str>) is encoded by the PARENT using
    its own locale default, so parent and child agree and a
    locale-vs-UTF-8 mismatch never reproduces even with non-ASCII content.
    This test instead feeds raw UTF-8 BYTES (text=False) AND forces
    LC_ALL=C in the child's env, so locale.getpreferredencoding() resolves
    to a strict ASCII-range codec inside the child -- the same shape as
    Windows resolving a redirected pipe to the (also non-UTF-8) ANSI code
    page. Pre-fix (sys.stdin.read(), locale-dependent decode) this would
    raise UnicodeDecodeError against the forced non-UTF-8 locale; post-fix
    (sys.stdin.buffer.read().decode("utf-8")) decode is locale-independent
    and must succeed regardless of the child's own locale env. ping ignores
    its params entirely, so this cannot assert content fidelity through the
    op's response -- it asserts the decode step itself does not raise
    under a hostile locale, which is the actual boundary Finding 1 fixed.
    """
    payload = {
        "message": "em dash —, curly quotes “quoted”, "
                    "non-Latin 日本語 verbatim\n",
    }
    child_env = _make_env(LC_ALL="C", LANG="C")
    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "ping",
         "--params-file", "-", "--bare"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        text=False,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=child_env,
        creationflags=_NO_CONSOLE,
    )
    stdout = result.stdout.decode("utf-8")
    stderr = result.stderr.decode("utf-8")
    assert result.returncode == 0, (
        f"ping via --params-file - with non-ASCII UTF-8 bytes under a "
        f"forced non-UTF-8 locale must exit 0; got {result.returncode}.\n"
        f"stdout: {stdout!r}\nstderr: {stderr!r}"
    )
    assert json.loads(stdout).get("ok") is True


# ---------------------------------------------------------------------------
# Branch 9 -- mutual exclusivity: positional params_json AND --params-file
# ---------------------------------------------------------------------------

def test_params_file_and_positional_params_json_are_mutually_exclusive(tmp_path):
    """Branch 9: passing BOTH the positional params_json AND --params-file
    is rejected via _fatal_stderr -- exit 1, error on STDERR, empty STDOUT.

    __main__.py's main() checks `args.params_file is not None and
    args.params_json is not None` before resolving either source, so this
    must fail fast rather than silently preferring one over the other.
    """
    params_path = tmp_path / "params.json"
    params_path.write_text("{}", encoding="utf-8")

    result = _invoke("ping", "{}", "--params-file", str(params_path))

    assert result.returncode == 1, (
        f"Passing both params_json and --params-file must exit 1; got "
        f"{result.returncode}.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        f"stdout must be empty on the mutual-exclusivity _fatal_stderr path; "
        f"got {result.stdout!r}"
    )
    assert result.stderr.strip(), "stderr must be non-empty on the mutual-exclusivity path"
    parsed_err = json.loads(result.stderr.strip())
    assert parsed_err.get("jsonrpc") == "2.0"
    assert "error" in parsed_err, f"Expected error envelope on stderr; got {parsed_err}"
    assert parsed_err["error"]["code"] == -32603
    assert "mutually exclusive" in parsed_err["error"]["message"].lower(), (
        f"Error message should mention mutual exclusivity; "
        f"got {parsed_err['error']['message']!r}"
    )


# ---------------------------------------------------------------------------
# Branch 10 -- --dump-op-timeouts
# ---------------------------------------------------------------------------

def test_dump_op_timeouts_emits_valid_json_with_default_and_overrides():
    """--dump-op-timeouts: no <op> required, exit 0, valid JSON on stdout.

    Asserts the exact stable shape: {"<op>": <float>, ..., "__default__": <float>}.
    "__default__" must be present. DEC-2 (docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md,
    commit 827cb8c8) retired the three ceremony.wsc_* per-op 120.0 overrides that this test
    used to assert -- _OP_TIMEOUT_OVERRIDES in coordinator_core/ipc.py is now an intentionally
    empty table, so every op (including the three former overrides) falls to the single global
    runaway guard. Assert the dump surface reflects exactly that retirement: the table is empty
    (no per-op keys beyond the reserved "__default__"), proving the dump surface reads the SAME
    source of truth _timeout_for() reads, not a hand-maintained duplicate that could drift stale
    in either direction.
    """
    result = _invoke("--dump-op-timeouts")

    assert result.returncode == 0, (
        f"--dump-op-timeouts must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stderr.strip() == "", (
        f"stderr must be empty on --dump-op-timeouts success; got {result.stderr!r}"
    )

    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict), f"Expected a JSON object; got {type(parsed).__name__}"
    assert "__default__" in parsed, f"Expected reserved '__default__' key; got keys {sorted(parsed.keys())!r}"
    assert isinstance(parsed["__default__"], float), (
        f"__default__ must be a float; got {parsed['__default__']!r} ({type(parsed['__default__']).__name__})"
    )

    assert parsed == {"__default__": parsed["__default__"]}, (
        "_OP_TIMEOUT_OVERRIDES is retired-empty (DEC-2) -- the dump must carry no per-op "
        f"override keys, only the reserved '__default__'; got {parsed}"
    )


def test_dump_op_timeouts_default_reflects_live_env_override():
    """--dump-op-timeouts __default__ must live-resolve COORDINATOR_DISPATCH_TIMEOUT_SECS,
    not a hardcoded 30.0 -- proving live resolution rather than a baked-in constant.
    """
    env = _make_env(COORDINATOR_DISPATCH_TIMEOUT_SECS="77")

    result = _invoke("--dump-op-timeouts", env=env)

    assert result.returncode == 0, (
        f"--dump-op-timeouts with an overridden env must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["__default__"] == 77.0, (
        f"__default__ must reflect the live COORDINATOR_DISPATCH_TIMEOUT_SECS=77 env override; "
        f"got {parsed['__default__']!r}. If this is 30.0, the surface hardcoded the default "
        f"instead of re-reading DISPATCH_TIMEOUT_SECS at call time."
    )


def test_dump_op_timeouts_requires_no_op_argument():
    """--dump-op-timeouts works with NO op argument at all -- the whole point of the
    nargs='?' relaxation on the positional `op` arg. Omitting --dump-op-timeouts
    entirely (no op, no flag) must still fail with the pre-existing 'op is required'
    contract, proving the relaxation didn't silently make op optional everywhere.
    """
    result = _invoke("--dump-op-timeouts")
    assert result.returncode == 0
    json.loads(result.stdout)  # must be valid JSON

    no_op_no_flag = _invoke()
    assert no_op_no_flag.returncode != 0, (
        "Omitting both <op> and --dump-op-timeouts must still fail -- op is "
        "only optional when --dump-op-timeouts is passed."
    )


def test_dump_op_timeouts_takes_priority_over_op_positional():
    """--dump-op-timeouts wins when an <op> positional is also passed.

    Review: code-reviewer (nit) -- the precedence ("flag wins, <op> is
    silently ignored") was previously undocumented and untested; this locks
    it in as intended behavior rather than incidental control flow.
    """
    result = _invoke("ping", "--dump-op-timeouts")

    assert result.returncode == 0, (
        f"--dump-op-timeouts combined with an <op> positional must still exit 0 "
        f"and dump timeouts; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert "__default__" in parsed, (
        f"Expected the timeout dump (not a ping dispatch result); got {parsed}"
    )


# ---------------------------------------------------------------------------
# Branch 11 -- _exit_code_for_response: success / transient-error / structural-error
# ---------------------------------------------------------------------------

def test_exit_code_for_response_success_is_zero():
    """No 'error' key → exit 0."""
    response = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert _exit_code_for_response(response, STRUCTURAL_PIN_ERROR) == 0


def test_exit_code_for_response_generic_error_is_one():
    """'error' present with a code other than STRUCTURAL_PIN_ERROR → exit 1 (soft/transient).

    Matches the pre-existing contract (module docstring Exit codes: 1) unchanged by the
    addition of exit code 2 — a generic op-level error must not shift to a new code.
    """
    response = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}}
    assert _exit_code_for_response(response, STRUCTURAL_PIN_ERROR) == 1


def test_exit_code_for_response_structural_pin_error_is_two():
    """'error' with code == STRUCTURAL_PIN_ERROR → exit 2 (loud/won't-self-heal).

    The distinct code this test suite exists to lock in: a structurally-wedged
    contract-pin failure (e.g. emit.cadence's CONTRACT_VERSION-vs-vendored-bundle
    desync) must be distinguishable from a generic exit-1 op error.
    """
    response = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": STRUCTURAL_PIN_ERROR, "message": "ContractPinError: desync"},
    }
    assert _exit_code_for_response(response, STRUCTURAL_PIN_ERROR) == 2
