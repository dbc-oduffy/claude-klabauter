"""
coordinator_core.ops.scan_unresolved_ubt_records — unresolved-UBT-marker scanner.

Purpose: read-only scan of ``state/review-trail/`` for ``*.ubt-compile.pending.json``
markers that lack a sibling ``*.resolved.json`` file — the blocking gate that decides
whether an unreviewed-build-timeout (UBT) compile marker is still outstanding.

Port source: ``find state/review-trail -name *.ubt-compile.pending.json``, for each
check sibling ``.resolved.json`` absence (docs/plans/2026-07-22-coordinator-ops-
buildout-from-fence-inventory.md § Wave 2; oracle fence: commands/workweek-complete.md:456).

Marker pairing convention: a pending marker ``<stem>.ubt-compile.pending.json`` is
resolved once a sibling file ``<stem>.resolved.json`` exists in the SAME directory.
``<stem>`` is the marker filename with the ``.ubt-compile.pending.json`` suffix
stripped — the two files share every character up to that suffix.

Registered as ``review_trail.scan_unresolved_ubt`` (params: ``{}`` -> ``{"unresolved":
list[str]}``). Scope: ``common_dir`` (state/review-trail is main-worktree-rooted, same
class as ``review_trail.write`` — table entry owned by a separate serial tail chunk;
this module's handler derives ``caller_worktree`` the same way ``review_trail.write``'s
handler does so it behaves correctly the moment that entry lands).

Idempotency (AC7): pure read, no filesystem mutation — a second invocation against
unchanged disk state returns the identical list. Manifest rates idempotency-hazard and
platform-hazard both "none"; no DEC-7 note is required for this op.

``main(argv)`` (below) is the CLI entrypoint the `d-run-ubt-pending-check` directive
(`directives_review.build_ubt_pending_check_directive`) dispatches, via the thin
`coordinator/bin/scan_unresolved_ubt_records.py` trampoline — mirrors the
`ops.blocked`/`ops.dispatch_shape_classify` bin/ops split already used for every other
CONSUMES_MANIFEST member that ships as a bareword-argv CLI over a pure op function.

Negative-spec:
    - Does NOT write, create, or delete any file — read-only scan, stdout/return only.
    - Does NOT scan ``archive/review-trail`` — the oracle fence scans only the live
      ``state/review-trail`` tree; archived markers are, by definition, already
      resolved-or-superseded by the archival ceremony.
    - Does NOT treat an absent ``state/review-trail`` directory as an error — mirrors
      the sibling review-trail readers' fresh-install-safe absent-dir handling.
    - Does NOT shell out to ``find``/``comm``/bash — ``Path.rglob`` replaces the find
      call uniformly across platforms (DEC-2, naked-Python mandate).
    - ``main()``'s ``--since`` flag is accepted but NOT used to filter results —
      ``scan_unresolved_ubt_records`` has no timestamp/SHA-scoped filtering
      capability; see ``main()``'s own docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root

_PENDING_SUFFIX = ".ubt-compile.pending.json"
_RESOLVED_SUFFIX = ".resolved.json"


def scan_unresolved_ubt_records(caller_worktree: Path) -> List[str]:
    """Return sorted absolute paths of unresolved UBT-compile pending markers.

    A marker ``<dir>/<stem>.ubt-compile.pending.json`` is unresolved when
    ``<dir>/<stem>.resolved.json`` does not exist alongside it.

    Absent-dir-safe: returns ``[]`` when ``state/review-trail`` does not exist under
    ``caller_worktree`` (fresh-install case, mirrors the sibling review-trail readers).
    """
    review_trail_dir = caller_worktree / "state" / "review-trail"
    if not review_trail_dir.is_dir():
        return []

    unresolved: List[str] = []
    for pending in review_trail_dir.rglob(f"*{_PENDING_SUFFIX}"):
        if not pending.is_file():
            continue
        stem = pending.name[: -len(_PENDING_SUFFIX)]
        resolved_sibling = pending.with_name(f"{stem}{_RESOLVED_SUFFIX}")
        if not resolved_sibling.exists():
            unresolved.append(str(pending))

    unresolved.sort()
    return unresolved


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("review_trail.scan_unresolved_ubt")
def _scan_unresolved_ubt_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``review_trail.scan_unresolved_ubt`` handler.

    Read-only (no `_OP_KEY_SCOPE` write side effects). ``repo_root`` receives
    ``git_common_dir(caller_worktree)`` via ipc.py when the ``common_dir`` scope entry
    is registered (separate tail chunk); handler derives the worktree via
    ``main_worktree_root(repo_root)`` before any path construction, mirroring
    ``review_trail.write``'s handler. When ``repo_root`` is None (op not yet in the
    scope table, or genuinely no repo context), returns an empty list rather than
    guessing a worktree.

    Params: none.

    Returns:
        {"unresolved": list[str]} — absolute paths of unresolved UBT-compile pending
        markers, sorted ascending.
    """
    if repo_root is None:
        return {"unresolved": []}
    caller_worktree = main_worktree_root(repo_root)
    unresolved = scan_unresolved_ubt_records(caller_worktree)
    return {"unresolved": unresolved}


