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
  3. _origin_worktree injection — worktree-scoped op (a test-owned probe) run
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
# `_WORKTREE_SCOPED_PROBE` / `_PROBE_EXPECTED_ERROR` lived here: a production op
# borrowed as a worktree-scope vehicle, plus the error string that stood in for a
# witness. Both cases that used them now drive `_WORKTREE_SCOPED_PROBE_SCRIPT`,
# which owns its op and reports the root directly.


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


#: Breadcrumbs the engine emits on stderr that are CONFIGURATION notices, not the log
#: noise the "stderr must be empty" assertions exist to catch. Each is a once-per-process
#: line stating a deliberate operator setting, and whether it appears depends on the box
#: rather than on the code under test -- so asserting a literally empty stderr made those
#: cases pass or fail on where they ran. `[warm-settings]` fires on any machine with
#: warmth disabled, which is a supported configuration and turns every dispatching case
#: in this module red for a reason none of them is about.
#:
#: Deliberately a prefix ALLOWLIST, not a regex over the whole stream: an unrecognised
#: line is still a failure, which is the property these assertions are for.
_BENIGN_STDERR_PREFIXES = ("[warm-settings]",)


def _stderr_noise(result: subprocess.CompletedProcess) -> str:
    """`result.stderr` with the benign configuration breadcrumbs removed."""
    return "\n".join(
        line
        for line in result.stderr.splitlines()
        if line.strip() and not line.startswith(_BENIGN_STDERR_PREFIXES)
    ).strip()


