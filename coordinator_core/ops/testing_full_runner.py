"""
coordinator_core.ops.testing_full_runner — JSON-RPC "testing.full_runner" operation.

Purpose: thin RPC wrapper that runs the C1 (`coordinator_core.testing.collect`) /
C2 (`coordinator_core.testing.run`) full cross-repo test-discovery-and-execution
engine against a caller-supplied `target_root`, returning a structured JSON
summary (`{families, passed, failed, suites, overall_ok}`) for programmatic
consumers — claude-klabauter's own emit/read-model surfaces (cockpit/fleet-state ingest)
are the likely first consumer. This op does not require a pre-wired consumer
to be well-formed.

Self-registration: importing this module calls register_op("testing.full_runner", ...)
as a side-effect (same pattern as ops/ping.py / ops/cartography_churn.py).
coordinator_core.ops.__init__ imports it so the registration fires eagerly.

Registration model (fleet-generic, coordinator-core-op-target-resolution-model,
docs/wiki/coordinator-core-engine.md § "Op classification, target resolution, and
testing discipline"): scope "none" + explicit REQUIRED `target_root` wire param —
mirrors the cartography.* / workflow.* / strategic.generate registration shape,
NOT a common_dir-resolved op. The handler ignores `repo_root`/`_origin_worktree`
entirely and resolves solely from the caller-supplied, guarded `target_root`.
It is NOT added to `ipc.WORKTREE_SCOPED_OPS` (that frozenset is derived from
`_OP_KEY_SCOPE` entries whose scope is "common_dir"/"show_top" — this op's
"none" scope entry keeps it out automatically).

Wire params:
    target_root  (str, REQUIRED)          — root of the repo tree to discover
                                             and run test suites against.
                                             Guarded via `_guard_target_root`
                                             (`coordinator_core.ops._path_guard`'s
                                             `contained_path`, plus an explicit
                                             literal-`..`-segment reject — see
                                             that helper's docstring) before any
                                             discovery/subprocess work happens.
    families     (list[str], optional)    — restrict discovery to a subset of
                                             `coordinator_core.testing.collect`'s
                                             four family keys; defaults to all four
                                             (`ALL_FAMILIES`).
    timeout      (int, optional)          — per-dispatched-unit subprocess
                                             timeout in seconds; defaults to
                                             `coordinator_core.testing.run.DEFAULT_TIMEOUT`.
    jobs         (int, optional)          — `ThreadPoolExecutor` worker cap
                                             forwarded to `run_suites`; defaults
                                             to that module's own CPU-derived cap.

Reply fields (result object in JSON-RPC response):
    families    (list[str]) — sorted distinct family keys present among the
                               discovered suites.
    passed      (int)       — count of suite entries with exit_code == 0.
    failed      (int)       — count of suite entries with exit_code != 0.
    suites      (list[dict]) — one entry per discovered `Suite`:
                               {"family": str, "path": str, "exit_code": int,
                                "duration": float}. Batched py-native suites
                               (DEC-5 — all `test_*.py` files run as a SINGLE
                               pytest invocation) each get their own entry but
                               share the one batched exit_code/duration, since
                               C2's engine does not expose a per-file result
                               within that batch.
    overall_ok  (bool)      — `coordinator_core.testing.run.overall_ok(results)`:
                               True iff every dispatched unit exited 0.
    Or `{"error": <str>}` if `target_root` is missing/invalid — this op reports
    errors as a structured field, never an exception that would surface only as
    a transport-level failure (mirrors ops/cartography_churn.py's error-return
    convention).

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md § C5 (DEC-9)

Negative-spec:
    - This op is explicitly NOT the `full_test_cmd` path. `full_test_cmd` uses
      the direct CLI invocation of `coordinator_core.testing.full_runner`
      (DEC-1/DEC-11), never this registered op's strangle_route. Routing
      `full_test_cmd` through this op's strangle_route path would green-wash
      failures: `cc_invoke` returns exit 0 on RPC-success regardless of the
      *content* of the JSON-RPC result, so a caller that only checks the
      transport exit code would see "0" even when every discovered suite
      failed. Consumers of this op MUST read the structured `overall_ok`/
      `failed` fields — the two-signal contract — never the transport exit code.
    - Does NOT rely on the transport exit code to carry the aggregate
      pass/fail verdict — `overall_ok`/`failed` are fields on the returned
      result object itself, computed the same way regardless of how the
      caller happens to invoke this op (RPC dispatch vs. direct handler call
      in a test).
    - Does NOT add `"testing.full_runner"` to `ipc.WORKTREE_SCOPED_OPS` — this
      op takes an explicit `target_root` param and needs no repo_root
      injection from the caller's own dispatching tree (fleet-generic scope
      "none" model, matching cartography.*/workflow.*/strategic.generate).
"""

