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
  - a re-stamp (`--restamp`) is `op.py :: restamp`, mirroring coordinator-content-repo's
    prior `emit-dispatch-workflow.py :: restamp` wrapper (same refusal
    shape, same serialisation — see that function's own docstring).
  - firing (`--fire`) is `coordinator_core.ops.workflow_fire.fire ::
    fire_workflow` — the ONE spawn this CLI ever makes, and only on the
    `--fire` path; `--plan`, `--inventory`, `--ask`, `--sizing`, and
    `--restamp` alone spawn nothing (S1-C7 AC). Guarded immediately before that spawn by
    `dispatch_emit.op.guard_against_fired_drift`, called with
    `result["sha256"]` (`_dispatch_emit`'s own reply digest, never a
    re-read or re-hashed copy) — refuses a fire whose on-disk script
    changed between this call's emit and its own `--fire` branch (a peer
    overwrote the deterministic emission path in between). Coordinator-content-repo
    parity: `emit-dispatch-workflow.py :: _guard_against_fired_drift`,
    called the same way at its own `fire()` wrapper.

`--inventory --lanes` runs the same `_dispatch_emit` with `lanes=True`: it emits
one script per ready part and prints one `Workflow(...)` line each.

This module owns nothing but argv parsing, the exit-code mapping over
those three functions' own return/raise contracts, and one added step:
running load-aware admission (`admission.await_admission`) before emission
on the plan/inventory/queue routes, merging the returned record into the
printed JSON (docs/plans/2026-09-27-load-aware-workflow-admission.md, C4).
It does NOT derive waves, pathspecs, script text, or receipt shape itself.

