"""coordinator_core.ops.dispatch_emit.cli — the CLI half of
`emit-dispatch-workflow.py`.

Purpose: argument parsing, exit-code mapping, and the thin dispatch to the
op-registered `dispatch.emit` handler plus the engine-owned `restamp`
function — the door-served CLI leg `coordinator/bin/emit-dispatch-workflow.py`
delegates to (S1-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md).

Every real computation lives one layer down:
  - emission (plan or inventory path) is
    `coordinator_core.ops.dispatch_emit.op :: _dispatch_emit`, the SAME
    function the registered `dispatch.emit` op calls — this module invokes
    it in-process, never through a second JSON-RPC round trip, so a CLI
    emission and an op-driven emission of the same plan produce byte-
    identical output by construction (they run the identical call).
  - a re-stamp (`--restamp`) is `op.py :: restamp`, mirroring DoE-claude's
    prior `emit-dispatch-workflow.py :: restamp` wrapper (same refusal
    shape, same serialisation — see that function's own docstring).
  - firing (`--fire`) is `coordinator_core.ops.workflow_fire.fire ::
    fire_workflow` — the ONE spawn this CLI ever makes, and only on the
    `--fire` path; `--plan`, `--inventory`, and `--restamp` alone spawn
    nothing (S1-C7 AC).

This module owns nothing but argv parsing and the exit-code mapping over
those three functions' own return/raise contracts. It does NOT derive
waves, pathspecs, script text, or receipt shape itself.

Negative-spec:
  - Does NOT re-implement `_dispatch_emit`'s InventoryPathConflictError,
    ForeignEmissionError, or PathEscapeError refusals — those raise from
    the op function unchanged; this module only maps the exception class
    to an exit code and a stderr line.
  - Does NOT resolve relative `--plan`/`--inventory`/`--out`/`--restamp`
    paths against `--repo-root`. A relative path resolves against the
    caller's cwd (the door carries it, per the S1-C7 plan row) exactly as
    a bare `Path(...)` does — `--repo-root` feeds ONLY the op's
    `target_root`/prompt-anchoring `repo_root` parameter, a narrower and
    deliberately separate use (`op.py :: _repo_root_for_plan`,
    `contained_path`). On the plan route `--repo-root` is pure prompt
    anchoring and genuinely optional; on the queue route it is load-bearing
    for `--queue`/`--profile-dir` containment (`op.py ::
    QueueRootMissingError` fires without it) — treat the two routes'
    requirement on this flag as different, not one shared "optional" claim.
  - Does NOT invent a second session-identity resolution. `--restamp`
    resolves via `coordinator_core.session.core.resolve_session_id`, the
    same fleet-canonical ladder `op.py :: _receipt_session_id` reads —
    never a private env-var read.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk S1-C7.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.op import (
    ForeignEmissionError,
    ForeignSessionRestampError,
    InventoryPathConflictError,
    NoReceiptToRestampError,
    PathEscapeError,
    QueuePlanConflictError,
    _dispatch_emit,
    restamp,
)
from coordinator_core.ops.dispatch_emit.queue_emit import QueuePathEscapeError
from coordinator_core.session.core import resolve_session_id

#: Exit codes. 0 emission/restamp/fire succeeded; 1 a data/refusal error
#: (mutually-exclusive params, foreign emission, foreign restamp session,
#: no-receipt-to-restamp, a non-zero-ERROR emit verdict); 2 usage error
#: (missing required flag, unresolvable combination).
EXIT_OK = 0
EXIT_DATA_ERROR = 1
EXIT_USAGE = 2

# Exceptions `_dispatch_emit` and `restamp` raise as data/refusal errors —
# mapped to EXIT_DATA_ERROR, never re-derived here.
# A bare `ValueError` for a missing required param
# (e.g. `_dispatch_emit`'s `plan_path`/`output_path`/`profile_dir` checks)
# lands here as EXIT_DATA_ERROR even though this module's own pre-checks
# above return EXIT_USAGE for the identical logical error. Not reachable
# today (every required-param case is pre-checked before `_dispatch_emit`
# ever raises), but a future required param added on only one side would
# fire this latent taxonomy mismatch.
_DATA_ERRORS = (
    InventoryPathConflictError,
    QueuePlanConflictError,
    PathEscapeError,
    QueuePathEscapeError,
    ForeignEmissionError,
    NoReceiptToRestampError,
    ForeignSessionRestampError,
    ValueError,
)


class _ProfileDirUnresolved(Exception):
    """No ``--profile-dir`` given and the default location holds no such profile."""


def _default_profile_dir(profile: str) -> str:
    """``<coordinator content root>/queue-profiles`` — where the plugin payload
    carries the DoE-authored profiles every published command emits against
    without naming a directory. Refuses when ``<profile>.yaml`` is not there, so
    the error names the probed path rather than surfacing as a bare
    FileNotFoundError from ``load_profile``."""
    from coordinator_core.resolve_coordinator_clone import (
        ResolveCoordinatorCloneError,
        resolve_content_root,
    )

    try:
        content_root = resolve_content_root()
    except ResolveCoordinatorCloneError as exc:
        raise _ProfileDirUnresolved(
            f"--profile-dir omitted and no coordinator content root resolved: {exc}"
        ) from exc
    profile_dir = Path(content_root) / "queue-profiles"
    if not (profile_dir / f"{profile}.yaml").is_file():
        raise _ProfileDirUnresolved(
            f"--profile-dir omitted and {profile_dir / (profile + '.yaml')} does not exist; "
            "pass --profile-dir <dir holding the profile>"
        )
    return str(profile_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emit-dispatch-workflow",
        description=(
            "Emit one plan's task-spine as a fireable Workflow script, or "
            "re-stamp / fire an already-emitted one. Thin CLI over "
            "dispatch.emit."
        ),
    )
    parser.add_argument("--plan", default=None, help="plan file to read the task spine from")
    parser.add_argument(
        "--inventory",
        default=None,
        help="mise-inventory record to mint a spine FROM first, then emit",
    )
    parser.add_argument(
        "--restamp",
        default=None,
        metavar="SCRIPT",
        help="re-stamp an emission receipt's sha256 over SCRIPT after a deliberate edit",
    )
    parser.add_argument("--out", dest="out_path", default=None, help="path to write the emitted .mjs script to")
    parser.add_argument(
        "--queue",
        action="append",
        default=None,
        help="a queue directory (repeatable) -- the queue route, exclusive of --plan/--inventory",
    )
    parser.add_argument("--profile", default=None, help="queue-grind profile name (queue route)")
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="directory <profile>.yaml lives under (queue route; default: "
        "<coordinator content root>/queue-profiles)",
    )
    parser.add_argument(
        "--appetite", default="standard", help="queue-grind appetite preset (queue route)"
    )
    parser.add_argument(
        "--where", default=None, metavar="JSON", help="where override, JSON DNF (queue route)"
    )
    parser.add_argument(
        "--where-file",
        default=None,
        metavar="PATH",
        help="where override, read as JSON DNF from PATH (queue route, exclusive of --where)",
    )
    parser.add_argument("--limit", default=None, type=int, help="limit override (queue route)")
    parser.add_argument(
        "--budget-tokens",
        dest="budget_tokens",
        default=None,
        type=int,
        help="budget_tokens override (queue route)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an --out path already holding a different session's emission",
    )
    parser.add_argument(
        "--fire",
        action="store_true",
        help="fire the emitted script via workflow.fire after a successful emit",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root anchoring the op's containment/prompt resolution (default: none)",
    )
    return parser


def _do_restamp(script_arg: str) -> int:
    session_id = resolve_session_id() or ""
    try:
        receipt = restamp(Path(script_arg), session_id)
    except (NoReceiptToRestampError, ForeignSessionRestampError) as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return EXIT_OK


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.restamp:
        if args.plan or args.inventory or args.out_path or args.fire:
            print(
                "emit-dispatch-workflow: ERROR — --restamp is exclusive of "
                "--plan/--inventory/--out/--fire",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return _do_restamp(args.restamp)

    is_queue_route = bool(args.queue) or bool(args.profile)

    if is_queue_route and (args.plan or args.inventory):
        print(
            "emit-dispatch-workflow: ERROR — --queue/--profile is exclusive of "
            "--plan/--inventory",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if not is_queue_route and not args.plan and not args.inventory:
        print(
            "emit-dispatch-workflow: ERROR — one of --plan, --inventory, or "
            "--restamp is required",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.where and args.where_file:
        print(
            "emit-dispatch-workflow: ERROR — --where is exclusive of --where-file",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if (args.where or args.where_file) and not is_queue_route:
        print(
            "emit-dispatch-workflow: ERROR — --where/--where-file requires "
            "--queue/--profile (the queue route)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if is_queue_route and (not args.queue or not args.profile):
        print(
            "emit-dispatch-workflow: ERROR — the queue route requires --queue and --profile",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if is_queue_route and not args.profile_dir:
        try:
            args.profile_dir = _default_profile_dir(args.profile)
        except _ProfileDirUnresolved as exc:
            print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
            return EXIT_USAGE

    if not args.out_path:
        print("emit-dispatch-workflow: ERROR — --out is required", file=sys.stderr)
        return EXIT_USAGE

    repo_root = Path(args.repo_root).resolve() if args.repo_root else None

    params: dict = {"force": args.force, "output_path": args.out_path}
    if args.plan:
        params["plan_path"] = args.plan
    if args.inventory:
        params["inventory_path"] = args.inventory

    if is_queue_route:
        params["queue"] = args.queue
        params["profile"] = args.profile
        params["profile_dir"] = args.profile_dir
        params["appetite"] = args.appetite

        where = None
        try:
            if args.where:
                where = json.loads(args.where)
            elif args.where_file:
                where = json.loads(Path(args.where_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"emit-dispatch-workflow: ERROR — --where/--where-file is not "
                f"valid JSON: {exc}",
                file=sys.stderr,
            )
            return EXIT_USAGE

        overrides: dict = {}
        if where is not None:
            overrides["where"] = where
        if args.limit is not None:
            overrides["limit"] = args.limit
        if args.budget_tokens is not None:
            overrides["budget_tokens"] = args.budget_tokens
        if overrides:
            params["overrides"] = overrides

    try:
        result = _dispatch_emit(params, repo_root=repo_root)
    except _DATA_ERRORS as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR

    print(json.dumps(result, indent=2, sort_keys=True))

    if not result["ok"]:
        return EXIT_DATA_ERROR

    if args.fire:
        from coordinator_core.ops.workflow_fire.fire import fire_workflow

        fire_record = fire_workflow(
            result["path"], cwd=str(repo_root) if repo_root else None
        )
        print(json.dumps(fire_record, indent=2, sort_keys=True))

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