from __future__ import annotations
import sys

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.testing.collect import ALL_FAMILIES, Suite, discover
from coordinator_core.testing.run import DEFAULT_TIMEOUT, SuiteResult, overall_ok, run_suites


def _guard_target_root(target_root_raw: str) -> Optional[Path]:
    """Resolve+guard a caller-supplied `target_root` string.

    Two layers, fail-closed at either:
      1. A literal `..` path segment anywhere in the RAW (pre-resolve) string
         is rejected outright. `contained_path`'s post-`.resolve()` check
         alone cannot catch this case — a traversal segment can legitimately
         resolve to *some* existing directory on disk (e.g.
         `/repo/sub/../../etc` resolves cleanly to `/etc`), so a resolve-then-
         check-containment-against-itself guard would never fire. Rejecting
         the literal token first closes that gap. (Full `safe_id` per-segment
         validation is deliberately NOT used here — its allowlist charset
         (`[A-Za-z0-9._-]+`) is narrower than what real repo directory names
         may legitimately contain, e.g. spaces; applying it to every segment
         of an arbitrary fleet-generic `target_root` would reject valid paths,
         not just traversal attempts.)
      2. `contained_path(resolved, [resolved])` re-affirms the resolved path
         (post-symlink-resolution) is exactly what it claims to be and exists;
         also confirms the result is a directory. `contained_path` fails
         closed on any `OSError` during resolution.

    Returns the guarded, resolved `Path`, or `None` if either layer rejects.
    """
    raw = Path(target_root_raw)
    non_anchor_parts = raw.parts[1:] if raw.is_absolute() else raw.parts
    if ".." in non_anchor_parts:
        return None

    try:
        resolved = raw.resolve(strict=True)
    except OSError:
        print(f"skip: _guard_target_root: resolved = raw.resolve(strict=True) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    if not resolved.is_dir():
        return None

    return contained_path(resolved, [resolved])


def _summarize(suites: list[Suite], results: list[SuiteResult]) -> tuple[list[dict], int, int]:
    """Build the `suites` reply entries + `(passed, failed)` counts.

    Non-py-native suites map 1:1 to their own `SuiteResult` (matched by
    identity). The single batched py-native `SuiteResult` (`suite is None`,
    DEC-5) is broadcast to every py-native `Suite`'s entry — see module
    docstring "Reply fields" § suites for why per-file attribution within
    that batch isn't available.
    """
    py_native_result = next((r for r in results if r.suite is None), None)
    per_suite_result = {id(r.suite): r for r in results if r.suite is not None}

    entries: list[dict] = []
    for suite in suites:
        result = per_suite_result.get(id(suite)) if suite.runner_kind != "pytest" else py_native_result
        if result is None:
            continue
        entries.append(
            {
                "family": suite.family,
                "path": str(suite.path),
                "exit_code": result.exit_code,
                "duration": result.duration,
            }
        )

    passed = sum(1 for e in entries if e["exit_code"] == 0)
    failed = len(entries) - passed
    return entries, passed, failed


@register_op("testing.full_runner")
def _testing_full_runner(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "testing.full_runner" handler.

    Params: see module docstring "Wire params".

    Returns: see module docstring "Reply fields", or `{"error": <str>}` if
    `target_root` is missing/invalid.
    """
    target_root_raw = params.get("target_root")
    if not target_root_raw:
        return {"error": "testing.full_runner requires a non-empty target_root param"}

    guarded_root = _guard_target_root(str(target_root_raw))
    if guarded_root is None:
        return {
            "error": f"invalid target_root: {target_root_raw!r} "
            "(traversal segment, non-existent path, or non-directory rejected)"
        }

    families_raw = params.get("families")
    if isinstance(families_raw, list) and families_raw:
        families = frozenset(str(f) for f in families_raw)
    else:
        families = ALL_FAMILIES

    timeout_raw = params.get("timeout")
    timeout = int(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT

    jobs_raw = params.get("jobs")
    jobs = int(jobs_raw) if jobs_raw is not None else None

    suites = discover(guarded_root, families=families)
    results = run_suites(suites, guarded_root, timeout=timeout, jobs=jobs)

    suite_entries, passed, failed = _summarize(suites, results)

    return {
        "families": sorted({s.family for s in suites}),
        "passed": passed,
        "failed": failed,
        "suites": suite_entries,
        "overall_ok": overall_ok(results),
    }
