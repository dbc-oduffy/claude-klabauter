"""publish-emission.py — CLI trampoline over the emission.publish engine op.

No shebang line: like this directory's other generator-owned two-leg entrypoints (a
tracked `.cmd`/`.ps1` sibling pair via `coordinator/bin/gen-launcher-shim.py`), this file
is invoked as `python3 publish-emission.py`, never as a bare word — no exec-bit/shebang
launch path exists for it, so none is asserted here.

Purpose: cockpit's boundary-roster trigger for `emission.publish` (AC8,
`coordinator_core/ops/emission_publish.py`) — the named CLI trampoline `docs/reference/
boundary-and-data-planes.md` names as this op's own invocation surface, matching every
existing roster entry's "never a direct op call from their process" shape
(`query-record-history.py` et al.).

AC11 negative-spec, load-bearing: this trampoline's argument parser accepts NO path,
destination, or body parameter -- its whole argument surface is the absence of one. That
is what keeps this narrow enough to sit on the boundary roster (see the plan's § Trigger
model): a future `--emission-path`/`--url`/`--body` flag "for a perfectly good local
reason" would widen the caller-steerable surface this boundary argument rests on, and the
negative-spec test in `coordinator_core/ops/emit/tests/test_emission_publish_op.py`
exists to turn that red rather than let it land silently.

IMPORT DISCIPLINE, load-bearing and measured (§ Performance plan, DR-344): this module
MUST NOT import `coordinator_core.ops` (directly or transitively at module scope) --
measured child CPU 343.8ms against DR-344's 50ms warm-engine bar, vs. 31.2ms for the
`cc_invoke`-only shape this file uses. `coordinator_core.ops.emission_publish` (and the
whole op registry) is reached ONLY inside the already-running warm engine process, on the
far side of `cc_invoke.route_mutation()`'s transport -- never imported here.

`emission.publish` is a MUTATING op, this roster's first -- unlike the four existing
COMPUTE_ONLY roster trampolines (`query-record-history.py` et al., which all call
`cc_invoke.route()`), this one calls `cc_invoke.route_mutation()`. `route()` returns the
bare result dict on transport success and does not raise on an op-level refusal (the
DR-215 exit_code trap); `route_mutation()` inspects the successful transport result for an
in-envelope exit_code/failed/error and raises `RouteMutationError` on a refusal -- e.g. a
transport failure inside C3 (`publish_transport.PublishTransportError`) -- so it surfaces
here as a non-zero exit rather than a printed success.

Spec backlink: docs/plans/2026-08-21-emission-publish-producer.md § C4 (AC8, AC11)

Usage:
    python3 publish-emission.py

Exit codes:
    0 — publish succeeded.
    2 — publish failed (transport failure, refused op, or engine-seam absence);
        stderr names the failure.

Negative-spec: does NOT invoke bash, sh, or any shell — subprocess spawning lives
entirely inside `cc_invoke.route_mutation()`. Does NOT accept `--emission-path`, `--out`,
`--url`, `--body`, or any other caller-steerable argument (AC11). Does NOT import
`coordinator_core.ops` at module scope (see IMPORT DISCIPLINE above).
"""
from __future__ import annotations

import argparse
import os
import sys

_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import cc_invoke  # noqa: E402
from records_query import _resolve_repo_root  # noqa: E402


def _no_legacy() -> None:
    """State-1 fallback — `emission.publish` has no bash predecessor.

    Raises unconditionally; `cc_invoke.route_mutation()` wraps the raise in the
    standardized four-rung remediation message on State-1 (seam absent).
    """
    raise RuntimeError("publish-emission: native seam required (no bash fallback)")


def _build_parser() -> argparse.ArgumentParser:
    # AC11: no path, destination, or body argument — deliberately empty argument
    # surface. Kept as a parser (not a bare `sys.argv` check) so `--help` still works
    # and so the negative-spec test can assert against `_build_parser()` directly.
    return argparse.ArgumentParser(prog="publish-emission.py")


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(argv) if argv is not None else sys.argv[1:]

    parser = _build_parser()
    parser.parse_args(effective_argv)

    repo_root = _resolve_repo_root()

    try:
        result = cc_invoke.route_mutation("emission.publish", {}, repo_root, _no_legacy)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: any failure -> diagnostic + exit 2
        print(f"publish-emission: emission.publish invocation failed: {exc}", file=sys.stderr)
        return 2

    repo_slug = result.get("repo_slug") if isinstance(result, dict) else None
    doc_id = result.get("doc_id") if isinstance(result, dict) else None
    print(f"publish-emission: published {repo_slug!r} (doc_id={doc_id!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