Negative-spec:
  - Does NOT pick a sizing's arm: `--ask`/`--sizing` forward to `_dispatch_emit`,
    which composes the one script that sizes, gates and routes in-session;
    `--fire` is refused with either.
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
    QueueRootMissingError` fires without a resolved root) — treat the two
    routes' requirement on this flag as different, not one shared
    "optional" claim. an OMITTED `--repo-root` on the queue route
    is no longer automatically `QueueRootMissingError` — `main()` defaults
    it from cwd's own `.git` ancestor first (`_default_repo_root_from_cwd`,
    mirroring `op.py :: _repo_root_for_plan`'s walk), so the refusal is now
    reached only from a cwd with no `.git` ancestor at all, same as before
    for that narrower case.
  - Does NOT invent a second session-identity resolution. `--restamp`
    resolves via `coordinator_core.session.core.resolve_session_id`, the
    same fleet-canonical ladder `op.py :: _receipt_session_id` reads —
    never a private env-var read.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk S1-C7.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.op import (
    ForeignEmissionError,
    ForeignSessionRestampError,
    InventoryPathConflictError,
    NoReceiptToRestampError,
    PathEscapeError,
    QueuePlanConflictError,
    SizingPathConflictError,
    _dispatch_emit,
    restamp,
)
from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    PipelineEmitRefused,
    subject_key,
    subject_slug,
)
from coordinator_core.ops.dispatch_emit.pipeline_inputs import (
    STRUCTURED_SUFFIXES,
    read_structured_file,
    subjects_from_value,
)
from coordinator_core.ops.dispatch_emit.emit import ScriptOverCapError
from coordinator_core.ops.dispatch_emit.inventory_mint import NothingUnlandedError
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, SizingHandBack
from coordinator_core.ops.dispatch_emit.mark_landed import (
    NoEmbeddedCommitPhaseError,
    PhaseNotFoundError,
    mark_landed_and_restamp,
)
from coordinator_core.ops.dispatch_emit.grind_admission import NOT_ADMITTED_EXTRA_KEY
from coordinator_core.ops.dispatch_emit.queue_emit import QueuePathEscapeError
from coordinator_core.session.core import resolve_session_id

_REQUIRED_OUT_SUFFIX = ".workflow.mjs"

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
    SizingPathConflictError,
    SizingFireRefused,
    PipelineEmitRefused,
    PathEscapeError,
    QueuePathEscapeError,
    ForeignEmissionError,
    NoReceiptToRestampError,
    ForeignSessionRestampError,
    ValueError,
)


class _ProfileDirUnresolved(Exception):
    pass


def _default_profile_dir(profile: str) -> str:
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
        "--ask",
        nargs="?",
        const=True,
        default=None,
        metavar="PROMPT",
        help="emit one in-session Workflow from a raw ask (PROMPT), or from an "
        "existing sizing with --sizing",
    )
    parser.add_argument(
        "--sizing",
        default=None,
        help="existing sizing under state/sizings/; the same entry as --ask --sizing",
    )
    parser.add_argument(
        "--baton",
        default=None,
        metavar="PATH",
        help="existing baton under state/handoffs/ to join the sizing to (--ask/--sizing only)",
    )
    parser.add_argument(
        "--deliverable-id",
        default=None,
        metavar="ID",
        help="deliverable id the baton and sizing share (--ask/--sizing only)",
    )
    parser.add_argument(
        "--writes",
        action="append",
        default=None,
        metavar="PATH",
        help="a file in the ask's footprint (repeatable; --ask/--sizing only); "
        "a path in a sibling checkout is a cross-repo write (see --cross-repo-approved); "
        "a path outside every checkout is refused at emit",
    )
    parser.add_argument(
        "--restamp",
        default=None,
        metavar="SCRIPT",
        help="re-stamp an emission receipt's sha256 over SCRIPT after a deliberate edit",
    )
    parser.add_argument(
        "--resume-from",
        default=None,
        metavar="CONTINUANCE",
        help="re-emit every lane inventory beside a mise continuance record "
        "(state/mise-inventory/<run-id>-continuance.md), leaving out each row whose "
        "plan spine row is already coded; --out is the base, each lane writes "
        "<base>-<lane>.workflow.mjs",
    )
    parser.add_argument(
        "--only-incomplete",
        default=None,
        metavar="RUN_TEXT",
        help="with --plan: re-emit only the rows no `checkpoint(wave N): ... \u2014 ids` "
        "commit subject in RUN_TEXT (a git log dump or run output) names as landed; "
        "edges onto landed rows count as satisfied",
    )
    parser.add_argument(
        "--hold",
        default=None,
        metavar="ROW[,ROW...]",
        help="with --plan: leave these rows, and every row depending on one, out of the waves "
        "without touching the plan; the digest lists them incomplete. Needs --hold-reason. "
        "Re-emit with --only-incomplete once the hold clears",
    )
    parser.add_argument(
        "--hold-reason",
        default=None,
        metavar="TEXT",
        help="why the --hold rows wait; recorded in the .emitted.json receipt and the digest",
    )
    parser.add_argument(
        "--review-only",
        nargs="?",
        const="",
        default=None,
        metavar="RUN_TEXT",
        help="with --plan or --inventory, and --run-base: emit a script that only reviews and tests the rows "
        "named by --rows and by RUN_TEXT's `checkpoint(wave N): ... — ids` subjects; with neither, "
        "the plan's `coded` rows (--inventory requires one of the two). Reviews the diff from --run-base to the worktree; "
        "no row, commit or push step is emitted",
    )
    parser.add_argument(
        "--rows",
        default=None,
        metavar="IDS",
        help="with --review-only: comma-separated row ids to review (e.g. C1,C2); coded rows allowed",
    )
    parser.add_argument(
        "--run-base",
        default=None,
        metavar="SHA",
        help="with --review-only: the run's base commit (7-40 lowercase hex digits)",
    )
    parser.add_argument(
        "--reverify-delivery",
        default=None,
        metavar="RUN_RECORD",
        help="with --plan (--out defaults to <plan>.workflow.mjs beside the plan): "
        "emit a one-stage script re-running only the delivery verifier at HEAD "
        "over RUN_RECORD's frozen delivery FAIL; record its result with "
        "`python -m coordinator_core.ops.dispatch_emit.reverify_delivery record`; "
        "an empty value resolves the record a warp run leaves at "
        ".coordinator-local/subagent-share/*/*.review-wave-bookkeeping.md whose plan_id matches",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="with --plan and --out: emit a script firing only the criterion judge at HEAD over the "
        "plan's stamped unmet review_stamp criterion; record its result with "
        "`python -m coordinator_core.ops.dispatch_emit.reverify_delivery record --result-json <task output>`, "
        "then `review-stamp.py rejudge --plan <plan>`",
    )
    parser.add_argument(
        "--mark-landed",
        dest="mark_landed_phase",
        default=None,
        metavar="PHASE_TITLE",
        help="resume a halted commit phase: splice its `phase: 'PHASE_TITLE...'` "
        "step to a literal `COMMIT-LANDED <sha>` and re-stamp -- requires "
        "--sha and a positional SCRIPT",
    )
    parser.add_argument(
        "--sha",
        default=None,
        help="commit sha to mark as landed (with --mark-landed)",
    )
    parser.add_argument(
        "script_positional",
        nargs="?",
        metavar="SCRIPT",
        help="the emitted script to edit, with --mark-landed",
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
    parser.add_argument(
        "--max-rows",
        dest="max_rows",
        default=None,
        type=int,
        help="refuse an --inventory minting more live rows than this (default 100)",
    )
    parser.add_argument(
        "--row-budget",
        "--tranche",
        dest="row_budget",
        default=None,
        type=int,
        help="--inventory: emit the next whole plans (dependency order) fitting N rows "
        "instead of refusing; the rest are reported as deferred",
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
        "--lanes",
        action="store_true",
        help="with --inventory: partition into write-disjoint lanes and byte-bounded "
        "parts, pin <run-id>.lanes.json, and emit one script per ready part",
    )
    parser.add_argument(
        "--part", default=None, help="with --lanes: emit only this part (refuses when not ready)"
    )
    parser.add_argument(
        "--lane-count",
        dest="lane_count",
        default=None,
        type=int,
        help="with --lanes: concurrent lane count when the lane map is first written (default 3)",
    )
    parser.add_argument(
        "--hot-files",
        dest="hot_files",
        default=None,
        type=int,
        help="with --lanes: how many most-shared files form the hub lane (default 40)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an --out path already holding a different session's emission",
    )
    parser.add_argument(
        "--cross-repo-approved",
        action="store_true",
        help="plan/ask route: the PM approved this run's sibling-repo writes (the "
        "approve_cross_repo_write touchpoint); implied on a remote venue",
    )
    parser.add_argument(
        "--chatty",
        action="store_true",
        help="plan route: compose an opt-in chatty workflow (roster, mailbox briefs, overseer wake stage)",
    )
    parser.add_argument(
        "--fire",
        action="store_true",
        help="fire the emitted script via workflow.fire after a successful emit",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root anchoring the op's containment/prompt resolution (default: the "
        "queue route derives one by walking up from cwd to the nearest .git; the "
        "plan/inventory route stays unanchored unless given explicitly)",
    )
    parser.add_argument(
        "--box-terms",
        dest="box_terms_path",
        default=None,
        metavar="FILE",
        help="the box's binding constraints, plain text one term per line; appended to every "
        "dispatched brief as `Box terms (from the driver; binding)` and recorded in the "
        "emission receipt and the plan script's meta.boxTerms",
    )
    parser.add_argument(
        "--preamble",
        dest="preamble_path",
        default=None,
        metavar="FILE",
        help="a run-wide posture block rendered once into every executor "
        "prompt this emission composes; its path and sha256 are recorded in the "
        "emission receipt",
    )
    parser.add_argument(
        "--pipeline",
        default=None,
        metavar="NAME",
        help="pipeline route: emit the named manifest-driven pipeline over the --brief file path "
        "and --subjects; emit-only, exclusive of every other route selector",
    )
    parser.add_argument(
        "--brief", default=None, metavar="PATH",
        help="pipeline route: path to the brief file (repo-relative or absolute; must exist)",
    )
    parser.add_argument(
        "--subjects",
        default=None,
        metavar="VALUE",
        help="pipeline route: a file of subject keys (one per line, # comments), a comma list, or a "
        ".json/.yaml/.yml file holding a subjects list (strings or objects) or a research spec "
        "(subjects plus topics, which become each subject's verifiers)",
    )
    parser.add_argument(
        "--list",
        action="append",
        default=None,
        metavar="NAME=a,b,c",
        help="pipeline route: a manifest input list (repeatable); NAME=@FILE reads a JSON/YAML list; "
        "a roster entry is slug=agent_type",
    )
    parser.add_argument(
        "--scratch-dir", default=None, metavar="PATH", help="pipeline route: repo-relative scratch dir"
    )
    parser.add_argument(
        "--from-sizing",
        dest="from_sizing",
        default=None,
        metavar="PATH",
        help="research route: emit the research pipelines the sizing's research block shapes to; "
        "emit-only, exclusive of every other route selector",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help="with --ask PROMPT: the scouts express lane -- write the ask to <scratch>/ask.md and "
        "emit the scouts pipeline over it (--list questions=a,b names up to two scout questions)",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=None,
        metavar="FILE",
        help="research route: a non-corpus file every member reads first, named in the brief (repeatable)",
    )
    parser.add_argument(
        "--resume-missing",
        action="store_true",
        help="pipeline route: re-emit only the fan-out elements whose expected output is missing or empty "
        "under --scratch-dir (resume after a usage-limit stop)",
    )
    parser.add_argument(
        "--validator",
        default=None,
        metavar="CMD",
        help="pipeline route: a one-line validator command; every manifest stage naming {{validator}} "
        "is emitted (per batch when it fans over a list) and filled with it; refused when no stage names it",
    )
    parser.add_argument(
        "--flag",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="pipeline route: a manifest flag (repeatable)",
    )
    return parser


def _load_subjects(value: str) -> "list":
    """Subject keys or objects: a keys file (one per line), a comma list, or a JSON/YAML subjects list or spec."""
    path = Path(value)
    if path.is_file() and path.suffix.lower() in STRUCTURED_SUFFIXES:
        subjects = subjects_from_value(read_structured_file(path), base=path.parent)
    else:
        raw = path.read_text(encoding="utf-8").splitlines() if path.is_file() else value.split(",")
        subjects = [k.strip() for line in raw for k in [line.split("#", 1)[0]] if k.strip()]
    if not subjects:
        raise PipelineEmitRefused([f"--subjects yields no subjects: {value}"])
    keys = [
        subject_key(s) for s in subjects
        if isinstance(s, str) or (isinstance(s, dict) and isinstance(s.get("subject"), str))
    ]
    if len({subject_slug(k) for k in keys}) != len(keys):
        raise PipelineEmitRefused(
            ["--subjects has duplicate (or slug-colliding) keys; scratch dirs would collide"]
        )
    return subjects


def _parse_lists(items: "list[str]") -> "dict[str, list]":
    lists: "dict[str, list]" = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name or not value:
            raise PipelineEmitRefused([f"--list {item!r} is not NAME=a,b,c or NAME=@FILE"])
        if value.startswith("@"):
            loaded = read_structured_file(Path(value[1:]))
            if not isinstance(loaded, list):
                raise PipelineEmitRefused([f"--list {name}: {value[1:]} must hold a list"])
            lists[name] = loaded
        else:
            lists[name] = [v.strip() for v in value.split(",") if v.strip()]
    return lists


def _parse_flags(items: "list[str]") -> "dict[str, str]":
    flags: "dict[str, str]" = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise PipelineEmitRefused([f"--flag {item!r} is not NAME=VALUE"])
        flags[name] = value
    return flags


def _default_repo_root_from_cwd() -> "Optional[Path]":
    try:
        here = Path.cwd()
    except OSError:
        return None
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _do_mark_landed(script_arg: "Optional[str]", phase_title: str, sha: "Optional[str]") -> int:
    if not script_arg:
        print(
            "emit-dispatch-workflow: ERROR — --mark-landed requires a positional "
            "SCRIPT argument",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not sha:
        print(
            "emit-dispatch-workflow: ERROR — --mark-landed requires --sha",
            file=sys.stderr,
        )
        return EXIT_USAGE
    session_id = resolve_session_id() or ""
    try:
        receipt = mark_landed_and_restamp(Path(script_arg), phase_title, sha, session_id)
    except (
        NoEmbeddedCommitPhaseError,
        PhaseNotFoundError,
        NoReceiptToRestampError,
        ForeignSessionRestampError,
    ) as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR
    except OSError as exc:
        print(f"emit-dispatch-workflow: ERROR — cannot read/write {script_arg!r}: {exc}",
              file=sys.stderr)
        return EXIT_DATA_ERROR
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return EXIT_OK


def _do_restamp(script_arg: str) -> int:
    session_id = resolve_session_id() or ""
    try:
        receipt = restamp(Path(script_arg), session_id)
    except (NoReceiptToRestampError, ForeignSessionRestampError) as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return EXIT_OK


def _print_workflow_invocation(
    result: dict,
    *,
    is_queue_route: bool,
    profile_dir: "Optional[str]" = None,
    repo_root: "Optional[Path]" = None,
) -> None:
    """Print the exact ``Workflow({...})`` call to make against the just-
    written script, to stderr, on EVERY route.

    the skills that document this emitter claim it prints this
    invocation. It printed nothing at all on the queue route -- the only
    way to find the required fire-time args (``run_stamp``, ``script_path``,
    ``profile_dir``, ``repo_root``, per ``grind_compose._FIRE_ARGS_CHECK``) was to read the
    emitted script's own guard. This runs unconditionally after a successful
    emit, on both routes, so the printed line is never route-dependent.

    The queue route's ``run_stamp`` is a fire-time run id, never part of the
    emitted script or its digest, so the printed call carries the UTC instant
    of this print: a paste-ready call, not a placeholder to hand-fill. The
    clock is read here, in the CLI, never in the emitter.
    """
    script_path = result.get("path")
    if not script_path:
        return
    if is_queue_route:
        run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(
            "\n  Workflow({ scriptPath: "
            f"{json.dumps(script_path)}, args: {{ run_stamp: '{run_stamp}', "
            f"script_path: {json.dumps(script_path)}, profile_dir: "
            f"{json.dumps(profile_dir)}, repo_root: "
            f"{json.dumps(repo_root.as_posix() if repo_root else None)} }} }})",
            file=sys.stderr,
        )
    else:
        fire_args = result.get("fire_args")
        if fire_args:
            print(
                f"\n  Workflow({{ scriptPath: {json.dumps(script_path)}, args: "
                f"{json.dumps(fire_args)} }})",
                file=sys.stderr,
            )
        else:
            print(
                f"\n  Workflow({{ scriptPath: {json.dumps(script_path)} }})   # no args",
                file=sys.stderr,
            )


def _part_out_path(out_path: str, index: int) -> str:
    out = Path(out_path)
    stem = out.name[: -len(_REQUIRED_OUT_SUFFIX)]
    return str(out.with_name(f"{stem}-p{index}{_REQUIRED_OUT_SUFFIX}"))


def _emit_inventory_parts(
    params: dict,
    repo_root: "Optional[Path]",
    over: ScriptOverCapError,
    fire: bool,
    admission_record: dict,
) -> int:
    """Cut an over-cap inventory into whole-plan parts, each its own
    `<run_id>-pN` spine and `-pN.workflow.mjs` script. The part count grows
    until every part composes under the cap; parts are fired in order."""
    count = max(2, -(-over.script_size * 11 // (_emit._WORKFLOW_SCRIPT_BYTE_CAP * 10)))
    if not params.get("output_path"):
        from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

        inventory = Path(params["inventory_path"])
        run_id = read_frontmatter_field(str(inventory), "run_id") or inventory.stem
        params = {
            **params,
            "output_path": str(inventory.parent / f"{run_id}{_REQUIRED_OUT_SUFFIX}"),
        }
    # Every part, and every retry at a higher count, cuts the ONE tranche record the
    # over-cap emit minted; re-minting per part numbered sibling parts t8/t9.
    tranche_record = getattr(over, "tranche_inventory", None)
    if tranche_record:
        params = {**params, "inventory_path": str(tranche_record)}
    while True:
        results: list = []
        try:
            for index in range(1, count + 1):
                part_params = {
                    **params,
                    "inventory_part": [index, count],
                    "output_path": _part_out_path(params["output_path"], index),
                }
                part_result = _dispatch_emit(part_params, repo_root=repo_root)
                part_result["part"] = f"{index}/{count}"
                results.append(part_result)
            break
        except ScriptOverCapError:
            count += 1
    print(
        f"emit-dispatch-workflow: inventory over the {_emit._WORKFLOW_SCRIPT_BYTE_CAP}-byte "
        f"Workflow script cap ({over.row_count} rows); emitted {count} parts. "
        "Fire them in order, each only after the previous run has landed.",
        file=sys.stderr,
    )
    print(json.dumps({"parts": results, "admission": admission_record}, indent=2, sort_keys=True))
    for part_result in results:
        _print_workflow_invocation(part_result, is_queue_route=False, repo_root=repo_root)
    if fire:
        print(
            "emit-dispatch-workflow: ERROR — --fire fires one script; the inventory was "
            f"emitted as {count} parts, none fired",
            file=sys.stderr,
        )
        return EXIT_DATA_ERROR
    return EXIT_OK if all(r["ok"] for r in results) else EXIT_DATA_ERROR


def _lane_inventories(record: Path) -> "list[Path]":
    """The inventory records beside a continuance record: every `<run-id>*.md`
    in its directory carrying a `## Chunk table`, minus the record itself and
    the minted `.spine.md` files."""
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

    run_id = read_frontmatter_field(str(record), "run_id") or record.name.removesuffix(
        "-continuance.md"
    )
    lanes = []
    for candidate in sorted(record.parent.glob(f"{run_id}*.md")):
        if candidate == record or candidate.name.endswith(".spine.md"):
            continue
        try:
            if "## Chunk table" in candidate.read_text(encoding="utf-8"):
                lanes.append(candidate)
        except OSError:
            continue
    return lanes


def _do_resume(args: argparse.Namespace) -> int:
    record = Path(args.resume_from)
    if not record.is_file():
        print(f"emit-dispatch-workflow: ERROR — --resume-from {args.resume_from!r} is not a file", file=sys.stderr)
        return EXIT_DATA_ERROR
    lanes = _lane_inventories(record)
    if not lanes:
        print(
            f"emit-dispatch-workflow: ERROR — no lane inventory with a `## Chunk table` "
            f"beside {record.name}",
            file=sys.stderr,
        )
        return EXIT_DATA_ERROR
    repo_root = Path(args.repo_root).resolve() if args.repo_root else _default_repo_root_from_cwd()
    out = Path(args.out_path)
    base = out.name[: -len(_REQUIRED_OUT_SUFFIX)]
    run_prefix = record.name.removesuffix("-continuance.md")
    results = []
    for lane_path in lanes:
        lane = lane_path.stem.removeprefix(run_prefix).lstrip("-") or "main"
        lane_out = out.with_name(f"{base}-{lane}{_REQUIRED_OUT_SUFFIX}")
        params: dict = {
            "inventory_path": str(lane_path),
            "output_path": str(lane_out),
            "skip_landed": True,
            "force": args.force,
        }
        if repo_root is not None:
            params["inventory_repo_root"] = str(repo_root)
        try:
            result = _dispatch_emit(params, repo_root=repo_root)
        except NothingUnlandedError:
            results.append({"lane": lane, "nothing_unlanded": True})
            continue
        except _DATA_ERRORS as exc:
            print(f"emit-dispatch-workflow: ERROR — lane {lane}: {exc}", file=sys.stderr)
            return EXIT_DATA_ERROR
        result["lane"] = lane
        results.append(result)
    print(json.dumps({"lanes": results}, indent=2, sort_keys=True))
    for result in results:
        if "path" in result:
            _print_workflow_invocation(result, is_queue_route=False, repo_root=repo_root)
    return EXIT_OK if all(r.get("ok", True) for r in results) else EXIT_DATA_ERROR


def _review_only_refusal(args) -> "Optional[str]":
    """The usage error for a bad ``--review-only``/``--run-base`` combination, or None."""
    if args.rows is not None and args.review_only is None:
        return "--rows is accepted only with --review-only"
    if args.review_only is None and args.run_base is None:
        return None
    if args.review_only is None or args.run_base is None:
        return "--review-only and --run-base are required together"
    if not args.plan and not args.inventory:
        return "--review-only is accepted only with --plan or --inventory"
    if args.plan and args.inventory:
        return "--review-only takes --plan or --inventory, not both"
    if not re.fullmatch(r"[0-9a-f]{7,40}", args.run_base):
        return f"--run-base {args.run_base!r} is not 7-40 lowercase hex digits"
    conflicts = [
        flag
        for flag, value in (
            ("--only-incomplete", args.only_incomplete),
            ("--resume-from", args.resume_from),
            ("--reverify-delivery", args.reverify_delivery is not None),
            ("--chatty", args.chatty),
            ("--row-budget", args.row_budget is not None),
            ("--lanes", args.lanes),
            ("--queue", args.queue),
            ("--profile", args.profile),
            ("--ask", args.ask is not None),
            ("--sizing", args.sizing),
            ("--pipeline", args.pipeline is not None),
        )
        if value
    ]
    if conflicts:
        return f"--review-only is exclusive of {', '.join(conflicts)}"
    return None


def _report_close_route_emit(result: dict, args: argparse.Namespace) -> int:
    """Print a --rejudge/--reverify-delivery emission; under --fire, fire it and print the
    handle. Trap: a close route that drops --fire returns no handle, and a headless caller
    reads that silence as a fire."""
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.fire:
        print(f"\n  Workflow({{ scriptPath: {json.dumps(result['path'])} }})", file=sys.stderr)
        return EXIT_OK
    from coordinator_core.ops.workflow_fire.fire import fire_workflow

    fire_record = fire_workflow(result["path"], cwd=args.repo_root or None)
    print(json.dumps(fire_record, indent=2, sort_keys=True))
    return EXIT_OK


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    review_only_error = _review_only_refusal(args)
    if review_only_error:
        print(f"emit-dispatch-workflow: ERROR — {review_only_error}", file=sys.stderr)
        return EXIT_USAGE

    if args.mark_landed_phase:
        if args.plan or args.inventory or args.out_path or args.fire or args.restamp:
            print(
                "emit-dispatch-workflow: ERROR — --mark-landed is exclusive of "
                "--plan/--inventory/--out/--fire/--restamp",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return _do_mark_landed(args.script_positional, args.mark_landed_phase, args.sha)

    if args.rejudge:
        if args.plan and not args.out_path:
            args.out_path = str(Path(args.plan).parent / f"{Path(args.plan).stem}{_REQUIRED_OUT_SUFFIX}")
        if not args.plan or not args.out_path or not args.out_path.endswith(_REQUIRED_OUT_SUFFIX):
            print(
                f"emit-dispatch-workflow: ERROR — --rejudge needs --plan; --out defaults to "
                f"<plan>{_REQUIRED_OUT_SUFFIX} beside the plan and must end {_REQUIRED_OUT_SUFFIX!r}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops.dispatch_emit.op import _installed_plugin_root
        from coordinator_core.ops.dispatch_emit.emit import resolve_agent_type_host
        from coordinator_core.ops.dispatch_emit.reverify_delivery import ReverifyRefused, emit_rejudge

        try:
            result = emit_rejudge(
                repo_root=Path(args.repo_root).resolve() if args.repo_root else Path.cwd(),
                plan_path=args.plan,
                out_path=args.out_path,
                agent_type_host=resolve_agent_type_host(
                    coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
                    claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT") or _installed_plugin_root(),
                ),
            )
        except (ReverifyRefused, *_DATA_ERRORS) as exc:
            print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
            return EXIT_DATA_ERROR
        return _report_close_route_emit(result, args)

    if args.reverify_delivery is not None:
        if args.plan and not args.out_path:
            args.out_path = str(Path(args.plan).parent / f"{Path(args.plan).stem}{_REQUIRED_OUT_SUFFIX}")
        if not args.plan or not args.out_path or not args.out_path.endswith(_REQUIRED_OUT_SUFFIX):
            print(
                f"emit-dispatch-workflow: ERROR — --reverify-delivery needs --plan; --out defaults to "
                f"<plan>{_REQUIRED_OUT_SUFFIX} beside the plan and must end {_REQUIRED_OUT_SUFFIX!r}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from coordinator_core.ops.dispatch_emit.op import _installed_plugin_root
        from coordinator_core.ops.dispatch_emit.emit import resolve_agent_type_host
        from coordinator_core.ops.dispatch_emit.reverify_delivery import ReverifyRefused, emit_reverify

        try:
            result = emit_reverify(
                repo_root=Path(args.repo_root).resolve() if args.repo_root else Path.cwd(),
                plan_path=args.plan,
                run_record=args.reverify_delivery,
                out_path=args.out_path,
                agent_type_host=resolve_agent_type_host(
                    coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
                    claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT") or _installed_plugin_root(),
                ),
            )
        except (ReverifyRefused, *_DATA_ERRORS) as exc:
            print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
            return EXIT_DATA_ERROR
        return _report_close_route_emit(result, args)

    if args.restamp:
        if args.plan or args.inventory or args.out_path or args.fire:
            print(
                "emit-dispatch-workflow: ERROR — --restamp is exclusive of "
                "--plan/--inventory/--out/--fire",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return _do_restamp(args.restamp)

    if args.resume_from:
        if args.plan or args.inventory or args.queue or args.profile or args.ask is not None or args.sizing or args.fire:
            print(
                "emit-dispatch-workflow: ERROR — --resume-from is exclusive of "
                "--plan/--inventory/--queue/--profile/--ask/--sizing/--fire",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if not args.out_path or not Path(args.out_path).name.endswith(_REQUIRED_OUT_SUFFIX):
            print(
                f"emit-dispatch-workflow: ERROR — --resume-from needs --out ending {_REQUIRED_OUT_SUFFIX!r}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return _do_resume(args)

    is_pipeline_route = args.pipeline is not None
    if args.research and not isinstance(args.ask, str):
        print("emit-dispatch-workflow: ERROR — --research needs --ask PROMPT", file=sys.stderr)
        return EXIT_USAGE
    is_research_route = bool(args.from_sizing) or args.research
    if is_research_route:
        others = [
            flag
            for flag, value in (
                ("--plan", args.plan),
                ("--inventory", args.inventory),
                ("--queue", args.queue),
                ("--profile", args.profile),
                ("--sizing", args.sizing),
                ("--pipeline", is_pipeline_route),
                ("--writes", args.writes),
                ("--baton", args.baton),
                ("--deliverable-id", args.deliverable_id),
                ("--fire", args.fire),
                ("--ask", args.from_sizing and args.ask is not None),
                ("--research", args.from_sizing and args.research),
            )
            if value
        ]
        if others:
            print(
                f"emit-dispatch-workflow: ERROR — the research route is exclusive of {', '.join(others)}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    if args.context and not is_research_route:
        print("emit-dispatch-workflow: ERROR — --context requires --from-sizing or --research", file=sys.stderr)
        return EXIT_USAGE
    pipeline_only = [
        flag
        for flag, value in (
            ("--brief", args.brief),
            ("--subjects", args.subjects),
            ("--scratch-dir", args.scratch_dir if not is_research_route else None),
            ("--resume-missing", args.resume_missing),
            ("--flag", args.flag),
            ("--list", args.list if not is_research_route else None),
            ("--validator", args.validator),
        )
        if value
    ]
    if pipeline_only and not is_pipeline_route:
        print(
            f"emit-dispatch-workflow: ERROR — {', '.join(pipeline_only)} require --pipeline",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if is_pipeline_route:
        others = [
            flag
            for flag, value in (
                ("--plan", args.plan),
                ("--inventory", args.inventory),
                ("--queue", args.queue),
                ("--profile", args.profile),
                ("--ask", args.ask is not None),
                ("--sizing", args.sizing),
                ("--writes", args.writes),
                ("--fire", args.fire),
            )
            if value
        ]
        if others:
            print(
                f"emit-dispatch-workflow: ERROR — --pipeline is exclusive of {', '.join(others)}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if not args.brief:
            print(
                "emit-dispatch-workflow: ERROR — --pipeline needs --brief PATH",
                file=sys.stderr,
            )
            return EXIT_USAGE

    is_queue_route = bool(args.queue) or bool(args.profile)

    is_ask_route = (args.ask is not None or bool(args.sizing)) and not is_research_route

    if args.writes and not is_ask_route:
        print(
            "emit-dispatch-workflow: ERROR — --writes is accepted only with --ask/--sizing",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if (args.baton or args.deliverable_id) and not is_ask_route:
        print(
            "emit-dispatch-workflow: ERROR — --baton/--deliverable-id are accepted only with "
            "--ask/--sizing",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if is_ask_route and args.fire:
        print(
            "emit-dispatch-workflow: ERROR — --fire is not accepted with --ask/--sizing; "
            "fire the printed Workflow({...}) line in-session.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if is_ask_route and (is_queue_route or args.plan or args.inventory):
        print(
            "emit-dispatch-workflow: ERROR — --ask/--sizing is exclusive of "
            "--plan/--inventory/--queue/--profile",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.ask is True and not args.sizing:
        print(
            "emit-dispatch-workflow: ERROR — --ask needs a PROMPT or --sizing",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if isinstance(args.ask, str) and args.sizing:
        print(
            "emit-dispatch-workflow: ERROR — --ask PROMPT is exclusive of --sizing",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if is_queue_route and (args.plan or args.inventory):
        print(
            "emit-dispatch-workflow: ERROR — --queue/--profile is exclusive of "
            "--plan/--inventory",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if (
        not is_queue_route and not args.plan and not args.inventory and not is_ask_route
        and not is_pipeline_route and not is_research_route
    ):
        print(
            "emit-dispatch-workflow: ERROR — one of --plan, --inventory, --ask, --sizing, "
            "--pipeline, --from-sizing, or --restamp is required",
            file=sys.stderr,
        )
        return EXIT_USAGE

    lane_flags = [
        flag
        for flag, value in (
            ("--lanes", args.lanes),
            ("--part", args.part),
            ("--lane-count", args.lane_count),
            ("--hot-files", args.hot_files),
        )
        if value is not None and value is not False
    ]
    if lane_flags:
        problem = None
        if not args.inventory:
            problem = f"{', '.join(lane_flags)} requires --inventory"
        elif args.part and not args.lanes:
            problem = "--part requires --lanes"
        elif (args.lane_count or args.hot_files) and not args.lanes:
            problem = "--lane-count/--hot-files require --lanes"
        elif args.out_path or args.fire:
            problem = "--lanes derives each part's script path and is exclusive of --out/--fire"
        if problem:
            print(f"emit-dispatch-workflow: ERROR — {problem}", file=sys.stderr)
            return EXIT_USAGE

    if args.where and args.where_file:
        print(
            "emit-dispatch-workflow: ERROR — --where is exclusive of --where-file",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if (args.hold or args.hold_reason) and (not args.plan or args.inventory or args.review_only is not None):
        print(
            "emit-dispatch-workflow: ERROR — --hold/--hold-reason apply to --plan alone "
            "(not --inventory or --review-only)",
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

    if not is_queue_route:
        # Restated from coordinator-content-repo emit-dispatch-workflow.py (review(slice A),
        # 08fb23d21): these flags are queue-route-only and are otherwise
        # silently ignored by the --plan/--inventory/--restamp route -- a
        # caller who drops --queue/--profile while editing a queue invocation
        # gets a normal emit with no signal that these did nothing.
        queue_only_set = [
            flag
            for flag, value in (
                ("--profile", args.profile),
                ("--appetite", args.appetite if args.appetite != "standard" else None),
                ("--profile-dir", args.profile_dir),
                ("--where", args.where),
                ("--where-file", args.where_file),
                ("--limit", args.limit),
                ("--budget-tokens", args.budget_tokens),
            )
            if value
        ]
        if queue_only_set:
            print(
                f"emit-dispatch-workflow: ERROR — {', '.join(queue_only_set)} "
                f"{'is' if len(queue_only_set) == 1 else 'are'} queue-route-only and "
                "require --queue and --profile -- without them, the plan/inventory/"
                "restamp route runs and silently ignores them.",
                file=sys.stderr,
            )
            return EXIT_USAGE

    if is_queue_route and not args.profile_dir:
        try:
            args.profile_dir = _default_profile_dir(args.profile)
        except _ProfileDirUnresolved as exc:
            print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
            return EXIT_USAGE

    if not args.out_path and args.plan and not is_queue_route:
        # Default --out to <plan-basename>.workflow.mjs beside the plan, the
        # plan route's only unambiguous target. The --inventory route defaults
        # in the op (beside its minted spine); the queue route has no single
        # plan file to derive a basename from and keeps requiring --out.
        args.out_path = str(Path(args.plan).parent / f"{Path(args.plan).stem}{_REQUIRED_OUT_SUFFIX}")

    if (
        not args.out_path
        and not is_ask_route
        and not is_pipeline_route
        and not is_research_route
        and not args.lanes
        and not (args.inventory and not is_queue_route)
    ):
        print("emit-dispatch-workflow: ERROR — --out is required", file=sys.stderr)
        return EXIT_USAGE

    out_path = Path(args.out_path) if args.out_path else None
    if out_path is not None and not out_path.name.endswith(_REQUIRED_OUT_SUFFIX):
        print(
            f"emit-dispatch-workflow: ERROR — --out {args.out_path!r} does not end "
            f"{_REQUIRED_OUT_SUFFIX!r} -- refusing to write an emitted script over a "
            f"path that is not named as one. Name --out ending {_REQUIRED_OUT_SUFFIX!r}.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    preamble_text: "Optional[str]" = None
    preamble_sha256: "Optional[str]" = None
    if args.preamble_path:
        import hashlib

        try:
            preamble_bytes = Path(args.preamble_path).read_bytes()
        except OSError as exc:
            print(
                f"emit-dispatch-workflow: ERROR — --preamble {args.preamble_path!r} "
                f"unreadable: {exc}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        preamble_text = preamble_bytes.decode("utf-8")
        preamble_sha256 = hashlib.sha256(preamble_bytes).hexdigest()

    box_terms: "list[str]" = []
    if args.box_terms_path:
        try:
            raw_terms = Path(args.box_terms_path).read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"emit-dispatch-workflow: ERROR — --box-terms {args.box_terms_path!r} "
                f"unreadable: {exc}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        box_terms = [ln.strip() for ln in raw_terms.splitlines() if ln.strip()]
        if not box_terms:
            print(
                f"emit-dispatch-workflow: ERROR — --box-terms {args.box_terms_path!r} names no term",
                file=sys.stderr,
            )
            return EXIT_USAGE
        terms_block = "Box terms (from the driver; binding):" + "".join(f"\n- {t}" for t in box_terms)
        preamble_text = f"{preamble_text}\n\n{terms_block}" if preamble_text else terms_block

    repo_root = Path(args.repo_root).resolve() if args.repo_root else None
    if repo_root is None and is_queue_route:
        repo_root = _default_repo_root_from_cwd()

    params: dict = {"force": args.force}
    # Venue is read here, in the dispatching session's own env: the op body may
    # run warm-served, where os.environ belongs to whoever spawned the server.
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import is_remote_venue

    if args.cross_repo_approved or is_remote_venue():
        params["cross_repo_approved"] = True
    if args.out_path:
        params["output_path"] = args.out_path
    if is_ask_route:
        params["ask"] = args.ask if args.ask is not None else True
        if args.sizing:
            params["sizing_path"] = args.sizing
        if args.writes:
            params["writes"] = list(args.writes)
        if args.baton:
            params["baton"] = args.baton
        if args.deliverable_id:
            params["deliverable_id"] = args.deliverable_id
        if repo_root is None:
            repo_root = _default_repo_root_from_cwd()
    if is_research_route:
        if args.from_sizing:
            params["from_sizing"] = args.from_sizing
        else:
            params["research"] = True
            params["ask"] = args.ask
        try:
            if args.list:
                params["lists"] = _parse_lists(args.list)
        except PipelineEmitRefused as exc:
            for reason in exc.reasons:
                print(f"emit-dispatch-workflow: ERROR — {reason}", file=sys.stderr)
            return EXIT_DATA_ERROR
        if args.scratch_dir:
            params["scratch_dir"] = args.scratch_dir
        if args.context:
            params["context"] = args.context
        if repo_root is None:
            repo_root = _default_repo_root_from_cwd()
    if is_pipeline_route:
        params["pipeline"] = args.pipeline
        params["brief"] = args.brief
        try:
            if args.subjects:
                params["subjects"] = _load_subjects(args.subjects)
            if args.flag:
                params["flags"] = _parse_flags(args.flag)
            if args.list:
                params["lists"] = _parse_lists(args.list)
        except PipelineEmitRefused as exc:
            for reason in exc.reasons:
                print(f"emit-dispatch-workflow: ERROR — {reason}", file=sys.stderr)
            return EXIT_DATA_ERROR
        if args.scratch_dir:
            params["scratch_dir"] = args.scratch_dir
        if args.validator is not None:
            params["validator"] = args.validator
        if args.resume_missing:
            if not args.scratch_dir:
                print(
                    "emit-dispatch-workflow: ERROR — --resume-missing requires --scratch-dir "
                    "(the interrupted run's scratch dir)",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            params["resume_missing"] = True
        if repo_root is None:
            repo_root = _default_repo_root_from_cwd()
    if args.plan:
        params["plan_path"] = args.plan
        if args.chatty:
            params["chatty"] = True
        hold_ids = [r.strip() for r in (args.hold or "").split(",") if r.strip()]
        if hold_ids:
            if not (args.hold_reason or "").strip():
                print("emit-dispatch-workflow: ERROR — --hold requires --hold-reason", file=sys.stderr)
                return EXIT_USAGE
            params["hold_rows"] = hold_ids
            params["hold_reason"] = args.hold_reason.strip()
        elif args.hold_reason:
            print("emit-dispatch-workflow: ERROR — --hold-reason requires --hold", file=sys.stderr)
            return EXIT_USAGE
        if args.only_incomplete:
            from coordinator_core.ops.dispatch_emit.emit import landed_rows_from_text

            params["landed_rows"] = sorted(
                landed_rows_from_text(Path(args.only_incomplete).read_text(encoding="utf-8"))
            )
    if args.review_only is not None:
        from coordinator_core.ops.dispatch_emit.emit import landed_rows_from_text
        from coordinator_core.ops.dispatch_emit.spine_read import SpineReadError, coded_row_ids

        review_set = {r.strip() for r in (args.rows or "").split(",") if r.strip()}
        if args.review_only:
            try:
                text = Path(args.review_only).read_text(encoding="utf-8")
            except OSError as exc:
                print(
                    f"emit-dispatch-workflow: ERROR — --review-only {args.review_only!r} "
                    f"unreadable: {exc}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            review_set |= landed_rows_from_text(text)
        if not review_set and args.inventory:
            print(
                "emit-dispatch-workflow: ERROR — --review-only with --inventory names no row: "
                "pass --rows or RUN_TEXT with checkpoint subjects",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if not review_set:
            try:
                review_set = set(coded_row_ids(args.plan))
            except (OSError, SpineReadError) as exc:
                print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
                return EXIT_DATA_ERROR
        review_rows = sorted(review_set)
        if not review_rows:
            print(
                "emit-dispatch-workflow: ERROR — --review-only names no row: pass --rows, "
                "RUN_TEXT with checkpoint subjects, or a plan with coded rows",
                file=sys.stderr,
            )
            return EXIT_DATA_ERROR
        params["review_only_rows"] = review_rows
        params["run_base_sha"] = args.run_base
    if args.inventory:
        params["inventory_path"] = args.inventory
        if args.max_rows is not None:
            params["max_rows"] = args.max_rows
        if args.row_budget is not None:
            params["row_budget"] = args.row_budget
        if args.lanes:
            params["lanes"] = True
            for name in ("part", "lane_count", "hot_files"):
                if getattr(args, name) is not None:
                    params[name] = getattr(args, name)
        inventory_root = repo_root or _default_repo_root_from_cwd()
        if inventory_root is not None:
            params["inventory_repo_root"] = str(inventory_root)
    if box_terms:
        params["box_terms"] = box_terms
        params["box_terms_path"] = args.box_terms_path
    if preamble_text is not None:
        params["preamble"] = preamble_text
        params["preamble_path"] = args.preamble_path
        params["preamble_sha256"] = preamble_sha256

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

    # Lazy imports (not module-level) so a test can stub `admission` via
    # `monkeypatch.setitem(sys.modules, "coordinator_core.ops.dispatch_emit.admission", fake)`
    # before this function runs -- a top-level `from ... import admission` would bind the
    # real module at cli.py's own import time, before any test gets a chance to swap it.
    from coordinator_core.ops.dispatch_emit import admission
    from coordinator_core.telemetry import op_latency

    hold_allowed = op_latency.execution_route() != op_latency.WARM_SERVER
    admission_record = admission.await_admission(Path.cwd(), hold_allowed=hold_allowed)
    if admission_record.get("verdict") not in ("admitted", "disabled"):
        print(
            "emit-dispatch-workflow: admission — "
            f"{admission_record.get('verdict')}; {admission_record.get('reasons')}; "
            f"waited {admission_record.get('waited_s')}s",
            file=sys.stderr,
        )

    regenerable_dirty = None
    try:
        if args.plan and not args.inventory:
            from coordinator_core.ops.dispatch_emit.dirty_write_set import (
                guard_against_dirty_write_set,
            )
            from coordinator_core.ops.dispatch_emit.op import _repo_root_for_plan

            guard_root = repo_root or _repo_root_for_plan(args.plan)
            if guard_root is not None:
                regenerable_dirty = guard_against_dirty_write_set(
                    Path(args.plan), guard_root,
                    hold=frozenset(params.get("hold_rows") or ()),
                    landed=frozenset(params.get("landed_rows") or ()),
                )
        try:
            result = _dispatch_emit(params, repo_root=repo_root)
        except ScriptOverCapError as over:
            if not args.inventory or args.lanes:
                raise
            return _emit_inventory_parts(params, repo_root, over, args.fire, admission_record)
    except SizingHandBack as hand_back:
        print(hand_back.line)
        return EXIT_OK
    except PipelineEmitRefused as exc:
        for reason in exc.reasons:
            print(f"emit-dispatch-workflow: ERROR — {reason}", file=sys.stderr)
        return EXIT_DATA_ERROR
    except _DATA_ERRORS as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR

    result["admission"] = admission_record
    if regenerable_dirty:
        result["regenerable_dirty"] = regenerable_dirty
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.lanes:
        for emitted in result["parts"]:
            _print_workflow_invocation(
                {**emitted, "fire_args": result.get("fire_args")},
                is_queue_route=False,
            )
    else:
        _print_workflow_invocation(
            result, is_queue_route=is_queue_route, profile_dir=args.profile_dir, repo_root=repo_root
        )

    hold = result.get("hold")
    if hold:
        print(
            f"emit-dispatch-workflow: held {', '.join(hold['rows'])} ({hold['reason']}); "
            f"dependents also held: {json.dumps(hold['dependents'], sort_keys=True)}; "
            "re-emit with --only-incomplete once the hold clears",
            file=sys.stderr,
        )

    for key in ("batons", "uncommitted"):
        if key in result:
            print(f"emit-dispatch-workflow: {key}: {json.dumps(result[key])}", file=sys.stderr)
    if (result.get(NOT_ADMITTED_EXTRA_KEY) or {}).get("count", 0) > 0:
        print(
            f"emit-dispatch-workflow: {NOT_ADMITTED_EXTRA_KEY}: "
            f"{json.dumps(result[NOT_ADMITTED_EXTRA_KEY])}",
            file=sys.stderr,
        )

    if not result["ok"]:
        return EXIT_DATA_ERROR

    if args.fire:
        from coordinator_core.ops.dispatch_emit.op import guard_against_fired_drift
        from coordinator_core.ops.workflow_fire.fire import fire_workflow

        guard_against_fired_drift(Path(result["path"]), result["sha256"])
        fire_record = fire_workflow(
            result["path"], cwd=str(repo_root) if repo_root else None
        )
        print(json.dumps(fire_record, indent=2, sort_keys=True))

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
