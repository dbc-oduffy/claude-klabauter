"""
coordinator_core.benchmarks.timer -- Spawn-to-exit timing primitive.

Purpose: the single shared timing primitive for the latency benchmark harness.
Every consumer (C5's floor probe, C6's harness loop) imports `time_invocation`
by this exact module:function path -- no chunk re-declares or duplicates the
spawn-to-exit child-process loop.

Builds the entrypoint-under-test argv verbatim (see
coordinator_core.invoke.__main__ for the exact command form: the invoke
module run as a script, given <op> and '<json-params>', with an optional
--repo flag). `--repo` is omitted for the 6 bare/none-scoped ops (repo=None)
and passed for the 14 worktree-scoped ops (repo=<path>), per the scope split
pinned in coordinator_core.ipc.WORKTREE_SCOPED_OPS.

AC9 exit/error guard (load-bearing -- lives HERE so no consumer can omit it):
every sampled invocation asserts BOTH child exit code == 0 AND that stdout is
not a JSON-RPC error envelope (`{"error": ...}`). Either failure INVALIDATES
the sample and raises `BenchmarkSampleInvalid` -- the sample is never timed-
and-returned, never silently skipped. This is what separates a latency
harness from a latency-of-error-paths harness: a fast, stable erroring op
would otherwise gate GREEN under a `min` statistic.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C0
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Optional


# Review: code-reviewer (Slice B F6, nit) — single shared home for the
# Windows-popup-guard creationflags idiom; harness.py and op_fixtures.py import
# this instead of each re-declaring `getattr(subprocess, "CREATE_NO_WINDOW", 0)`.
SUBPROCESS_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Bounded timeout (seconds) for every subprocess.run call in this package —
# Review: code-reviewer (Slice B F3, P2) — a hung op must fail loud (AC9's
# "erroring op fails loud" spirit extends to "a hanging op fails loud"), not
# wedge the whole benchmark run indefinitely.
SUBPROCESS_TIMEOUT_S = 60


class BenchmarkSampleInvalid(RuntimeError):
    """
    Raised when a timed invocation's child process exits non-zero or returns
    a JSON-RPC error envelope. Carries enough context (op, returncode, stdout
    excerpt) for the caller to fail loud rather than silently drop the sample.
    """

    def __init__(self, op: str, returncode: int, stdout_excerpt: str) -> None:
        self.op = op
        self.returncode = returncode
        self.stdout_excerpt = stdout_excerpt
        super().__init__(
            f"benchmark sample invalid for op={op!r}: "
            f"returncode={returncode}, stdout_excerpt={stdout_excerpt!r}"
        )


def _build_argv(op: str, params_json: str, repo: Optional[str]) -> list[str]:
    """Builds the invoke argv, omitting --repo for bare/none-scoped ops."""
    argv = [sys.executable, "-m", "coordinator_core.invoke", op, params_json]
    if repo is not None:
        argv += ["--repo", repo]
    return argv


def time_invocation(op: str, params_json: str, repo: Optional[str]) -> float:
    """
    Times ONE spawn-to-exit invocation of the invoke entrypoint (op,
    params_json, optional repo) and returns elapsed wall time in
    milliseconds.

    Discarding warm-up runs is a caller-level concern (this function times a
    single invocation); callers that need N samples with warm-up discard
    should loop over this function themselves.

    Raises BenchmarkSampleInvalid (AC9) if the child process exits non-zero
    or its stdout is a JSON-RPC error envelope -- never returns a timing for
    an invalid sample.
    """
    argv = _build_argv(op, params_json, repo)

    start = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        # Review: code-reviewer (Slice B F3, P2) — a hung child process must
        # fail loud like any other invalid sample, not wedge the run forever.
        excerpt = (exc.stdout or "")[:500] if isinstance(exc.stdout, str) else ""
        raise BenchmarkSampleInvalid(op, -1, f"TIMEOUT after {SUBPROCESS_TIMEOUT_S}s: {excerpt}")
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    stdout = completed.stdout or ""
    error_envelope = False
    try:
        parsed = json.loads(stdout)
        # Review: code-reviewer (Slice C F2, nit) — a parsable-but-non-dict JSON
        # body (a bare list/scalar) is not a valid JSON-RPC envelope either; only
        # a dict without an "error" key is accepted.
        error_envelope = not isinstance(parsed, dict) or "error" in parsed
    except (json.JSONDecodeError, ValueError):
        # Review: code-reviewer (Slice A F1, nit) — rewritten to name the actual
        # mechanism: unparsable stdout is treated as an invalid sample regardless
        # of returncode. A healthy exit 0 always emits a parsable JSON-RPC
        # envelope, so a parse failure alone is sufficient grounds to invalidate
        # the sample -- it does not "route via the returncode check".
        error_envelope = True

    if completed.returncode != 0 or error_envelope:
        excerpt = stdout[:500]
        raise BenchmarkSampleInvalid(op, completed.returncode, excerpt)

    return elapsed_ms
