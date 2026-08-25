"""
coordinator_core.ops.coordinator_render_rollup — direct-call render helper backing
coordinator/bin/coordinator-render-rollup.sh (DoE sh/python polyglot trampoline).

Purpose: ports the CLI orchestration logic of the DoE bash render helper (arg
parsing, transport-seam invocation, TWO-SIGNAL-style fail-open, omit-on-empty
render) into makima as a plain callable module. ``main(argv)`` is called by the
DoE trampoline exactly once per invocation and calls the ALREADY-REGISTERED
"deliverable.rollup" op handler (coordinator_core.ops.deliverable_rollup._handler)
directly, in-process — no subprocess re-spawn of coordinator_core.invoke, no
JSON-RPC envelope round-trip. This is the same direct-import shape used by
coordinator_core.hooks.auto_push / coordinator_core.ops.handoff_gate_aging: the
DoE caller is itself Python (the polyglot trampoline), so an in-process call is
strictly cheaper than shelling back out through the command-type transport.

The command-type transport (coordinator_core.invoke / cc_invoke) explicitly does
not enforce auth/authz on this path (see coordinator-core-invoke.sh's docstring,
DoE c6d97219 2026-07-22: "Does NOT read or require any auth token — the command
path bypasses IPC auth"), so calling the handler directly is not a weaker-auth shortcut
relative to the bash-era behavior.

Not a JSON-RPC op itself: no ``register_op`` call here, no registry-map/ipc/authz
entries needed — same "plain module, direct import" shape as
coordinator_core.ops.handoff_gate_aging.

Fail-open at every stage past argument parsing — never a hard error for this
render helper. The only non-zero exit is a missing/empty required CLI argument
(mirrors the original bash's ``${1:?...}``/``${2:?...}`` unbound-variable
convention, which also fires on an empty string, not just an absent argument).

Port source: coordinator/bin/coordinator-render-rollup.sh (kept — converted to a
sh/python polyglot trampoline over this module; filename/`.sh` suffix unchanged
so existing callers need zero edits).
Spec backlink: docs/plans/2026-07-06-deliverable-rollup-render-and-fk-population.md § AC3

Negative-spec (hard-won):
  - Does NOT re-implement deliverable.rollup's scan/resolve logic — calls the
    existing registered op handler verbatim. Any bug already living in that
    handler (e.g. a malformed state/initiatives/<id>.yaml file that makes the
    shared YAML loader return a non-dict) surfaces here as a caught Exception
    -> fail-open skip, exactly as it did through the old
    bash -> cc_invoke -> coordinator_core.invoke -> JSON-RPC-error path. Do NOT
    "fix" that upstream bug from this module — it is out of this port's scope.
  - ``resolution_mode`` must be exactly "direct" to render — any other value
    (a future §5.2 "transitive" widen) is a silent skip, matching the vendored
    contract's direct-only v1.0 scope.
  - Dedup by initiative id (first-occurrence-wins) is ALREADY performed by the
    op handler itself (coordinator_core/ops/deliverable_rollup.py); this module
    re-dedups defensively at render time (matches the old bash script's own
    belt-and-braces dedup) rather than trusting the handler's invariant blindly.
  - Does NOT write any file, does NOT call git, does NOT mutate any state —
    pure read + stdout render, matching the original bash helper's COMPUTE_ONLY
    consumer role.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Sequence


def _fail_open(deliverable_id: str, message: str) -> int:
    """Log a skip diagnostic to stderr and return the fail-open exit code (0)."""
    print(
        f"coordinator-render-rollup: {message} for deliverable_id={deliverable_id} (skip)",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entry point: ``main(["<deliverable_id>", "<repo_root>"]) -> exit code``.

    Mirrors the original bash helper's contract exactly:
      - Missing or empty ``deliverable_id``/``repo_root`` -> usage error, exit 1
        (the only non-zero exit path; matches ``${1:?...}``/``${2:?...}``).
      - Any failure past argument parsing (repo resolution, op import, op
        exception, malformed result shape) -> fail-open skip: diagnostic on
        stderr, no stdout, exit 0.
      - ``resolution_mode != "direct"`` -> silent skip, exit 0, no stdout.
      - Empty ``advances_initiatives`` -> silent skip, exit 0, no stdout
        (omit-on-empty is the common case, not a warning).
      - Otherwise: one "advances initiative <label> (<id>)" line per distinct
        initiative id, in first-occurrence order, exit 0.
    """
    if len(argv) < 2 or not argv[0] or not argv[1]:
        print(
            "coordinator-render-rollup: <deliverable_id> and <repo_root> are required "
            "(usage: coordinator-render-rollup.sh <deliverable_id> <repo_root>)",
            file=sys.stderr,
        )
        return 1

    deliverable_id = argv[0]
    repo_root_arg = argv[1]

    try:
        from coordinator_core.lifecycle import git_common_dir
    except Exception as exc:  # noqa: BLE001 — engine import failure, fail-open per convention
        return _fail_open(deliverable_id, f"engine import failed ({exc})")

    try:
        common_dir = git_common_dir(Path(repo_root_arg).resolve())
    except Exception as exc:  # noqa: BLE001 — not-a-git-repo / git failure, fail-open
        return _fail_open(deliverable_id, f"repo_root resolution failed ({exc})")

    try:
        from coordinator_core.ops.deliverable_rollup import _handler
    except Exception as exc:  # noqa: BLE001 — op module import failure, fail-open
        return _fail_open(deliverable_id, f"op module import failed ({exc})")

    try:
        result = _handler({"deliverable_id": deliverable_id}, repo_root=common_dir)
    except Exception as exc:  # noqa: BLE001 — mirrors the old transport fail-closed skip
        return _fail_open(deliverable_id, f"op raised {type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return _fail_open(deliverable_id, "op returned a non-dict result")

    if result.get("resolution_mode") != "direct":
        return 0

    advances = result.get("advances_initiatives") or []
    if not isinstance(advances, list):
        return 0

    # scan_incomplete: additive optional field, absent == False. Not yet emitted
    # by deliverable_rollup._handler (frozen v1.0 wire shape, see
    # test_handler_payload_wire_shape_omits_scan_incomplete) -- this widens the
    # render side ahead of the writer flip per the widen-before-flip sequencing.
    # Spec backlink: cross-repo memo 2026-07-22 (project-makima-em),
    # "deliverable-rollup contract: propose additive scan_incomplete field".
    # Review: code-reviewer -- Finding 8 (nit): `is True` rather than `bool(...)`
    # so a future non-bool truthy sentinel (e.g. a string) fails to trip
    # "(partial scan)" silently -- strict bool-only semantics once the writer flips.
    scan_incomplete = result.get("scan_incomplete", False) is True

    lines: List[str] = []
    seen = set()
    for item in advances:
        if not isinstance(item, dict):
            continue
        iid = item.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        label = item.get("label") or iid
        line = f"advances initiative {label} ({iid})"
        if scan_incomplete:
            line += " (partial scan)"
        lines.append(line)

    if lines:
        print("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