# ---------------------------------------------------------------------------
# CLI entrypoint — dispatched by `d-run-ubt-pending-check` via
# `coordinator/bin/scan_unresolved_ubt_records.py`
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    """CLI entrypoint for the `d-run-ubt-pending-check` directive.

    Usage: ``scan_unresolved_ubt_records.py --mode pending --since <sha>``

    ``--mode`` only recognizes ``pending`` — the sole caller today
    (`directives_review.build_ubt_pending_check_directive`); any other value is a
    usage error. ``--since`` is accepted (the directive always supplies it) but is
    NOT used to filter results — `scan_unresolved_ubt_records` is a pure
    pending/resolved-sibling scan over the full ``state/review-trail`` tree with no
    timestamp/SHA-scoped filtering capability; narrowing by ``--since`` is out of
    this CLI's scope (see module docstring's Negative-spec).

    Resolves the caller's worktree from cwd via `pickup_assemble.resolve_repo_root`
    (the same zero-spawn `.git` read-model `workstream_complete.brief()` itself
    uses) rather than requiring an explicit `--repo-root` flag — this CLI is always
    invoked from within the ceremony's own worktree.

    Prints each unresolved marker's absolute path on its own line to stdout.

    NON-BLOCKING BY DESIGN: finding unresolved markers is the pass's expected,
    normal outcome, not a failure. Per the pre-conversion SKILL body (git
    `4f58613b^:coordinator/skills/workstream-complete/SKILL.md`, the UBT
    pending-marker step): "Captures the build verdict as a deferred record;
    resolution happens at `/workday-complete` Step 0c." The scan's job is to
    CAPTURE the record here, not resolve it — resolution is a separate,
    later ceremony. Exiting non-zero on a hit would make `apply.py`'s
    per-directive halt contract read this expected capture as
    `DIRECTIVE_FAILED` and block `/workstream-complete` from completing on
    any repo carrying a deferred marker, inverting a deliberately-deferred
    check into a ceremony-stopper. Do not "fix" this back to blocking.

    Exit codes:
        0 — scan completed, regardless of whether unresolved markers were
            found; any found markers are printed to stdout as the captured
            record.
        2 — usage error (missing flag value, unrecognized argument, an
            unsupported `--mode` value, or an unresolvable repo root).
    """
    mode: Optional[str] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--mode":
            if i + 1 >= len(argv):
                print("scan_unresolved_ubt_records: --mode requires a value", file=sys.stderr)
                return 2
            mode = argv[i + 1]
            i += 2
        elif tok == "--since":
            if i + 1 >= len(argv):
                print("scan_unresolved_ubt_records: --since requires a value", file=sys.stderr)
                return 2
            i += 2  # accepted, not used for filtering — see docstring
        else:
            print(f"scan_unresolved_ubt_records: unrecognized argument {tok!r}", file=sys.stderr)
            return 2

    if mode != "pending":
        print(
            f"scan_unresolved_ubt_records: unsupported --mode {mode!r} (only 'pending' is implemented)",
            file=sys.stderr,
        )
        return 2

    # Deferred import: coordinator_core.pickup_assemble transitively imports
    # coordinator_core.ops, whose eager-load loop imports this module back —
    # a module-level import here creates a circular import that de-registers
    # this op whenever pickup_assemble is imported before ops.
    from coordinator_core.pickup_assemble import resolve_repo_root

    caller_worktree = resolve_repo_root()
    if caller_worktree is None:
        print("scan_unresolved_ubt_records: not inside a git worktree", file=sys.stderr)
        return 2

    unresolved = scan_unresolved_ubt_records(caller_worktree)
    for path in unresolved:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