def _invoke(*args: str, cwd: str | Path | None = None, env: dict | None = None,
            timeout: int = 30) -> subprocess.CompletedProcess:
    """Spawn coordinator_core.invoke as a subprocess and return the CompletedProcess.

    ``--allow-unstamped-dispatch`` is appended to every DISPATCHING call, for the
    same reason ``conftest.py``'s ``pytest_configure`` calls
    ``ipc.allow_unstamped_dispatch()`` for the in-process path and
    ``test_plan_tasks_mutate::_invoke_cli`` passes it for its own subprocess leg:
    this repo IS the dev tree, never the published stamped mirror, so a bare
    subprocess dispatch hits ipc.py's stamp gate before reaching any handler. The
    gate landed after this module was written and every dispatching case here has
    been red against it since -- asserting the stamp gate rather than the
    entrypoint behaviour each case exists to cover. This is the sanctioned
    "deliberate manual testing" carve-out the gate's own refusal message names.

    NOT appended to a flag-only call (``--dump-op-timeouts``, or the no-argument
    case): those never reach dispatch, several of them assert stderr is empty, and
    one asserts that omitting an op still fails. Passing an irrelevant flag there
    would be testing argparse, not the surface.

    Args:
        *args:   Arguments after ``python -m coordinator_core.invoke``.
        cwd:     Working directory for the subprocess (default: claude-klabauter repo root).
        env:     Environment dict (default: _make_env()).
        timeout: subprocess.run timeout in seconds (default: 30).
    """
    dispatching = bool(args) and not args[0].startswith("-")
    cmd = [sys.executable, "-m", "coordinator_core.invoke", *args]
    if dispatching and "--allow-unstamped-dispatch" not in args:
        cmd.append("--allow-unstamped-dispatch")
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

    assert _stderr_noise(result) == "", (
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

    assert _stderr_noise(result) == "", (
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

    Behavioral assertion: the op returns a JSON-RPC response on stdout (not a
    fatal pre-dispatch error on stderr) carrying the repo_root the handler was
    actually handed. That is a direct witness that resolution and injection both
    happened, rather than the indirect one the borrowed vehicles allowed (an
    error string reachable only after a root resolved).

    Vehicle note, and the reason there is no longer a production op named here.
    This drove `coverage.gate` until that op's cost scaled with the review-trail
    corpus (48s, past the engine's own dispatch timeout); it was repointed at
    `handoff.has_live_children`, which has since been KILLED for a budget breach
    (`-32006`), leaving this case asserting a suspension notice. Twice the test
    reported on its vehicle rather than on `invoke`.

    The scope class was always the only property it needed, so the vehicle is now
    test-owned: `_WORKTREE_SCOPED_PROBE_SCRIPT` registers a handler, enters
    `WORKTREE_SCOPED_OPS` by rebinding that name in `ipc` before `main()` reads
    it, and echoes back the `repo_root` it received. Do NOT repoint this at a
    production op again -- the borrowed-vehicle failure has a two-for-two record.

    Contrast with test_none_scoped_outside_git_tree: a none-scoped op never calls
    _resolve_repo_root(), so running OUTSIDE a git tree still exits 0.
    """
    # Run from the claude-klabauter repo root — git rev-parse will succeed here.
    result = _worktree_scope_probe()

    assert result.returncode == 0, (
        f"the worktree-scope probe must exit 0; got {result.returncode}.\n"
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
    assert parsed["result"]["repo_root"] == str(_PROJECT_ROOT), (
        f"the handler must have been handed the resolved repo root; got {parsed}"
    )

    # stderr must be empty — no fatal pre-dispatch error.
    assert _stderr_noise(result) == "", (
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
        assert _stderr_noise(result) == "", (
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

    It also accepted `returncode in (0, 1)` and asserted only that the envelope was
    JSON, which is a green this case could reach WITHOUT `--repo` doing anything —
    and did: it passed against the killed vehicle's `-32006` suspension envelope.
    The owned probe reports the root it was handed, so `--repo` is now actually
    under test.
    """
    result = _worktree_scope_probe("--repo", str(_PROJECT_ROOT))

    assert result.returncode == 0, (
        f"the worktree-scope probe with --repo must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    assert result.stdout.strip(), "stdout must be non-empty (JSON-RPC response)"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert parsed["result"]["repo_root"] == str(_PROJECT_ROOT), (
        f"--repo must be the root the handler receives; got {parsed}"
    )
    assert _stderr_noise(result) == "", (
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
      - A test-owned handler that `asyncio.to_thread`s a `time.sleep`, under
        COORDINATOR_DISPATCH_TIMEOUT_SECS=0.001, so the internal wait_for fires
        while a real executor thread is still live — which is the precondition
        the regression is about, not merely a slow op.
      - subprocess.run(..., timeout=15) — if the process hangs, TimeoutExpired
        is raised and the test fails.
      - Assert result.returncode is not None (process exited, not killed by us).
      - Assert stdout is a JSON-RPC response with the expected timeout error.

    VEHICLE OWNED, NOT BORROWED, and that is the fix this case had been waiting
    for. It drove `coverage.gate` until K-001 deleted that op, after which it
    asserted "timed out" against `Method not found` and went red — the second
    time a borrowed vehicle took this module down (see
    `test_worktree_scoped_op_dispatches_inside_repo`, whose own replacement op
    has since been killed for a budget breach). The reviewer nit recorded here
    called for exactly this and deferred it "to avoid coupling this test to
    test-only op registration infrastructure"; that infrastructure is now used
    three times over in this same module, so the coupling cost is already paid
    and the deferral only bought two outages. It also removes the probabilistic
    arm the nit named: the sleep cannot finish inside 1ms, so the timeout branch
    is asserted unconditionally rather than "if by fluke".

    Negative-spec: if asyncio.run() were used instead of the manual loop, this
    test would raise subprocess.TimeoutExpired because shutdown_default_executor()
    joins the live thread — blocking until the thread finishes (unbounded time).
    The 15s outer timeout is the regression guard.
    """
    env = _make_env(COORDINATOR_DISPATCH_TIMEOUT_SECS="0.001")

    try:
        result = _run_probe_script(
            _SLOW_OP_SCRIPT, env=env, timeout=15  # outer guard — must exit well within 15s
        )
    except subprocess.TimeoutExpired:  # pragma: no cover
        raise AssertionError(
            "C3 regression: subprocess did NOT exit within 15s with "
            "COORDINATOR_DISPATCH_TIMEOUT_SECS=0.001 — os._exit hang suspected.  "
            "This is the hang that a manual loop + os._exit must prevent."
        )

    # Process exited — returncode must be set (not None).
    assert result.returncode is not None, "returncode must be set after normal exit"

    # The handler sleeps far past 1ms, so the timeout branch is the only reachable
    # one — no "if by fluke it completed" arm, which is what made this probabilistic.
    assert result.returncode == 1, (
        f"Expected exit 1 after the dispatch timeout; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # stdout must be valid JSON — the response was emitted and flushed before os._exit.
    assert result.stdout.strip(), "stdout must contain the JSON-RPC response"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert "error" in parsed, f"the op must have timed out, not succeeded; got {parsed}"
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
    assert _stderr_noise(result) == "", (
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
         "--params-file", "-", "--bare", "--allow-unstamped-dispatch"],
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

    Empty stdin (json.loads("")
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

    A str payload piped via
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
         "--params-file", "-", "--bare", "--allow-unstamped-dispatch"],
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
    empty table, so every op that is not otherwise projected falls to the single global
    runaway guard.

    THE OVERRIDE TABLE IS NOT THE WHOLE DUMP, and asserting it was is how this test spent
    three surface changes red. It read `parsed == {"__default__": ...}` -- exact equality
    against an empty override table -- so the ceremony projection, `__ceremony_budget__`, and
    the transport-deadline rows each made it fail without any of them being wrong. A red that
    fires on every correct change stops being read, and this one guards a payload two sibling
    repos size their kill ceilings from.

    What it asserts now is the property the equality was reaching for: every per-op row present
    is one the engine PROJECTED on purpose, and the retired overrides did not come back. The
    reserved rows are checked by name; the projected ceremony rows must equal the ceremony
    budget; nothing else may appear. `test_dump_op_timeouts_projects_the_transport_deadline`
    covers the `__ceremony__<op>` rows' own semantics.
    """
    result = _invoke("--dump-op-timeouts")

    assert result.returncode == 0, (
        f"--dump-op-timeouts must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert _stderr_noise(result) == "", (
        f"stderr must be empty on --dump-op-timeouts success; got {result.stderr!r}"
    )

    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict), f"Expected a JSON object; got {type(parsed).__name__}"
    assert "__default__" in parsed, f"Expected reserved '__default__' key; got keys {sorted(parsed.keys())!r}"
    assert isinstance(parsed["__default__"], float), (
        f"__default__ must be a float; got {parsed['__default__']!r} ({type(parsed['__default__']).__name__})"
    )

    from coordinator_core.ipc import CEREMONY_BUDGET_SECS, is_ceremony_method

    reserved = {
        "__default__",
        "__ceremony_budget__",
        "__ceremony_mutation_read_deadline__",
        "__warm_miss_wait__",
    }
    for key, value in parsed.items():
        if key in reserved or key.startswith("__ceremony__"):
            continue
        assert is_ceremony_method(key), (
            "_OP_TIMEOUT_OVERRIDES is retired-empty (DEC-2), so the only per-op rows the dump "
            f"may carry are the ceremony projection; {key!r} is neither reserved nor a ceremony "
            f"op. Full payload: {parsed}"
        )
        assert value == CEREMONY_BUDGET_SECS, (
            f"projected ceremony op {key!r} must carry the ceremony budget, not {value!r}"
        )

    assert parsed["__ceremony_budget__"] == CEREMONY_BUDGET_SECS
    assert set(parsed) >= reserved, f"missing reserved rows; got {sorted(parsed)}"


def test_dump_op_timeouts_projects_the_transport_deadline():
    """The `__ceremony__<op>` rows: the engine ASSERTING ceremony membership, and
    carrying the deadline a delivered mutation is read for.

    Two things are wrong without them, both measured 2026-09-20. An external caller
    sizes its subprocess kill ceiling off this dump (`cc_invoke::_op_timeout_ceiling`),
    while the engine's own warm client keeps reading the answer to a mutation it has
    already put on the wire for `ipc.mutation_read_deadline_for(op)` -- deliberately
    NOT ceremony-clamped, because abandoning a delivered commit turns a slowness report
    into an unknown-whether-it-committed. Publishing only the 2s performance budget gave
    the parent a 4s ceiling over a child waiting 30s, so `WARM_DISPATCH_INDETERMINATE`
    was unreachable on exactly the ops that commit.

    And membership cannot be re-derived client-side. `is_ceremony_method` is a union of
    prefix, alias table, and owning module; `cc_invoke` may not import `ipc` (asyncio on
    the thin client's cold path) and its prefix test misses `commit.exec_bit_change` and
    `review.snapshot_diff_and_head` -- one of which commits. The row's PRESENCE is the
    answer to membership, its VALUE the answer to the deadline.
    """
    from coordinator_core.ipc import (
        CEREMONY_BUDGET_SECS,
        is_ceremony_method,
        mutation_read_deadline_for,
    )

    result = _invoke("--dump-op-timeouts")
    assert result.returncode == 0
    parsed = json.loads(result.stdout)

    projected = [k for k in parsed if is_ceremony_method(k)]
    assert projected, "the dump projects no ceremony ops at all -- the loop is dead"

    for op in projected:
        row = f"__ceremony__{op}"
        assert row in parsed, (
            f"{op!r} is projected at the ceremony budget but carries no {row!r} row, so a "
            "caller reading this dump cannot tell it is a ceremony op OR how long its own "
            "child will wait"
        )
        assert parsed[row] == mutation_read_deadline_for(op)
        assert parsed[row] > parsed[op], (
            "the transport deadline must exceed the performance budget -- equal is the "
            "collapse this projection exists to prevent"
        )

    # The two ops whose membership a prefix test cannot see. Their presence here is the
    # whole reason membership is published rather than mirrored.
    for alias in ("commit.exec_bit_change", "review.snapshot_diff_and_head"):
        assert not alias.startswith("ceremony.")
        assert f"__ceremony__{alias}" in parsed, (
            f"{alias!r} is a ceremony op by alias, and the only signal a prefix-matching "
            "client has for it is this row"
        )

    assert parsed["__ceremony_mutation_read_deadline__"] > CEREMONY_BUDGET_SECS


def test_dump_op_timeouts_default_reflects_live_env_override():
    """--dump-op-timeouts __default__ must live-resolve COORDINATOR_DISPATCH_TIMEOUT_SECS,
    not a hardcoded 30.0 -- proving live resolution rather than a baked-in constant.

    NARROWING, and only narrowing. This case asserted 77 until 2026-09-20 and had been
    red ever since `_resolve_dispatch_timeout_secs` became the sole env seam and clamped
    it `min(requested, DISPATCH_TIMEOUT_SECS)`: 77 is a WIDENING, so it resolves to 30
    by design, and the test was demanding the one outcome the clamp exists to forbid. A
    red that can only be cleared by reintroducing the defect proves nothing about live
    resolution, so both directions are asserted here instead -- the narrowing value must
    land, and the widening one must be inert.
    """
    result = _invoke("--dump-op-timeouts", env=_make_env(COORDINATOR_DISPATCH_TIMEOUT_SECS="0.5"))

    assert result.returncode == 0, (
        f"--dump-op-timeouts with an overridden env must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["__default__"] == 0.5, (
        f"__default__ must reflect a live NARROWING COORDINATOR_DISPATCH_TIMEOUT_SECS=0.5; "
        f"got {parsed['__default__']!r}. If this is 30.0, the surface dumped the "
        f"DISPATCH_TIMEOUT_SECS constant instead of calling the resolver, and every caller "
        f"reading this dump is sizing its ceiling 60x above what the engine will allow."
    )

    widened = _invoke("--dump-op-timeouts", env=_make_env(COORDINATOR_DISPATCH_TIMEOUT_SECS="77"))
    assert widened.returncode == 0
    assert json.loads(widened.stdout)["__default__"] == 30.0, (
        "a widening override must be inert -- `_resolve_dispatch_timeout_secs` clamps "
        "`min(requested, DISPATCH_TIMEOUT_SECS)`, and live resolution must not become a "
        "route around that"
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

    The precedence ("flag wins, <op> is
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


# ---------------------------------------------------------------------------
# Branch 14 -- stdout transport hardening: a handler print() must not corrupt
# the JSON-RPC envelope on stdout.
#
# Live incident this pins: coordinator_core/ops/plan_tasks_mutate.py's
# _resolve() calls close_out_and_stamp._stamp_plan_landed(...) in-process,
# which unconditionally print()s a status line. That line landed on the same
# stdout stream cc_invoke parses as JSON, breaking every
# coordinator/bin/ CLI built on cc_invoke with "invoke stdout is not valid
# JSON". main()'s dispatch loop must capture ANY handler-level stdout write
# and relay it to stderr, never letting it interleave with the envelope.
#
# A throwaway op is registered directly in the SAME subprocess that runs
# main() (via `python -c`, not `python -m coordinator_core.invoke`) — main()
# calls os._exit so it cannot be exercised in-process from THIS test process,
# but the registration + main() call can still share one child process.
# ---------------------------------------------------------------------------

_PRINT_OP_SCRIPT = """
import sys
from coordinator_core import ipc

async def _noisy(params, repo_root=None):
    print("stray diagnostic line from a handler")
    return {"ok": True}

ipc.register_op("test.stdout_hardening_probe", _noisy)

sys.argv = ["coordinator_core.invoke", "test.stdout_hardening_probe", "{}", "--allow-unstamped-dispatch"]
from coordinator_core.invoke.__main__ import main
main()
"""

_RAISING_OP_SCRIPT = """
import sys
from coordinator_core import ipc

async def _boom(params, repo_root=None):
    print("stray diagnostic line before the raise")
    raise RuntimeError("deliberate handler failure")

ipc.register_op("test.stdout_hardening_raise_probe", _boom)

sys.argv = ["coordinator_core.invoke", "test.stdout_hardening_raise_probe", "{}", "--allow-unstamped-dispatch"]
from coordinator_core.invoke.__main__ import main
main()
"""


#: The worktree-scope vehicle, OWNED. `WORKTREE_SCOPED_OPS` is a frozenset computed
#: from `_OP_KEY_SCOPE` at import, and `main()` reads it from `ipc` at call time, so a
#: probe can enter the class by rebinding that name before calling `main()` -- without
#: touching the production table or depending on any production op continuing to exist.
#:
#: The handler REPORTS the `repo_root` it was handed, which is a direct witness that
#: resolution and `_origin_worktree` injection both happened. The borrowed-op versions
#: could only infer it: they asserted a specific handler error string that was merely
#: unreachable without a resolved root, and each died with its vehicle.
_WORKTREE_SCOPED_PROBE_SCRIPT = """
import sys
from coordinator_core import ipc

_OP = "test.worktree_scope_probe"

async def _echo_repo_root(params, repo_root=None):
    return {"repo_root": str(repo_root) if repo_root is not None else None}

ipc.register_op(_OP, _echo_repo_root)

# Two tables, because they answer different halves. `main()` reads
# WORKTREE_SCOPED_OPS to decide whether to resolve a root and inject
# `_origin_worktree`; `ipc.resolve_op_repo_key` reads the PRIVATE `_OP_KEY_SCOPE`
# (not the public re-export) to decide what repo_root the HANDLER is handed.
# Patching only the first injects a field nothing reads back.
#
# "show_top" rather than "common_dir" so the handler receives the worktree path
# itself; common_dir would hand it `<root>/.git` and the assertion would be about
# git's layout rather than about resolution.
from coordinator_core import op_scopes

ipc._OP_KEY_SCOPE = {**ipc._OP_KEY_SCOPE, _OP: "show_top"}
ipc.WORKTREE_SCOPED_OPS = frozenset(op_scopes.WORKTREE_SCOPED_OPS) | {_OP}

sys.argv = ["coordinator_core.invoke", _OP, "{}", "--allow-unstamped-dispatch"] + EXTRA_ARGV
from coordinator_core.invoke.__main__ import main
main()
"""


def _worktree_scope_probe(*extra_argv: str) -> subprocess.CompletedProcess:
    """The worktree-scope probe, optionally with extra argv (e.g. ``--repo``)."""
    argv = "EXTRA_ARGV = " + repr(list(extra_argv)) + "\n"
    return _run_probe_script(argv + _WORKTREE_SCOPED_PROBE_SCRIPT)


#: Sleeps on a real executor thread, which is the precondition the C3 regression is
#: about: `asyncio.to_thread` work still live when the internal `wait_for` gives up.
#: A handler that merely `await asyncio.sleep`s would be cancelled cleanly and would
#: never exercise the omitted `shutdown_default_executor()` this test guards.
_SLOW_OP_SCRIPT = """
import asyncio
import sys
import time
from coordinator_core import ipc

async def _slow(params, repo_root=None):
    await asyncio.to_thread(time.sleep, 2)
    return {"ok": True}

ipc.register_op("test.dispatch_timeout_probe", _slow)

sys.argv = ["coordinator_core.invoke", "test.dispatch_timeout_probe", "{}", "--allow-unstamped-dispatch"]
from coordinator_core.invoke.__main__ import main
main()
"""


def _run_probe_script(
    script: str, env: dict | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run a probe script that registers its own op and then calls `main()`.

    A test-OWNED vehicle. Cases here that borrowed a production op as a vehicle
    have gone red twice when that op was deleted or killed, each time asserting
    something about the borrowed op rather than about `invoke`.
    """
    cmd = [sys.executable, "-c", script]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=_PROJECT_ROOT,
        env=env if env is not None else _make_env(),
        creationflags=_NO_CONSOLE,
    )


def test_handler_stdout_print_relayed_to_stderr_not_corrupting_envelope():
    """A handler that print()s during dispatch still yields a stdout stream
    that is exactly one parseable JSON-RPC envelope; the printed line appears
    on stderr instead of being lost or interleaved onto stdout.
    """
    result = _run_probe_script(_PRINT_OP_SCRIPT)

    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert parsed.get("result", {}).get("ok") is True

    assert "stray diagnostic line from a handler" in result.stderr, (
        f"handler stdout must be relayed to stderr, not discarded; got "
        f"stderr={result.stderr!r}"
    )
    assert "stray diagnostic line from a handler" not in result.stdout, (
        f"handler stdout must never reach the real stdout stream; got "
        f"stdout={result.stdout!r}"
    )


def test_handler_raise_still_yields_error_envelope_on_stdout():
    """The error-envelope path is unchanged by the stdout-capture hardening:
    a handler that raises still produces a JSON-RPC error response on
    stdout, and any stdout it printed before raising is relayed to stderr.
    """
    result = _run_probe_script(_RAISING_OP_SCRIPT)

    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert "error" in parsed, f"expected a JSON-RPC error envelope; got {parsed}"

    assert "stray diagnostic line before the raise" in result.stderr
    assert "stray diagnostic line before the raise" not in result.stdout


_PRINT_OP_BROKEN_STDERR_SCRIPT = """
import sys
from coordinator_core import ipc

async def _noisy(params, repo_root=None):
    print("stray diagnostic line from a handler")
    return {"ok": True}

ipc.register_op("test.stdout_hardening_broken_stderr_probe", _noisy)

class _BrokenStderr:
    def write(self, s):
        raise BrokenPipeError("simulated closed stderr")
    def flush(self):
        raise BrokenPipeError("simulated closed stderr")

sys.stderr = _BrokenStderr()

sys.argv = ["coordinator_core.invoke", "test.stdout_hardening_broken_stderr_probe", "{}", "--allow-unstamped-dispatch"]
from coordinator_core.invoke.__main__ import main
main()
"""


def test_handler_stdout_relay_raising_still_yields_envelope_on_stdout():
    """A relay write that raises (e.g. BrokenPipeError from a caller that
    closed stderr early) must not prevent the JSON-RPC envelope from being
    printed to stdout -- the relay is best-effort, the envelope is not.
    """
    cmd = [sys.executable, "-c", _PRINT_OP_BROKEN_STDERR_SCRIPT]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_PROJECT_ROOT,
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )

    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"
    assert parsed.get("result", {}).get("ok") is True
