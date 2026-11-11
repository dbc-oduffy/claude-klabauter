"""``dispatch.land_run``: land a run's product and its run record in one scoped commit.

``land_run`` is the successor of the run-end commit op. The landing set is closed, in three kinds:
(i) the declared writes of every credited row that differ from HEAD, with the files a row's own report
claims under its declared prefix; (ii) ``run_artifacts.candidate_paths`` (the run's record dir and what
it authored); (iii) the record dirs of superseded predecessor runs; (iv) a product file in the run's stored
review-prep whole diff (``reviewed_sha256``) that no credited row declares, a pure-shrink edit included,
which passes the same holds as a declared path and is otherwise named in ``withheld``. Nothing else
enters: no tree scan, no prefix expansion, no peer's dirt. Differences from HEAD are read in process from the object store,
and the commit is ``git.commit.commit_paths`` (zero spawns, a CAS ref move).

A credited row's changed declared path either lands or is named: a peer-held, never-commit,
sibling-repo or EM-withheld path stays out of the commit, rides a ``Row-Unlanded: <row> <path> <reason>``
trailer and the reply's ``stranded``. Such a row is not in ``Lands-Chunks:`` and stays open. A path a
senior live run merely declares does not hold a path this run's own ``checkpoint(wave N)`` commit
already landed; a live write claim of another session still holds it. Every held path is named in the
reply's ``peer_held_left``. Under a requirements ledger, a credited row's path a live sibling holds
refuses the landing (``shared-path-sibling-held``); any other withheld credited path clears the
``ledger_met`` observation and the reply names it in ``ledger_withheld_product``.

Everything that can run after the commit is queued to the close job (``close_background``): the review
stamp, coded stamp, implemented flip, sizing ship, cascade, completion entry, baton ship and ledger
writeback. The reply reports those legs ``deferred``/``queued``; ``usage.json`` is stamped here because
``sibling_hold`` reads it at HEAD as the landed marker.

Negative spec: the expectation on the run's own record-named run-artifact paths is dropped through
``run_artifact_ownership.OwnArtifacts.drop_expectations`` and the paths are re-read at commit, named in
``run_artifacts.reread``; the delivery note is never in the expectation here (excluded from the recorded
fingerprints), so it is re-read like the terminal commit's, which keeps its own declared hash. A path
``process_artifact_policy`` withholds (foreign repo, non-coordinator host) is dropped from the landing
set and reported in ``run_artifacts.withheld``; it is never staged and never carries an expectation.

Other record modes (halted, grind, fanout) are lazily imported from ``land_run_contract.MODE_MODULES``.
``landed_on_head`` and ``already_landed_reply`` are public: the mode modules and callers share them.

DR-208 five-question affirmation (MUTATING):
  1. Writes, deletes, or reorders any state file, queue, or git object?   YES. The commit writes git
     objects and moves the branch ref; the run token and sibling status files change.
  2. Writes into rag's relational store?                                  No.
  3. Opens any file for write (including sentinel creation)?              YES. usage.json, the
     completion receipts, the review record and the close-job record.
  4. Mutates shared mutable state outside its own module?                 YES. The landed commit is
     read by every later dispatch against this repo.
  5. Persistent state changes observable across process boundaries?      YES. The sha, the ref move
     and the close-job record persist across the box.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Collection, Mapping, Optional, Sequence

from coordinator_core.git.machine_local_paths import machine_local_paths
from coordinator_core import ipc as _ipc
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import land_run_contract as contract
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_FOOTPRINT_FILE,
    OUTSIDE_ROW_FOOTPRINT,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RUN_DIR_ROOT,
    TERMINAL_RESUME_LEDGER_PARAM,
    measured_rev_record_path,
    read_run_outcome,
    record_dir,
    UNCHANGED_SINCE_HALT_REASON,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.halt_gap_reverify_closure import closed_by_reverify
from coordinator_core.ops.dispatch_emit.halted_run_route import halted_refusal, route_suffix
from coordinator_core.ops.dispatch_emit.ledger_recovery import RECOVERY_WITHHELD_PARAM, _posix as _norm_rel
from coordinator_core.ops.dispatch_emit.refire_inherited_scope import (
    ORIGIN_PREDECESSOR_WITHHELD,
    inherited_scope,
    withheld_of as _withheld_of,
)
from coordinator_core.ops.dispatch_emit import land_deadline, land_leg_record, landing_halted_view, review_floor
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

_PARAM_FIELDS = (
    Field("script_path", "nonempty_str", required=True),
    Field("incomplete_chunks", "str_list", required=True),
    Field("landed_chunks", "str_list"),
    Field("inline_review", "dict"),
    Field("task_output_path", "nonempty_str"),
    Field("plan_path", "nonempty_str"),
    Field("falsifier_broken", "list"),
    Field("held", "list"),
    Field("release_held", "list"),
    Field("withhold_paths", "list"),
    Field("run_id", "nonempty_str"),
    Field("run_token", "nonempty_str"),
    Field("ledger_verdict", "dict"),
    Field(RECOVERY_WITHHELD_PARAM, "str_list"),
    Field(TERMINAL_RESUME_LEDGER_PARAM, "dict"),
    Field("doc_paths", "str_list"),
    Field("allow_empty", "bool"),
    Field("declared_reverts", "str_list"),
)

_SHA40 = re.compile(r"[0-9a-f]{40}")
_FILTERED = "?"
_PYTEST_NODE = re.compile(r"[\w./-]+\.py::[\w.\[\]-]+(?:::[\w.\[\]-]+)*")
_UNDONE_CAP = 400
_EVIDENCE_SUFFIX = ".evidence.yaml"
_CLOSE_JOB_WINDOW_MINUTES = 5
_USAGE_FILE = "usage.json"

#: A corpus the run machinery itself writes: a declared write into it is process state, not product.
_PROCESS_STATE_CORPORA = frozenset({"sizings", "warp", "handoffs", "apm-rulings", "queue-grind"})

#: Artifact classes the product landing carries; ``plan`` and ``baton`` only for a manifest (ask) run.
_PRODUCT_ARTIFACT_CLASSES = frozenset({
    "record-dir", "evidence-sidecar", "seam-sidecar", "slice-sizing", "superseded-record",
    "docs-stage", "delivery-note", "sizing",
})
_ASK_ARTIFACT_CLASSES = frozenset({"plan", "baton"})

#: Refusals raised before the run's incomplete set is known: ``stranded`` would read ``{}``.
_STRANDED_UNKNOWN_REFUSALS = frozenset({"missing-run-outcome", "unreviewed"})

_STATUS_FIELD = r'^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*'
_DELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}DONE(?:_WITH_CONCERNS)?[*_`]{0,2}:|<exit-status>DONE</exit-status>'
    rf'|(?i:{_STATUS_FIELD}DONE(?:_WITH_CONCERNS)?\b)',
    re.MULTILINE,
)
_UNDELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}(?:PARTIAL|BLOCKED)[*_`]{0,2}:|<exit-status>(?:PARTIAL|BLOCKED)</exit-status>'
    rf'|(?i:{_STATUS_FIELD}(?:PARTIAL|BLOCKED)\b)',
    re.MULTILINE,
)
_PARTIAL_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}PARTIAL[*_`]{0,2}:|<exit-status>PARTIAL</exit-status>'
    rf'|(?i:{_STATUS_FIELD}PARTIAL\b)',
    re.MULTILINE,
)
_BLOCKED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}BLOCKED[*_`]{0,2}:|<exit-status>BLOCKED</exit-status>'
    rf'|(?i:{_STATUS_FIELD}BLOCKED\b)',
    re.MULTILINE,
)
_UNDONE_LINE_RE = re.compile(r"^\s*[-*]?\s*[*_`]{0,2}(?:not done|undone|remaining)", re.IGNORECASE)
_CAUSE_HEADING_RE = re.compile(r"^#{1,6}\s+.*\b(?:why|cause|blocker|blocked|reason)\b", re.IGNORECASE)
_KIND_FIELD = re.compile(r"^(derived_from|scope_mode)[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)
_SIZING_OBJECT_RESOLUTION = re.compile(r"^[0-9a-f]{7,64}$")


#: The first leg checked in the product landing; a halt on it means nothing started or committed.
SCRIPT_READ_LEG = "land_run.script_read"


def _error(message: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": message, **extra}


def held_revision_refusal(root: Path, ledger: object, run_dir: Path) -> Optional[dict]:
    """Refusal (``refused="held-revision-unconsumed"``) when a ``workflow.revise`` held for the
    ledger's sizing was never folded into the run; else None. One indexed read, shared by both commit gates."""
    from coordinator_core.ops.dispatch_emit import held_revisions

    sizing_rel = getattr(ledger, "sizing_rel", None)
    if not sizing_rel:
        return None
    held = held_revisions.unconsumed(root, sizing_rel, run_dir.name)
    if not held:
        return None
    names = ", ".join(f"{h.get('id')} (run {h.get('run_id')}, {h.get('at')})" for h in held)
    return _error(
        f"held revision unconsumed for {sizing_rel}: {names}. Re-fire the sizing so bind folds it",
        refused="held-revision-unconsumed",
        held=[{"id": h.get("id"), "run_id": h.get("run_id"), "at": h.get("at")} for h in held],
    )


def _read_rel(root: Path, rel: Optional[str]) -> Optional[str]:
    """Text of ``root/rel`` when it resolves under ``root``, else None."""
    if not rel:
        return None
    target = contained_path(root / rel, [root])
    if target is None:
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ---- the handler ------------------------------------------------------------------------------


from coordinator_core.ops.workflow_fire.finalizer import terminal_action as _private_terminal_action


@register_op("dispatch.land_run")
@_private_terminal_action('dispatch.land_run')
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land a run's product and record in one scoped commit; reply keys are ``REPLY_KEYS``.

    Product mode params: ``script_path``, ``incomplete_chunks`` and ``inline_review`` (or a
    ``task_output_path`` supplying them), plus ``run_id``/``run_token``, ``ledger_verdict``,
    ``resume_ledger``, ``landed_chunks``, ``held``, ``doc_paths``, ``declared_reverts``,
    ``allow_empty``, ``session_id``. A call carrying ``halted_by``, ``fanout`` or ``grind`` is one of
    the record modes. A repeat call whose first call landed answers ``already_landed``.
    """
    from coordinator_core.git.call_io_scope import call_io_scope

    with call_io_scope():
        return _dispatch(params, repo_root)


def _stamp_withheld_prints(root: Path, reply: object) -> None:
    """Halted mode stamps ``run_artifacts.withheld`` without ``WITHHELD_PRINTS_FILE``; write it so a
    later run of the same sizing credits those paths (``predecessor_withheld`` credits only printed
    ones). The file lands with the record's next commit, not this halt's."""
    if not isinstance(reply, dict) or reply.get("committed") is not True:
        return
    withheld, record_dir_rel = reply.get("withheld"), reply.get("record_dir")
    if isinstance(withheld, dict) and withheld and isinstance(record_dir_rel, str):
        from coordinator_core.ops.dispatch_emit.halted_record_ledger import _record_withheld_prints

        _record_withheld_prints(root, record_dir_rel, withheld, [])


def _dispatch(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return _error(
            f"{contract.OP_NAME} requires a common_dir-keyed dispatch; repo_root (git common dir) "
            "was not supplied"
        )
    try:
        mode = contract.mode_of(params)
    except contract.ModeAmbiguous as exc:
        return _error(str(exc), refused=exc.refused)
    from coordinator_core.lifecycle import main_worktree_root

    root = main_worktree_root(repo_root)
    # A halt landing is the run leaving its record in the tree; the leg budget refusing it strands
    # run state uncommitted, so only the dispatcher and server-window clamps bound it.
    budget = float("inf") if mode == "halted" else None
    retried = False
    while True:
        try:
            with land_deadline.scope(budget_secs=budget):
                reply = _dispatch_mode(mode, root, repo_root, params)
                if mode == "product" and isinstance(reply, dict) and reply.get("committed") is True:
                    land_leg_record.write(
                        root, params.get("run_id"), outcome=land_leg_record.OUTCOME_LANDED,
                        leg=None, bound_by=None, legs=land_deadline.leg_costs(),
                    )
                    _chain_landed_tranche(root, params, reply)
                return reply
        except land_deadline.LandDeadlineExceeded as exc:
            if not retried and _script_read_stall_retryable(mode, exc):
                retried = True
                continue
            return _deadline_halt(root, params, exc)


def _script_read_stall_retryable(mode: str, exc: land_deadline.LandDeadlineExceeded) -> bool:
    """True for a product halt on the first leg (``land_run.script_read``: nothing started, nothing
    committed) while the dispatcher window still holds a full leg budget plus the reply margin."""
    if mode != "product" or exc.leg != SCRIPT_READ_LEG:
        return False
    from coordinator_core import dispatch_abandon

    left = dispatch_abandon.remaining()
    return left is None or left >= land_deadline.LAND_BUDGET_SECS + dispatch_abandon.REPLY_MARGIN_SECS


def _deadline_halt(root: Path, params: dict, exc: land_deadline.LandDeadlineExceeded) -> dict:
    reply = land_deadline.halt_reply(exc)
    rel = land_leg_record.write(
        root, params.get("run_id"), outcome=land_leg_record.OUTCOME_HALT,
        leg=exc.leg, bound_by=exc.bound_by, legs=reply["leg_costs"],
    )
    if rel:
        reply["leg_record"] = rel
    return reply


def _chain_landed_tranche(root: Path, params: dict, reply: dict) -> None:
    """Hand a fully landed run to ``tranche_chain.chain_after_land`` (one detached spawn when its
    inventory family owes a tranche). Never on a repeat call, a partial land (incomplete rows or
    stranded paths) or a refusal; never raises into the land."""
    try:
        run_id, script = params.get("run_id"), params.get("script_path")
        if (
            reply.get(ALREADY_LANDED_KEY) or reply.get("refused") or params.get("incomplete_chunks")
            or any((reply.get("stranded") or {}).values())
            or not isinstance(run_id, str) or not run_id or not isinstance(script, str) or not script
        ):
            return
        from coordinator_core.ops.dispatch_emit import tranche_chain

        tranche_chain.chain_after_land(root, run_id, root / script)
    except Exception:  # noqa: BLE001 -- the chain never fails the land
        return


def _dispatch_mode(mode: str, root: Path, repo_root: Path, params: dict) -> dict:
    if mode != "product":
        import importlib

        reply = importlib.import_module(contract.MODE_MODULES[mode]).land(root, repo_root, params)
        if mode == "halted":
            _stamp_withheld_prints(root, reply)
        return reply
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    token_state: dict = {}
    try:
        with halted_hold_release.release_scope():
            return _land_product(root, repo_root, params, token_state)
    finally:
        _settle_token(token_state, landed=bool(token_state.get("landed")))


def _settle_token(token_state: dict, *, landed: bool, receipt: Optional[dict] = None) -> None:
    """Spend the claimed run token when a commit landed, release it for every other end."""
    claim = token_state.pop("claim", None)
    if claim is None:
        return
    from coordinator_core.ops.dispatch_emit import run_token

    root, run_id, token, script_path = claim
    if landed:
        run_token.consume(root, run_id, landed=receipt)  # the commit landed: spent whoever holds the claim now
    elif _claim_adopted(token_state.pop("identity", None), claim):
        return  # an adopter holds the claim; releasing would revive a token it is using
    else:
        run_token.release(root, run_id, token, script_path)


def _claim_adopted(identity: Optional[dict], claim: tuple) -> bool:
    """True when this call's claim (`identity`, from `run_token.redeem_claim`) no longer stands on disk."""
    if identity is None:
        return False
    from coordinator_core.ops.dispatch_emit import run_token

    root, run_id, token, script_path = claim
    return not run_token.holds_claim(root, run_id, token, script_path, identity)


class _ClaimAdopted(Exception):
    """The run token claim was adopted by a later call; this call commits nothing."""


def _claim_budget_secs() -> float:
    """The land_run op budget a claim's `deadline` is measured against."""
    return float(_ipc._timeout_for(contract.OP_NAME))


# ---- params -----------------------------------------------------------------------------------


def with_run_session(repo_root: Path, params: dict) -> dict:
    """``params`` with ``session_id`` filled from the run's emission receipt when it lacks one.

    The run id is ``params['run_id']``; a run with no receipt, or a caller-supplied
    ``session_id``, leaves ``params`` unchanged."""
    run_id = params.get("run_id")
    if params.get("session_id") or not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
        return params
    from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

    session = recorded_emitter(repo_root, run_id)
    return {**params, "session_id": session} if session else params


def params_from_task_output(path_raw: str, repo_root: Optional[Path] = None, run_id: Optional[str] = None) -> dict:
    """``next_action.params`` out of a Workflow task-output file, bare or ``{result}``-wrapped.

    A launcher envelope (``runs``) yields the digest of the run ``run_id`` selects; ``run_id`` may be
    omitted only when the envelope holds one run. With ``repo_root`` the params gain the run's
    ``session_id`` (``with_run_session``).
    Raises ValueError naming the file when no ``next_action.params`` dict is found."""
    path = Path(path_raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read params.task_output_path {path_raw!r}: {exc}") from exc
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("runs"), list):
        from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import _launcher_run_digest

        data = _launcher_run_digest(data, path_raw, run_id)
    action = data.get("next_action") if isinstance(data, dict) else None
    found = action.get("params") if isinstance(action, dict) else None
    if not isinstance(found, dict):
        raise ValueError(
            f"{path_raw!r} carries no next_action.params: neither a digest nor a "
            "task-output envelope wrapping one"
        )
    return with_run_session(repo_root, found) if repo_root is not None else found


def _reclose_params(root: Path, params: object) -> Optional[dict]:
    """``{plan_path, run_id, criterion?, ...}`` when the call re-pays a landed run's close legs, else None.

    Two shapes: a ``task_output_path`` whose ``next_action.kind`` is ``close_job`` (its params carry
    ``run_id``, never a run outcome), or a ``script_path`` with no run outcome and no task output whose
    run already has a close-job record. Both need that record to name the plan; none -> None, so the
    ordinary refusal answers. ``criterion`` is the call's own judge verdict (``inline_review.criterion``,
    else the task output's). Product is never re-landed on this route."""
    if not isinstance(params, dict) or params.get("incomplete_chunks") is not None:
        return None
    from coordinator_core.ops.dispatch_emit import run_close

    run_id = None
    criterion = None
    review = params.get("inline_review")
    if isinstance(review, dict) and isinstance(review.get("criterion"), dict):
        criterion = review["criterion"]
    task_output = params.get("task_output_path")
    if isinstance(task_output, str):
        try:
            data = json.loads(Path(task_output).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
            data = data["result"]
        action = data.get("next_action") if isinstance(data, dict) else None
        if not isinstance(action, dict) or action.get("kind") != "close_job":
            return None
        found = action.get("params")
        run_id = found.get("run_id") if isinstance(found, dict) else None
        digest_review = data.get("inline_review")
        if criterion is None and isinstance(digest_review, dict) and isinstance(digest_review.get("criterion"), dict):
            criterion = digest_review["criterion"]
    elif isinstance(params.get("script_path"), str) and params.get("inline_review") is None:
        run_id = PurePosixPath(params["script_path"].replace("\\", "/")).stem
    if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
        return None
    stem = run_close.resolve_close_job_stem(root, run_id)
    record = run_close._load(root, stem) if stem else None
    plan = params.get("plan_path") or (record or {}).get("plan_rel")
    if not isinstance(plan, str) or not plan:
        return None
    out = {"plan_path": plan, "run_id": run_id}
    for key in ("task_output_path", "session_id"):
        if params.get(key) is not None:
            out[key] = params[key]
    if criterion:
        out["criterion"] = criterion
    return out


def _recorded_rejudge_met(root: Path, plan_rel: str, terminal_sha: str) -> Optional[dict]:
    """The ``met`` criterion (with its record as ``sidecar``) of the newest ``criterion-rejudge`` or
    delivery-verdict record bound to the plan and recorded after the terminal commit, else None.
    Never raises."""
    try:
        from datetime import datetime

        import yaml

        from coordinator_core.frontmatter.primitives import split_frontmatter
        from coordinator_core.git.run import git_out
        from coordinator_core.ops.dispatch_emit.verdict_supersession import latest_criterion_record

        text = _read_rel(root, plan_rel)
        split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
        plan_id = str((yaml.safe_load(split.fm_text) or {}).get("plan_id") or "") if split else ""
        stamp = git_out(["-C", str(root), "log", "-1", "--format=%cI", terminal_sha])
        if not plan_id or not stamp:
            return None
        found = latest_criterion_record(root, plan_id, datetime.fromisoformat(stamp.strip()))
        verdict = found["criterion"] if found else None
        if verdict and verdict.get("status") == "met" and str(verdict.get("observation") or "").strip():
            return {**verdict, "sidecar": found["record_path"]}
        return None
    except Exception:  # noqa: BLE001 -- no re-judge read leaves the held verdict standing
        return None


def _reclose(root: Path, reclose: dict) -> dict:
    """Answer a repeat call for a run whose landing receipt sha is on HEAD: ``already_landed``.

    The close-job record takes the call's ``criterion`` when it differs, and the record's unfinished
    legs are re-queued to ``close.background``. ``criterion_status`` is read back from the record,
    never judged here; the other close keys read ``deferred``. Writes no product and no commit."""
    from coordinator_core.ops.dispatch_emit import run_close

    run_id = reclose["run_id"]
    stem = run_close.resolve_close_job_stem(root, run_id)
    record = run_close._load(root, stem) if stem else None
    if stem is None or record is None:
        return _error(
            f"no close-job record at {run_close.close_job_path(run_id)} or "
            f"{run_close.close_job_path(run_id + '.workflow')}. A run that has not landed closes "
            f"through {contract.OP_NAME} with script_path.",
            refused="no-close-job",
        )
    plan = reclose["plan_path"]
    if str(record.get("plan_rel") or "").replace("\\", "/").removeprefix("./") != plan.replace("\\", "/").removeprefix("./"):
        return _error(
            f"{run_close.close_job_path(stem)} belongs to plan {record.get('plan_rel')!r}, not {plan!r}. "
            "Pass that plan's path.",
            refused="close-job-plan-mismatch",
        )
    sha = record.get("terminal_sha")
    if not isinstance(sha, str) or not landed_on_head(root, sha):
        return _error(
            f"terminal_sha {sha!r} is absent or not an ancestor of HEAD. Land the run's terminal commit first.",
            refused="terminal-commit-not-landed",
        )
    criterion = reclose.get("criterion")
    standing = criterion if isinstance(criterion, dict) else record.get("criterion")
    if not isinstance(standing, dict) or standing.get("status") != "met":
        criterion = _recorded_rejudge_met(root, plan, sha) or criterion
    _requeue_close(root, {}, key=stem, criterion=criterion)
    refreshed = run_close._load(root, stem) or record
    held = refreshed.get("criterion")
    reply: dict = {
        "committed": True, "sha": sha, ALREADY_LANDED_KEY: True, "rows": [], "paths": [],
        "error": None, "refused": None, "close": None,
        "review_stamp": "deferred", "plan_status": "deferred",
    }
    if isinstance(held, dict) and held.get("status"):
        reply["criterion_status"] = held["status"]
    return reply


def _missing_run_outcome(params: object, task_output: Optional[str] = None) -> Optional[dict]:
    """Refusal naming every run-outcome param the caller omitted, else None."""
    if not isinstance(params, dict) or not isinstance(params.get("script_path"), str):
        return None
    absent = [n for n in ("incomplete_chunks", "inline_review") if params.get(n) is None]
    if "incomplete_chunks" not in absent:
        return None
    if task_output:
        return _error(
            f"params {absent} not passed and task_output_path {task_output!r} supplies none: its "
            "next_action.params carries no run outcome (a close_job action names a landed run's close "
            "legs; re-pay them with a run_id that has a close-job record, no incomplete_chunks). Pass "
            "the run's own task-output file, or incomplete_chunks and inline_review from its digest",
            refused="missing-run-outcome",
            missing=absent,
        )
    return _error(
        f"params {absent} not passed. The run outcome is not derivable from script_path "
        f"{params['script_path']!r} and no params.task_output_path was given. Pass "
        "task_output_path=<the run's task-output file>, or incomplete_chunks and inline_review "
        "from the digest; a run that died before its review stage is re-emitted review-only "
        "(emit-dispatch-workflow --inventory <inv> --review-only --rows <DONE ids> --run-base <sha>)",
        refused="missing-run-outcome",
        missing=absent,
    )


def _delivery_not_rostered(inline_review: dict) -> bool:
    from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED

    return (
        not isinstance(inline_review.get("delivery"), dict)
        and inline_review.get("delivery_not_run") == DELIVERY_NOT_ROSTERED
    )


def _anchor_request(root: Path, params: dict, inline_review: Optional[dict]):
    """The zero-chunk request for a marker-less run whose review delivered PASS with no product
    file: it still owes the plan an ``Inline-Review`` record commit. None for any other shape."""
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest, plan_deliverable_id

    plan_rel = params.get("plan_path")
    if not isinstance(plan_rel, str) or not isinstance(inline_review, dict):
        return None
    delivery = inline_review.get("delivery")
    if _delivery_not_rostered(inline_review):
        delivery = inline_review.get("prep")
    elif not isinstance(delivery, dict) or delivery.get("verdict") != "PASS":
        return None
    if not isinstance(delivery, dict):
        return None
    files = delivery.get("product_files")
    if isinstance(files, bool) or not isinstance(files, int) or files != 0:
        return None
    plan_text = _read_rel(root, plan_rel)
    if plan_text is None:
        return None
    return CommitRequest(chunks=(), plan_path=plan_rel, deliverable_id=plan_deliverable_id(plan_text))


def _derive_judge_status(inline_review: object) -> Optional[str]:
    """Re-derive ``inline_review.criterion.status`` from its ``clauses``, in place: a clause that is
    not met downgrades a ``met`` reply, never the other way. Returns the deviation text or None."""
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None
    clauses = criterion.get("clauses")
    if not isinstance(clauses, list):
        return None
    statuses = [c.get("status") if isinstance(c, dict) else None for c in clauses]
    if "not_met" in statuses:
        derived, why = "not_met", "a clause is not_met"
    elif any(s != "met" for s in statuses):
        derived, why = "indeterminate", "a clause is not met or indeterminate"
    elif not statuses:
        derived, why = "indeterminate", "met reply carries no clauses"
    elif "observed" in criterion and not criterion["observed"]:
        derived, why = "indeterminate", "met reply observed nothing"
    else:
        return None
    note = f"judge reply status 'met' disagrees with its clauses: derived {derived} ({why})"
    criterion["status"] = derived
    criterion["reason"] = note
    criterion["deviations"] = [*(criterion.get("deviations") or []), note]
    return note


_RETHINK_EXITS = {
    "impossible": ("impossible", "impossible"),
    "first_officer": ("doctrine_conflict", "first_officer"),
    "doctrine_conflict": ("doctrine_conflict", "first_officer"),
    "ceiling": ("doctrine_conflict", "ceiling"),
}
#: Outcomes the workflow's terminal gate records in run-outcome.json that no loop-file line carries.
_RECORDED_EXIT_OUTCOMES = ("impossible", "doctrine_conflict")


def _read_loop_lines(path: Path) -> list:
    """The loop file's JSON object lines; an unreadable file or a bad line contributes nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _objective_gate(root: Path, run_id: object, inline_review: object) -> Optional[dict]:
    """Terminal objective gate for a run whose record dir holds objective.json, else None.

    Folds the loop with ``close_verdict``: done only on a passing last check; otherwise the close is
    the loop's recorded exit (impossible, or doctrine_conflict), an empty loop being doctrine_conflict
    (no-check); an unmet fold with no recorded exit reports exhausted, never done. Trap: no op appends
    `rethink` or `reacher` loop lines, so an impossible or doctrine_conflict exit reaches here only through
    the `objective_loop` the workflow wrote to run-outcome.json, which is read before it is overwritten.
    Settles ``inline_review.criterion.status`` in place to met or not_met, overriding any derived
    indeterminate. Returns ``{status, outcome, exit_reason, unmet_axes, summary, objective_loop}``.
    """
    from coordinator_core.ops.dispatch_emit import objective_contract as oc

    try:
        rdir = record_dir(root, run_id)
    except (ValueError, TypeError):
        return None
    objective_path = rdir / oc.OBJECTIVE_FILE
    if not objective_path.is_file():
        return None
    objective_readable = True
    try:
        axis_ids = [a.id for a in oc.Objective.from_json(json.loads(objective_path.read_text(encoding="utf-8"))).axes]
    except (OSError, ValueError, TypeError, AttributeError):
        axis_ids, objective_readable = [], False
    lines = _read_loop_lines(rdir / oc.LOOP_FILE)
    verdict = oc.close_verdict(lines, axis_ids)
    if not objective_readable:  # no declared axes to hold the last check to: never done
        verdict = {"status": oc.STATUS_NOT_MET, "unmet": verdict["unmet"], "reason": "objective-unreadable"}
    rethinks = [
        oc.Rethink(
            trigger=str(ln.get("trigger") or ""), approach_path=ln.get("approach_path") or ln.get("delta_plan_path"),
            verdict=str(ln.get("verdict") or ln.get("reason") or ""),
        )
        for ln in lines if ln.get("kind") == oc.LINE_RETHINK
    ]
    outcome = exit_reason = None
    if verdict["status"] != oc.STATUS_DONE:
        if verdict.get("reason") == oc.REASON_NO_CHECK:
            outcome, exit_reason = oc.OUTCOME_DOCTRINE_CONFLICT, "no-check"
        else:
            for rethink in reversed(rethinks):
                if rethink.verdict in _RETHINK_EXITS:
                    outcome, exit_reason = _RETHINK_EXITS[rethink.verdict]
                    break
            if outcome is None:
                recorded = (read_run_outcome(root, run_id) or {}).get("objective_loop")
                if isinstance(recorded, dict) and recorded.get("outcome") in _RECORDED_EXIT_OUTCOMES:
                    outcome, exit_reason = recorded["outcome"], recorded.get("exit_reason")
                    rethinks = [r for r in (recorded.get("rethinks") or []) if isinstance(r, dict)] or rethinks
            if outcome is None:
                outcome, exit_reason = oc.OUTCOME_EXHAUSTED, oc.EXIT_UNRESOLVED
    calibration = next((ln.get("calibration") for ln in reversed(lines) if ln.get("kind") == oc.LINE_CALIBRATION), None)
    result = oc.objective_loop_result(
        lines, calibration, rethinks=rethinks, outcome=outcome, exit_reason=exit_reason, axis_ids=axis_ids,
    )
    spends = [ln.get("spend_usd") for ln in lines if isinstance(ln.get("spend_usd"), (int, float))]
    summary = {
        "checks": sum(1 for ln in lines if ln.get("kind", oc.LINE_CHECK) == oc.LINE_CHECK),
        "reacher_iterations": sum(1 for ln in lines if ln.get("kind") == oc.LINE_REACHER),
        "spend_usd": max(spends) if spends else 0.0,
    }
    status = "met" if verdict["status"] == oc.STATUS_DONE else "not_met"
    if isinstance(inline_review, dict):
        crit = inline_review.get("criterion")
        crit = crit if isinstance(crit, dict) else {}
        # A judged not_met is never softened by a passing falsifier; its own reason stays.
        if status == "not_met" or crit.get("status") != "not_met":
            crit["status"] = status
        crit["unmet_axes"] = list(result["unmet_axes"])
        if status == "not_met":
            crit["reason"] = f"objective not met ({result['exit_reason']}): {', '.join(result['unmet_axes']) or 'no axes'}"
        inline_review["criterion"] = crit
        inline_review["objective_gate"] = {
            "outcome": result["outcome"], "exit_reason": result["exit_reason"],
            "unmet_axes": list(result["unmet_axes"]), **summary,
        }
    return {
        "status": status, "outcome": result["outcome"], "exit_reason": result["exit_reason"],
        "unmet_axes": list(result["unmet_axes"]), "summary": summary, "objective_loop": result,
    }


def _record_objective_outcome(root: Path, run_id: object, inline_review: object, gate: dict) -> None:
    """Write ``objective_loop`` (and the settled review) into run-outcome.json; best-effort."""
    if not isinstance(inline_review, dict):
        return
    try:
        existing = read_run_outcome(root, run_id) or {}
        write_run_outcome(root, run_id, {**existing, "inline_review": inline_review, "objective_loop": gate["objective_loop"]})
    except (ValueError, OSError):
        return


def _stage_returns(inline_review: dict) -> Optional[dict]:
    """The run record's stage-return fields out of the digest's ``inline_review``; None when the
    digest carries no ``prep`` block."""
    if not isinstance(inline_review.get("prep"), dict):
        return None
    out: dict = {k: inline_review.get(k) for k in ("prep", "delivery", "tests", "criterion")}
    if isinstance(inline_review.get("objective_gate"), dict):
        out["objective_gate"] = inline_review["objective_gate"]
    not_run = inline_review.get("delivery_not_run")
    if isinstance(not_run, str) and not_run:
        out["delivery_not_run"] = not_run
    wave_not_run = inline_review.get("wave_not_run")
    if isinstance(wave_not_run, list) and wave_not_run:
        out["wave_not_run"] = wave_not_run
    fixes = inline_review.get("fixes")
    if isinstance(fixes, int) and not isinstance(fixes, bool):
        out["fixes_applied"] = fixes
    integration = inline_review.get("integration")
    if isinstance(integration, dict):
        out["integration_sidecar"] = integration.get("sidecar")
        out["unresolved"] = integration.get("unresolved") or []
        out["confinement_violations"] = integration.get("confinement_violations") or 0
    return out


def _close_kind(plan_text: Optional[str]) -> tuple[str, bool]:
    """``(close kind, plan_governs)`` off the plan's frontmatter: a minted inventory spine is
    ``mise-run``, a minted XS spine ``xs-s-run`` with no governing plan, an S spec-dispatch plan
    ``xs-s-run`` with one, anything else ``plan-run``."""
    if plan_text is None:
        return "plan-run", True
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.dispatch_emit.plan_row_coding import MINTED_SPINE_ORIGIN

    split = split_frontmatter(plan_text.replace("\r\n", "\n"))
    if split is None:
        return "plan-run", True
    fields: dict = {}
    for name, raw in _KIND_FIELD.findall(split.fm_text):
        fields.setdefault(name, re.sub(r"[ \t]+#.*$", "", raw).strip().strip("\"'"))
    origin = fields.get("derived_from")
    if origin == MINTED_SPINE_ORIGIN:
        return "mise-run", False
    if origin == "sizing object":
        return "xs-s-run", False
    if fields.get("scope_mode") == "spec-dispatch":
        return "xs-s-run", True
    return "plan-run", True


def _cited_sizing(root: Path, plan_path: Optional[str]) -> Optional[str]:
    """Repo-relative path of the sizing the plan cites as ``sizing_object:`` when it is an existing
    file under the sizings home."""
    text = _read_rel(root, plan_path)
    if text is None:
        return None
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.session.record_homes import home_dir

    split = split_frontmatter(text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return None
    guarded = contained_path(root / cited, [Path(home_dir(str(root), "sizings"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(root.resolve()).as_posix()


def _resolving_sizing(root: Path, plan_path: Optional[str]) -> Optional[dict]:
    """``{id, tshirt}`` for the run's cited sizing, else None."""
    rel = _cited_sizing(root, plan_path)
    text = _read_rel(root, rel)
    if text is None:
        return None
    import yaml

    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    estimate = doc.get("estimate") if isinstance(doc.get("estimate"), dict) else {}
    return {
        "id": doc.get("deliverable_id") or PurePosixPath(rel).stem,
        "tshirt": estimate.get("tshirt") or "?",
    }


# ---- run token and repeat calls ---------------------------------------------------------------


def landed_on_head(root: Path, sha: str) -> bool:
    """True when ``sha`` is HEAD or an ancestor of HEAD; in process, False on anything unreadable."""
    try:
        from coordinator_core.git import commit_walk
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_state import head_sha

        head = head_sha(root, fresh=True)
        if not (isinstance(sha, str) and sha and head):
            return False
        if head == sha:
            return True
        common_dir = resolve_git_common_dir(root)
        target = commit_walk.commit_meta(common_dir, sha)
        if target is None:
            return False
        return any(found == sha for found, _c in commit_walk.walk_since(common_dir, head, target["committer_epoch"]))
    except Exception:  # noqa: BLE001 -- an unreadable object store answers "not landed"
        return False


def already_landed_reply(root: Path, run_id: object, token: object, script_path: object) -> Optional[dict]:
    """The reply for a repeat call whose first call landed (its spent receipt's sha is on HEAD), else
    None. Writes nothing."""
    from coordinator_core.ops.dispatch_emit import run_token

    receipt = run_token.spent_receipt(root, run_id, token, script_path)
    if receipt is None:
        return None
    sha = receipt.get("sha")
    if not isinstance(sha, str) or not landed_on_head(root, sha):
        return None
    return {
        "committed": True,
        "sha": sha,
        ALREADY_LANDED_KEY: True,
        "rows": list(receipt.get("rows") or []),
        "paths": list(receipt.get("paths") or []),
        "error": None,
        "refused": None,
        "close": None,
    }


_OVERLAP_REMEDY = (
    "A peer commit changed a path this landing writes; refire after the peer's commit, since the "
    "run's paths changed under it."
)


def _redeem_or_answer(
    root: Path, run_id: object, token: str, script_path: object, token_state: Optional[dict] = None
) -> Optional[dict]:
    """None when this call holds the claim (its identity lands in ``token_state["identity"]``); else the
    reply that ends it: ``already_landed`` from the spent receipt, ``run-token-in-flight`` for a claim a
    live, in-budget first call still holds (a dead or over-budget holder's claim is adopted by
    ``redeem_claim``), else ``run-token``."""
    from coordinator_core.ops.dispatch_emit import run_token

    budget = _claim_budget_secs()
    won = run_token.redeem_claim(root, run_id, token, script_path, budget_secs=budget)
    if won is None and _readmitted_reviewed(root, run_id, token, script_path):
        won = run_token.redeem_claim(root, run_id, token, script_path, budget_secs=budget)
    if won is not None:
        if token_state is not None:
            token_state["identity"] = won
        return None
    landed = already_landed_reply(root, run_id, token, script_path)
    if landed is not None:
        return landed
    if run_token.claim_pending(root, run_id, token, script_path):
        return _error(
            "a first call of this run's landing still holds the run token claim and has not finished; "
            "this call committed nothing",
            refused="run-token-in-flight",
        )
    return _error(
        "params.run_token does not match a live, unspent token minted for params.run_id and this "
        "script (a token admits one landed commit). A run cut after review: pass params.run_id with "
        "--from-task-output <the run's task-output file>; its recorded land params are used",
        refused="run-token",
    )


def _readmitted_reviewed(root: Path, run_id: object, token: str, script_path: object) -> bool:
    """True when the run's recorded land params carry this token and script and its retired
    receipt was restored to a live record, ready for one more `redeem`."""
    from coordinator_core.ops.dispatch_emit import run_token, terminal_commit_params_channel

    recorded = terminal_commit_params_channel.recorded_land_params(root, run_id)
    if recorded is None or recorded.get("run_token") != token or recorded.get("script_path") != script_path:
        return False
    return run_token.readmit_reviewed(root, run_id, token, script_path)


# ---- in-process tree reads --------------------------------------------------------------------


def _worktree_blob(root: Path, rel: str) -> Optional[str]:
    """The blob sha ``git add`` would write for ``rel``'s worktree bytes; ``"?"`` when its checkin
    filter cannot be reproduced (it then counts as differing); None when the file is absent."""
    from coordinator_core.git.commit import FilterUnsupported, _checkin_bytes

    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    try:
        body = _checkin_bytes(root, rel, data)
    except FilterUnsupported:
        return _FILTERED
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


_HEAD_SPINES = landing_halted_view._SPINES


def _head_entries(root: Path, paths: Sequence[str]) -> dict:
    """``{path: (mode, sha)}`` for ``paths`` in HEAD's tree, same answer as ``git_state.head_blobs``.
    HEAD's directory trees are kept per ``(root, HEAD sha)`` and a call reads only the directories
    no earlier call of the landing loaded, so a tree is read and parsed once however many path sets
    ask. Falls back to ``head_blobs`` when HEAD or a tree is unreadable."""
    from coordinator_core.git.git_state import head_blobs, head_sha

    wanted = list(dict.fromkeys(p for p in paths if p))
    if not wanted:
        return {}
    head = head_sha(root)
    found = landing_halted_view.tree_entries(root, head, wanted) if head is not None else None
    return head_blobs(root, wanted) if found is None else found


def _dirty_among(root: Path, paths: Sequence[str]) -> tuple[set, set, dict]:
    """``(changed, ignored, head)``: ``changed`` are the literals differing from HEAD (present with
    other bytes, absent from HEAD, or tracked and gone); ``ignored`` the changed untracked ones a
    gitignore pattern covers; ``head`` the HEAD ``{path: (mode, sha)}`` entries."""
    from coordinator_core.git.ignore_match import IgnoreMatcher

    literals = list(dict.fromkeys(p for p in paths if p))
    head = _head_entries(root, literals)
    changed: set = set()
    for p in literals:
        if (root / p).is_dir():
            continue
        now = _worktree_blob(root, p)
        entry = head.get(p)
        if now is None:
            if entry is not None:
                changed.add(p)
        elif now == _FILTERED or entry is None or now != entry[1]:
            changed.add(p)
    ignored: set = set()
    untracked = [p for p in changed if p not in head and (root / p).exists()]
    if untracked:
        matcher = IgnoreMatcher(root)
        ignored = {p for p in untracked if matcher.ignored(p, False) is not False}
    return changed, ignored, head


# ---- the marker -------------------------------------------------------------------------------


def _own_prefix_files(root: Path, chunk, cache: dict) -> Optional[list]:
    """Files ``chunk``'s own report claims under its own declared prefixes (whole path segments).

    None when the chunk declares prefixes but its report is unreadable or carries no list."""
    if not chunk.prefixes:
        return []
    if chunk.report not in cache:
        cache[chunk.report] = _read_rel(root, chunk.report)
    text = cache[chunk.report]
    from coordinator_core.ops.dispatch_emit.commit_request import parse_prefix_claims

    claims = parse_prefix_claims(text) if text is not None else None
    if claims is None:
        return None
    bounded = [(p, p if p.endswith("/") else p + "/") for p in chunk.prefixes]
    return [c for c in claims if any(c == p or c.startswith(under) for p, under in bounded)]


def _zero_path_prefix_dirty(root: Path, chunks: Sequence, landing: Collection[str], cache: dict) -> dict:
    """``{chunk id: dirty files under its prefixes}`` for a credited chunk that declares prefixes yet lands nothing.

    Only such chunks pay for one scoped status read over their prefixes; ignored files are dropped.
    A status read that cannot answer leaves the refusal unfired (the dirty set is unknowable)."""
    from coordinator_core.ops.dispatch_emit import credit_gate
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import worktree_snapshot

    landing = set(landing)
    zero: dict = {}
    for chunk in chunks:
        if not chunk.prefixes:
            continue
        bounds = [p.rstrip("/") + "/" for p in chunk.prefixes]
        own = [*chunk.paths, *(_own_prefix_files(root, chunk, cache) or [])]
        if any(p in landing for p in own) or any(p.startswith(u) for p in landing for u in bounds):
            continue
        zero[chunk.id] = list(chunk.prefixes)
    if not zero:
        return {}
    prefixes = sorted({p.rstrip("/") for ps in zero.values() for p in ps})
    snap = worktree_snapshot(root, [], prefixes, [])
    if snap is None:
        return {}
    found = sorted(p for p in snap.changed - landing if (root / p).is_file())
    dirty = set(found) - set(snap.ignored_among(found))
    return credit_gate.zero_path_prefix_credit(zero, {}, dirty)


def _manifest_declared(manifest_abs: Optional[Path]) -> tuple[list, Optional[str]]:
    """``(declared product paths, staging head)`` from the stage manifest: its rows' ``writes`` minus
    run-record paths and process state; ``([], None)`` when unreadable."""
    if manifest_abs is None:
        return [], None
    try:
        data = json.loads(Path(manifest_abs).read_text(encoding="utf-8"))
        rows = data.get("rows")
    except (OSError, ValueError, AttributeError):
        return [], None
    skip = (RECORD_DIR_ROOT.rstrip("/") + "/", RUN_DIR_ROOT.rstrip("/") + "/")

    def process_state(rel: str) -> bool:
        if not rel.startswith("state/"):
            return False
        parts = rel.split("/")
        return len(parts) < 3 or parts[1] in _PROCESS_STATE_CORPORA

    paths: list = []
    for row in rows if isinstance(rows, list) else ():
        writes = row.get("writes") if isinstance(row, dict) else None
        for w in writes if isinstance(writes, list) else ():
            if isinstance(w, str) and w and not w.startswith(skip) and not process_state(w) and w not in paths:
                paths.append(w)
    head = data.get("head_sha")
    return paths, head if isinstance(head, str) and _SHA40.fullmatch(head) else None


# ---- the ledger gate and #60 ------------------------------------------------------------------


def _verified_only(resume: object) -> dict:
    raw = resume.get("verified_only") if isinstance(resume, dict) else None
    out: dict = {}
    for row_id, ev in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(ev, dict):
            command, observed = ev.get("command"), ev.get("observed")
            if isinstance(command, str) and command.strip() and isinstance(observed, str) and observed.strip():
                out[str(row_id)] = {"command": command.strip(), "observed": observed.strip()}
    return out


def _path_changed(common: Path, root: Path, base: str, rel: str) -> Optional[bool]:
    """Whether ``rel``'s worktree blob differs from its blob at commit ``base``; None when the tree
    object cannot be read."""
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

    at_base = _blob_at_commit(common, base, rel)
    if at_base is None:
        return None
    now = _worktree_blob(root, rel)
    if now is None:
        return at_base is not ABSENT
    return at_base is ABSENT or now == _FILTERED or now != at_base


def _halt_base(root: Path, resume: dict, plan_rel: Optional[str], stage_head: Optional[str]) -> tuple:
    """``(base, source)`` from ``halt_baseline.since_halt_base``: the first halt of the resume's
    chain, never the halt commit the fire itself wrote."""
    from coordinator_core.ops.dispatch_emit.halt_baseline import since_halt_base

    return since_halt_base(root, resume, plan_rel, stage_head)


def _fence_reply(fence: dict) -> dict:
    """Reply keys for a ledger resume's fence: ``footprint_breach`` when paths were withheld,
    ``footprint: "unrecorded"`` when the resume has no footprint artifact; ``{}`` off a resume."""
    if fence.get("breach"):
        return {"footprint_breach": list(fence["breach"])}
    if fence and not fence.get("recorded"):
        return {"footprint": "unrecorded"}
    return {}


def _leg_footprint_writes(root: Path, run_dir: Path) -> Optional[list]:
    """The string ``writes`` of the run's ``LEG_FOOTPRINT_FILE`` (record dir first, then the run
    dir), or ``None`` when no readable file carries a ``writes`` list."""
    data = None
    for home in (record_dir(root, run_dir.name) if safe_id(run_dir.name) else run_dir, run_dir):
        try:
            data = json.loads((home / LEG_FOOTPRINT_FILE).read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue
    writes = data.get("writes") if isinstance(data, dict) else None
    if not isinstance(writes, list):
        return None
    return [w.replace("\\", "/") for w in writes if isinstance(w, str) and w]


def _remnant_record(root: Path, run_dir: Optional[Path]) -> Optional[dict]:
    """The run's ``recovery.json`` ``remnant`` object (its manifest dir, else its record dir); None when absent."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr

    if run_dir is None:
        return None
    for d in (run_dir, root / RECORD_DIR_ROOT / run_dir.name):
        try:
            remnant = json.loads((d / lr.RECOVERY_RECORD_FILE).read_text(encoding="utf-8")).get("remnant")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(remnant, dict):
            return remnant
    return None


def _remnant_breach(root: Path, run_dir: Path) -> list:
    """Paths the run's ``recovery.json`` ``remnant`` record shows the remnant-complete leg wrote
    outside its recorded footprint (``ledger_recovery.remnant_breach``); ``[]`` when absent."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr

    remnant = _remnant_record(root, run_dir)
    return lr.remnant_breach(remnant) if remnant is not None else []


def _resume_recorded_paths(root: Path, run_dir: Path, inline_review: object) -> list:
    """Repo-relative paths the run's earlier legs and review recorded as edited: the leg footprint's
    ``writes`` plus the review test-runner's ``edited``/``created`` where present."""
    from coordinator_core.ops.dispatch_emit.in_run_widen import widened_paths

    found = list(_leg_footprint_writes(root, run_dir) or ()) + widened_paths(run_dir)
    tests = inline_review.get("tests") if isinstance(inline_review, dict) else None
    if isinstance(tests, dict):
        for key in ("edited", "created"):
            listed = tests.get(key)
            if isinstance(listed, list):
                found.extend(p.replace("\\", "/") for p in listed if isinstance(p, str) and p)
    contained = (
        p for p in found
        if not PurePosixPath(p).is_absolute() and ".." not in PurePosixPath(p).parts and not re.match(r"^[A-Za-z]:", p)
    )
    return list(dict.fromkeys(contained))


def _footprint_fence(
    root: Path, run_dir: Path, resume: object, manifest_abs: Optional[Path], plan_rel: Optional[str],
) -> dict:
    """The ledger-resume footprint fence: ``{"recorded": False}`` when the run's record dir holds no
    readable ``LEG_FOOTPRINT_FILE``, else ``{"recorded": True, "breach": [paths]}`` naming each
    manifest-declared path changed since the halt base that the allowed set omits. The allowed set is
    the artifact's recorded ``writes`` plus the ``writes`` of the plan rows serving the blocking ids,
    read at landing (the re-plan leg appends rows after the artifact is written). No spawn: it reuses
    the in-process tree read of ``_work_since_halt``."""
    if not isinstance(resume, dict):
        return {"recorded": False}
    writes = _leg_footprint_writes(root, run_dir)
    if writes is None:
        return {"recorded": False}
    from coordinator_core.ops.dispatch_emit import footprint_breach
    from coordinator_core.ops.dispatch_emit.in_run_widen import widened_paths

    from coordinator_core.ops.dispatch_emit.write_baseline import predecessor_withheld

    allowed = set(writes) | set(widened_paths(run_dir)) | set(
        footprint_breach.serving_writes(root / plan_rel if plan_rel else None, resume.get("blocking") or [])
    ) | predecessor_withheld(root, _cited_sizing(root, plan_rel), run_dir)
    paths, stage_head = _manifest_declared(manifest_abs)
    base, _source = _halt_base(root, resume, plan_rel, stage_head)
    if base is None:
        return {"recorded": True, "breach": []}
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common = resolve_git_common_dir(root)
    breach = [p for p in paths if p not in allowed and _path_changed(common, root, base, p)]
    return {"recorded": True, "breach": breach}


def _work_since_halt(root: Path, base: str, declared: Sequence[str], withheld: dict) -> Optional[str]:
    """How a refire's declared product relates to the halt commit ``base``: ``"changed"`` when a
    declared path's worktree blob differs from the blob at ``base``; ``"withheld-pending"`` when none
    does but a path the halt withheld is dirty against HEAD (the halt left it with the halt state's own
    bytes, so it reads unchanged against ``base`` while it is the work to land); ``"unchanged"``
    otherwise; None when a tree object cannot be read."""
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common = resolve_git_common_dir(root)
    for p in declared:
        moved = _path_changed(common, root, base, p)
        if moved is None:
            return None
        if moved:
            return "changed"
    return "withheld-pending" if _withheld_pending(root, declared, withheld) else "unchanged"


def _withheld_pending(root: Path, declared: Sequence[str], withheld: dict) -> bool:
    """True when a declared path the halt withheld is dirty against HEAD. Independent of the halt
    base, so a caller can answer it before resolving the base."""
    pending = [p for p in declared if p in withheld]
    if not pending:
        return False
    head = _head_entries(root, pending)
    for p in pending:
        now = _worktree_blob(root, p)
        entry = head.get(p)
        if now is None:
            if entry is not None:
                return True
        elif entry is None or now == _FILTERED or now != entry[1]:
            return True
    return False


UNREADABLE_HALT_RECORD = "unreadable-halt-record"


def _read_markers_strict(root: Path, exclude_run: Optional[str], skip: frozenset = frozenset()) -> None:
    """Read every in-TTL sibling record dir's `halted.json` strictly, bar the run ids in `skip` (runs whose
    index entry already shows they withhold none of the landing); ValueError on a corrupt or
    unreadable one (absent is fine). `_unlanded_run_ids` cannot list such a run, so it is scanned here.
    Cost: one read per in-TTL record dir outside `skip`; the record-dir listing and stamps come from
    `halted_hold_release.record_stamps`, read once per `release_scope`."""
    import time

    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    now = time.time()
    base = Path(root) / RECORD_DIR_ROOT
    for name, stamp in halted_hold_release.record_stamps(root):
        if name == exclude_run or name in skip or (stamp is not None and now - stamp > run_token.TTL_SECONDS):
            continue
        halted_run.read_strict(base / name)


# `minted_before` no token-only run precedes: `_unlanded_run_ids` lists halted runs alone, as `foreign_halted_withheld` keeps only those.
_NO_SENIOR_TOKEN_RUN = (float("-inf"), "")


def foreign_halted_withheld(
    root: Path, run_id: Optional[str], plan_rel: Optional[str], paths: Sequence[str],
    holders_out: Optional[dict] = None,
) -> dict:
    """`{path: holding run id}` for each of `paths` another still-held halted run withholds.
    `holders_out`, when given, receives `{path: [every holding run id]}` for the returned paths.

    A landing run's own declaration never overrides this: the other run's hunks sit in the path. A halted
    run the landing releases (`halted_hold_release`) holds nothing; `_unlanded_run_ids` already drops it.
    A corrupt or unreadable `halted.json` (absent is not) in any unlanded run's record dir holds every
    wanted path (`UNREADABLE_HALT_RECORD`), as does an OSError or ValueError reading them; any other
    exception propagates.
    """
    wanted = [p for p in paths if p]
    if not wanted:
        return {}
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import run_ownership, sibling_claims

        from coordinator_core.ops.dispatch_emit import halted_hold_release

        _read_markers_strict(root, run_id, sibling_claims.index_clear_runs(root, wanted))
        # One batched cat-file for the kept blobs of halted runs withholding a wanted path: unscoped, release_reason spawns git per run.
        with halted_hold_release.blob_scope(root):
            halted_hold_release.prefetch_kept_blobs(root, sibling_claims.halted_records(root, run_id, wanted))
            unlanded = list(sibling_claims._unlanded_run_ids(
                root, run_id, plan_rel, minted_before=_NO_SENIOR_TOKEN_RUN, covering=wanted,
            ))
            # held_paths frees a withheld path whose kept hunks are already at HEAD (hand-committed).
            live = {
                rid: run_ownership.LiveRun(withheld=halted_hold_release.held_paths(root, rid, run_id, plan_rel))
                for rid in unlanded
                if warp_run_deliverable._halted(root, rid)
            }
        if not any(r.withheld for r in live.values()):
            return {}
        every = run_ownership.foreign_withheld_holders(run_id or "", wanted, live)
        out = {p: owners[0] for p, owners in every.items()}
        if out:
            # A phantom or criterion-met holder is released here, before its withhold is applied.
            from coordinator_core.ops.dispatch_emit import peer_hunk_gate

            kept = set(peer_hunk_gate._release_criterion_met(
                root, sorted(out.items()), landing_run_id=run_id, landing_plan=plan_rel,
            ))
            out = {p: rid for p, rid in out.items() if (p, rid) in kept}
        if holders_out is not None:
            holders_out.update({p: every[p] for p in out})
        return out
    except (OSError, ValueError):  # a hold that cannot be computed holds
        return {p: UNREADABLE_HALT_RECORD for p in wanted}


def _measured_released_holds(
    root: Path, measured: Optional[dict], foreign_held: Mapping, holders: Mapping,
) -> dict:
    """`{path: [halted holder ids]}` of the `foreign_held` paths whose worktree blob is the blob the run's
    measured-rev record carries: the verified tree already contains the halted holders' hunks, so the
    path lands whole. A path whose bytes differ from the record, or a record that is absent or
    unreadable, stays held. Hash-only: spawns nothing."""
    blobs = measured.get("blobs") if isinstance(measured, dict) and not measured.get("unreadable") else None
    if not isinstance(blobs, dict) or not foreign_held:
        return {}
    from coordinator_core.git.commit import checkin_blob_sha

    out: dict = {}
    for p in foreign_held:
        want = blobs.get(p)
        if not want or foreign_held[p] == UNREADABLE_HALT_RECORD:
            continue
        try:
            same = checkin_blob_sha(root, p) == want
        except Exception:  # noqa: BLE001 -- unknown bytes keep the hold
            same = False
        if same:
            out[p] = list(holders.get(p) or [foreign_held[p]])
    return out


def _merge_in_progress(root: Path) -> bool:
    """A merge commit cannot carry own-hunk staging, so a narrowed halted hold is not attempted over one."""
    try:
        from coordinator_core.git.git_dir import resolve_git_dir

        return (resolve_git_dir(root) / "MERGE_HEAD").exists()
    except Exception:  # noqa: BLE001 -- unknown keeps the whole-path hold
        return True


def _halted_only_release(
    root: Path, run_id: Optional[str], plan_rel: Optional[str], foreign_held: dict, declared: Sequence[str],
    minted_before: Optional[tuple],
) -> Optional[list]:
    """Ids of the halted runs whose hold on `foreign_held` the landing run `run_id` may override, else None.

    Armed only when `run_id` itself carries a halted marker (the door `record_halted_run_landed` takes),
    `halted_hold_release.halted_only_withholders` names every withholder and each is a halted run. A
    peer's claim never gates it (DR-474: claims are provenance; a halted run holds nothing). Whole paths
    only: the caller lands the held paths as ordinary candidates. Never raises; None keeps the exclusion.
    """
    if not foreign_held or not run_id:
        return None
    try:
        from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        marker = halted_run._read(record_dir(root, run_id))
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            return None
        holders = halted_hold_release.halted_only_withholders(root, run_id)
        if not holders or not set(foreign_held.values()) <= set(holders):
            return None
        return list(holders)
    except Exception:  # noqa: BLE001 -- unknown claim: the refusal stands
        return None


def _record_halted_auto_landed(root: Path, run_id: str, sha: str, session_id: Optional[str]) -> dict:
    """Close `run_id` as landed at `sha` through `dispatch.record_halted_run_landed`, in process. Never raises."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run_landed

        params = {"run_id": run_id, "sha": sha, **({"session_id": session_id} if session_id else {})}
        out = halted_run_landed._handler(params, root)
        return {"run_id": run_id, "ok": bool(out.get("ok")), **({"error": out["error"]} if out.get("error") else {})}
    except Exception as exc:  # noqa: BLE001 -- the commit stands
        return {"run_id": run_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _em_discharged_rows(resume: object, items: Sequence) -> frozenset:
    """Ids of ``met`` verdict items an EM-performed row discharged between halts: the resume's
    ``em_discharged`` map ``{plan row id: text}`` links to an item when the item's ``met_by`` names a
    discharged row id, or a discharge text names the item's id, as a whole token. The EM's landed
    commit is the work since the halt, so the row owes no diff. No git, no I/O. The same item
    must be ``blocking`` to be judged at all, so a discharge never credits a row outside the resume."""
    raw = resume.get(RESUME_LEDGER_EM_DISCHARGED_FIELD) if isinstance(resume, dict) else None
    if not isinstance(raw, dict) or not raw:
        return frozenset()

    def names(text: object, token: str) -> bool:
        return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", str(text)) is not None

    out: set = set()
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "met":
            continue
        rid, met_by = str(item.get("id")), item.get("met_by")
        met_text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by or "")
        if any(names(met_text, str(row)) or names(text, rid) for row, text in raw.items()):
            out.add(rid)
    return frozenset(out)


def _halt_gap_lines(root: Path, resume: object) -> dict:
    """``{row_id: {"remaining": [str]}}`` of a ledger resume's halt gap lines: the payload's
    ``remaining`` mapping when present, else the halt record's. File reads only; ``{}`` when unknown."""
    from coordinator_core.ops.dispatch_emit import ask_contract as ac

    if not isinstance(resume, dict):
        return {}
    payload = resume.get(ac.RESUME_LEDGER_REMAINING_FIELD)
    if isinstance(payload, dict) and payload:
        return payload
    run_id, halt_sha = resume.get("run_id"), resume.get("halt_sha")
    if not isinstance(run_id, str):
        return {}
    record = ac.read_halt_record(root, run_id, halt_sha if isinstance(halt_sha, str) else None)
    return dict(record.remaining) if record is not None else {}


def _unclosed_halt_gaps(resume: object, verdict: object, gap_lines: object, reverify: object = None) -> list:
    """``[{id, reason}]`` for each blocking row the verdict calls met or post_commit while one of its
    halt gap lines (stripped, exact) has no ``gaps_closed`` entry of the same gap with a non-empty
    ``how``. Pure; rows with no gap lines owe nothing.

    A gap line that is ``commit`` or one of the item's ``post_commit_acts`` is discharged by the commit itself.

    Trap: checks coverage only; ``how`` is the judge's prose and its truth is not verified here."""
    from coordinator_core.ops.dispatch_emit.ask_contract import GAPS_CLOSED_FIELD, HALT_GAP_UNCLOSED_REASON

    blocking = resume.get("blocking") if isinstance(resume, dict) else None
    items = verdict.get("items") if isinstance(verdict, dict) else None
    if not isinstance(blocking, list) or not isinstance(items, list) or not isinstance(gap_lines, dict):
        return []
    failures: list = []
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in blocking or item.get("status") not in ("met", "post_commit"):
            continue
        entry = gap_lines.get(str(item["id"]))
        lines = entry.get("remaining") if isinstance(entry, dict) else entry
        if not isinstance(lines, (list, tuple)):
            continue
        closed = item.get(GAPS_CLOSED_FIELD)
        covered = {
            str(c.get("gap")).strip() for c in (closed if isinstance(closed, list) else [])
            if isinstance(c, dict) and isinstance(c.get("gap"), str) and isinstance(c.get("how"), str) and c["how"].strip()
        }
        acts = item.get("post_commit_acts")
        discharged = {"commit", *(str(a).strip().lower() for a in (acts if isinstance(acts, list) else []))}
        for line in lines:
            if (
                isinstance(line, str) and line.strip() and line.strip() not in covered
                and line.strip().lower() not in discharged and not closed_by_reverify(line, reverify)
            ):
                failures.append({"id": item["id"], "reason": f"{HALT_GAP_UNCLOSED_REASON}: {line.strip()}"})
                break
    return failures


def _unchanged_since_halt(
    root: Path, resume: object, verdict: object, manifest_abs: Optional[Path],
    plan_rel: Optional[str], detail: dict, exclude: Sequence[str] = (),
) -> list:
    """``[{id, reason}]`` for each blocking row of a ledger resume the verdict calls met or
    post_commit while no declared product path counts as work since the halt commit.

    Base: ``halt_baseline.since_halt_base`` (the first halt of the chain). A
    ``verified_only`` row with a command and observation, and a resume whose ``unlanded`` is an empty
    list, owe no diff. ``detail`` receives ``{base, base_source, outcome}`` when rows are refused."""
    blocking = resume.get("blocking") if isinstance(resume, dict) else None
    items = verdict.get("items") if isinstance(verdict, dict) else None
    if not isinstance(blocking, list) or not isinstance(items, list):
        return []
    from coordinator_core.ops.dispatch_emit.pre_halt_credit import credited_rows

    credited = credited_rows(resume, verdict)
    post_commit = {
        str(i["id"]) for i in items
        if isinstance(i, dict) and str(i.get("id")) in credited and i.get("status") == "post_commit"
    }
    exempt = {*_verified_only(resume), *(credited - post_commit), *_em_discharged_rows(resume, items)}
    judged = [
        str(i["id"]) for i in items
        if isinstance(i, dict) and i.get("id") in blocking and i.get("status") in ("met", "post_commit")
        and str(i["id"]) not in exempt
    ]
    unlanded = resume.get("unlanded")
    if not judged or (isinstance(unlanded, list) and not unlanded):
        return []
    paths, stage_head = _manifest_declared(manifest_abs)
    paths = [p for p in paths if p not in exclude]
    # Withheld work pending against HEAD clears every row whatever the chain base is, so the
    # base's `git log` walk is skipped; a resume with no base candidate still resolves it below.
    from coordinator_core.ops.dispatch_emit.halt_baseline import _sha

    if paths and (stage_head or _sha(resume.get("since_sha")) or _sha(resume.get("halt_sha"))) and _withheld_pending(
        root, paths, _withheld_of(root, resume)
    ):
        return []
    base, source = _halt_base(root, resume, plan_rel, stage_head)
    if post_commit and base is not None:
        from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import verified_post_commit_credit

        verified = verified_post_commit_credit(root, base, items, sorted(post_commit & set(judged)))
        judged = [i for i in judged if i not in verified]
        if not judged:
            return []
    outcome = "no-base" if base is None else "no-declared-paths" if not paths else "unchanged"
    if outcome == "unchanged":
        state = _work_since_halt(root, base, paths, _withheld_of(root, resume))
        if state in ("changed", "withheld-pending"):
            return []
        outcome = "tree-unreadable" if state is None else "unchanged"
        if outcome == "unchanged" and not exclude:  # a footprint breach is dirty work: never "already landed"
            from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import CITED_DETAIL_KEY, cited_at_or_before

            cited = cited_at_or_before(root, base, items, judged)
            if cited and all(i in cited for i in judged):
                shas = set(cited.values())
                detail[CITED_DETAIL_KEY] = cited
                detail["landed_sha"] = next(iter(shas)) if len(shas) == 1 else base
                detail["landed_paths"] = list(paths)
            else:
                from coordinator_core.ops.dispatch_emit.halt_landing import landed_before_halt

                owners = [x for x in (plan_rel, resume.get("plan"), resume.get("run_id")) if isinstance(x, str)]
                landed = landed_before_halt(root, base, paths, owners)
                if landed:
                    detail["landed_sha"] = landed
                    detail["landed_paths"] = list(paths)
    detail.update(base=base, base_source=source if base else None, outcome=outcome)
    return [{"id": i, "reason": UNCHANGED_SINCE_HALT_REASON} for i in judged]


def _recovery_chunks(root: Path, ask_run_dir: Optional[Path]) -> dict:
    """The ``chunks`` map of the run's ``recovery.json`` (its manifest dir, else its record dir); ``{}`` when absent."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr

    if ask_run_dir is None:
        return {}
    for d in (ask_run_dir, root / RECORD_DIR_ROOT / ask_run_dir.name):
        try:
            chunks = json.loads((d / lr.RECOVERY_RECORD_FILE).read_text(encoding="utf-8")).get("chunks")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(chunks, dict):
            return chunks
    return {}


def _recovery_backing(chunks: dict, ids: Sequence[str]) -> tuple[dict, list]:
    """``({chunk: open-row reason}, unbacked ids)`` for ``params.recovery_withheld``. A chunk is backed when
    ``recovery.json`` records it exhausted (at the attempt cap), blocked or held. The record is written by
    the re-verify agent: this catches a param the record does not support, not an agent misreporting its
    own attempts."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
    from coordinator_core.ops.dispatch_emit import self_correction as sc

    reasons: dict = {}
    unbacked: list = []
    for cid in ids:
        entry = chunks.get(cid)
        status = entry.get("status") if isinstance(entry, dict) else None
        attempts = entry.get("attempts") if isinstance(entry, dict) else None
        last = str(entry.get("last_reason") or "") if isinstance(entry, dict) else ""
        if status == lr.STATUS_EXHAUSTED and attempts == lr.RECOVERY_ATTEMPT_CAP:
            reasons[cid] = f"{lr.CAP_EXHAUSTED} ({attempts} attempts): {last}"
        elif (
            status == lr.STATUS_EXHAUSTED
            and type(attempts) is int
            and 1 <= attempts <= lr.RECOVERY_ATTEMPT_CAP
            and last == sc.NO_PROGRESS
        ):
            reasons[cid] = f"{sc.NO_PROGRESS} ({attempts} attempts): {last}"
        elif status == lr.STATUS_BLOCKED and isinstance(attempts, int):
            reasons[cid] = f"{lr.FIX_BLOCKED} ({attempts} attempts): {last}"
        elif status == lr.STATUS_HELD:
            reasons[cid] = f"{lr.HELD}: {last}"
        else:
            unbacked.append(cid)
    return reasons, unbacked


_IMPORT_TARGET = re.compile(r"; (\S+) is (?:uncommitted in the worktree|absent from HEAD|deleted by this commit)")


def _recovery_dependents(root: Path, request, incomplete: set, backed: dict, chunks: dict) -> dict:
    """``{chunk: import-closure-open}`` for every other finished chunk whose ``.py`` imports a path a
    recovery-withheld chunk owns, transitively. Edges come from the landing import scan over the chunks
    still landing, rescanned until no chunk is added (a dependent's own importers leave the landing too)."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    rows = [{"id": c.id, "writes": list(c.paths)} for c in request.chunks]
    status = {cid: chunks.get(cid) for cid in backed}
    found: dict = {}
    while True:
        live = sorted({
            p for c in request.chunks if c.id not in incomplete and c.id not in found
            for p in c.paths if p.endswith(".py") and (root / p).is_file()
        })
        if not live:
            return found
        edges = []
        for f in li.import_closure_findings(root, live, []):
            m = _IMPORT_TARGET.search(f.detail)
            if f.path and m:
                edges.append((f.path, m.group(1)))
        owners = {**status, **{cid: lr.STATUS_HELD for cid in found}}
        added = {
            cid: why for cid, why in lr.landing_withheld(owners, rows, edges).items()
            if why == lr.IMPORT_CLOSURE_OPEN and cid not in incomplete and cid not in found
        }
        if not added:
            return found
        found.update(added)


def _ledger_gate(
    root: Path, run_dir: Path, verdict: object, manifest_abs: Optional[Path], gated: Sequence[str],
    operator: dict, plan_text: Optional[str], inline_review: dict, resume_ledger: object,
    plan_rel: Optional[str], fence: Optional[dict] = None, recovery_reasons: Optional[dict] = None,
    code_paths: Sequence[str] = (), advisories: Optional[list] = None,
) -> tuple[Optional[dict], object, list]:
    """``(refusal or None, ledger, open rows)`` for a ledger-bearing ask run: the refusal when the
    verdict does not pass ``requirements_ledger.judge`` against the live register rows (a not_met row a
    gated chunk withholds passes and returns in ``open``), or when a ledger resume has no work since its
    halt. A run emitted without a ledger is not gated. ``recovery_reasons`` is ``{chunk: reason}`` of the
    chunks ``recovery.json`` backs (``_recovery_backing``): they extend ``gated`` and their open rows carry
    that reason. ``advisories`` (out-param) receives the judge's ignored-verdict-id advisory line."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger
    from coordinator_core.ops.dispatch_emit.engine_owned_rows import routed_roots

    try:
        ledger = requirements_ledger.read_ledger(root, run_dir)
    except requirements_ledger.LedgerRefused as exc:
        return _error(
            f"the run's bound sizing is unreadable: {exc}. Restore the sizing and re-fire",
            refused="ledger-unreadable",
        ), None, []
    if ledger is None:
        return None, None, []
    held_refusal = held_revision_refusal(root, ledger, run_dir)
    if held_refusal is not None:
        return held_refusal, ledger, []
    if not isinstance(verdict, dict):
        return _error(
            "the run is ledger-bearing but params.ledger_verdict is absent: the final ledger "
            "checker's verdict gates this commit. Re-run the run's ledger check and pass its "
            "verdict, or revise with workflow.revise and re-run the check",
            refused="ledger-unjudged",
        ), ledger, []
    try:
        manifest_data = json.loads(Path(manifest_abs).read_text(encoding="utf-8")) if manifest_abs else None
        routed = routed_roots(manifest_data) if manifest_abs else {}
    except (OSError, ValueError):
        manifest_data, routed = None, {}
    recovery_reasons = recovery_reasons or {}
    result = requirements_ledger.judge(
        ledger, verdict, routed, gated=[*gated, *recovery_reasons], operator=operator, repo_root=root, plan_text=plan_text,
        wave_reviews=[w for w in inline_review.get("wave_reviews") or [] if isinstance(w, dict)],
        noted_paths=requirements_ledger.noted_paths(root, run_dir),
        code_paths=code_paths, review_verdicts=review_floor.verdict_count(inline_review, root),
        note_ids=requirements_ledger.note_ids(run_dir),
    )
    from coordinator_core.ops.dispatch_emit import measured_rev

    try:
        drifted = measured_rev.foreign_drift(run_dir, root)
        drift_reason = f"measured-rev-drift: {', '.join(drifted)}"
    except (OSError, ValueError):
        drifted, drift_reason = ["<record>"], "measured-rev-record-unreadable"
    if drifted:
        items = verdict.get("items")
        stale_met = [
            {"id": i.get("id"), "reason": drift_reason}
            for i in (items if isinstance(items, list) else [])
            if isinstance(i, dict) and i.get("status") == "met"
        ]
        if stale_met:
            result = {**result, "pass": False, "failures": [*result["failures"], *stale_met]}
    baseline = manifest_data.get("write_baseline") if isinstance(manifest_data, dict) else None
    if baseline and result["pass"]:
        from coordinator_core.ops.dispatch_emit import own_checkpoint_paths
        from coordinator_core.ops.dispatch_emit.write_baseline import (
            pre_existing_dirt,
            predecessor_withheld,
            predecessor_wrote,
            prior_pass_written,
            unwritten,
        )

        mrows = [r for r in manifest_data.get("rows") or [] if isinstance(r, dict)]
        prep = inline_review.get("prep")
        base = prep.get("run_base_sha") if isinstance(prep, dict) else None
        dirt: list = []
        prior_credit: list = []

        def prior() -> frozenset:
            """Credit from earlier passes and same-sizing halts; read only once a path shows as dirt."""
            if not prior_credit:
                prior_credit.append(
                    prior_pass_written(root, run_dir)
                    | predecessor_withheld(root, _cited_sizing(root, plan_rel), run_dir)
                    | predecessor_wrote(root, run_dir, baseline)
                )
            return prior_credit[0]

        for it in verdict.get("items") or []:
            if not isinstance(it, dict) or it.get("status") not in ("met", "post_commit"):
                continue
            claimed = [r for r in mrows if str(it.get("id")) in (r.get("register_rows") or [])]
            if not claimed or any(r.get("kept_from") or r.get("adopts_stranded") for r in claimed):
                continue
            writes = {w for r in claimed for w in r.get("writes") or [] if isinstance(w, str)}
            if not writes:
                continue
            if len(unwritten(root, baseline, writes)) != len(writes):
                continue
            foreign = pre_existing_dirt(root, baseline, writes, frozenset())
            if foreign:
                try:
                    own = own_checkpoint_paths.checkpoint_landed_paths(
                        root, plan_rel, base if isinstance(base, str) else None, sorted(writes),
                    )
                except Exception:  # noqa: BLE001 -- unreadable history proves no own pass
                    own = frozenset()
                foreign = pre_existing_dirt(root, baseline, writes, own) if own else foreign
            if foreign:
                foreign = pre_existing_dirt(root, baseline, writes, {*own, *prior()})
            if foreign:
                dirt.append({"id": it.get("id"), "reason": "pre-existing-dirt: " + ", ".join(sorted(foreign))})
        if dirt:
            result = {**result, "pass": False, "failures": [*result["failures"], *dirt]}
    since_halt: dict = {}
    remnant_breach = _remnant_breach(root, run_dir)
    fenced: dict = {}
    if resume_ledger is None and remnant_breach:
        fenced = {"recorded": True, "breach": remnant_breach}
        if fence is not None:
            fence.update(fenced)
    if resume_ledger is not None:
        from coordinator_core.ops.dispatch_emit.pre_halt_credit import with_notes

        resume_ledger = with_notes(resume_ledger, requirements_ledger.read_notes(run_dir))
        fenced = _footprint_fence(root, run_dir, resume_ledger, manifest_abs, plan_rel)
        fenced = {**fenced, "breach": list(dict.fromkeys([*(fenced.get("breach") or ()), *remnant_breach]))} if remnant_breach else fenced
        if fence is not None:
            fence.update(fenced)
        stale = _unchanged_since_halt(
            root, resume_ledger, verdict, manifest_abs, plan_rel, since_halt, fenced.get("breach") or (),
        )
        stale = [*stale, *_unclosed_halt_gaps(
            resume_ledger, verdict, _halt_gap_lines(root, resume_ledger), inline_review.get("reverify"),
        )]
        if stale:
            result = {**result, "pass": False, "failures": [*result["failures"], *stale]}
    if fenced.get("breach"):
        from coordinator_core.ops.dispatch_emit import footprint_breach

        rows = manifest_data.get("rows") if isinstance(manifest_data, dict) else None
        row_writes = {
            str(r["id"]): [w for w in r.get("writes") or () if isinstance(w, str)]
            for r in rows if isinstance(r, dict) and r.get("id") is not None
        } if isinstance(rows, list) else {}
        items = verdict.get("items")
        on_breach = footprint_breach.met_on_breach(
            items if isinstance(items, list) else [], fenced["breach"], row_writes,
        )
        if on_breach:
            result = {**result, "pass": False, "failures": [*result["failures"], *on_breach]}
    advisory = requirements_ledger.advisory_text(result)
    if advisory is not None and advisories is not None:
        advisories.append(advisory)
    if result["pass"]:
        return None, ledger, [
            {**r, "reason": recovery_reasons.get(str(r["reason"])[len("withheld_by "):], r["reason"])}
            if str(r.get("reason", "")).startswith("withheld_by ") else r
            for r in result.get("open") or []
        ]
    message = "the requirements are not met: " + "; ".join(
        (f"{f['id']}: {f['reason']}" if f["id"] is not None else f["reason"]) for f in result["failures"][:10]
    )
    from coordinator_core.ops.dispatch_emit.halt_landing import since_halt_close

    landed = since_halt_close(since_halt, result["failures"], verdict)
    if landed:
        return {
            "committed": True, "sha": landed, ALREADY_LANDED_KEY: True,
            "paths": since_halt["landed_paths"], "since_halt": since_halt,
            "error": None, "refused": None, "close": None,
        }, ledger, []
    return _error(
        message,
        refused="ledger-unmet",
        ledger_failures=result["failures"],
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
        awaiting_operator=[
            {"id": r["id"], "row": r["row"], "command": r["command"]} for r in result["open"] if "command" in r
        ],
        **({"since_halt": since_halt} if since_halt else {}),
    ), ledger, []


def _ledger_met_ids(verdict: object) -> list:
    items = verdict.get("items") if isinstance(verdict, dict) else None
    return [
        str(i["id"]) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("status") == "met" and i.get("id") is not None
    ]


# ---- grading ----------------------------------------------------------------------------------


def _undone_summary(text: str) -> str:
    lines = text.splitlines()
    picked = [ln.strip() for ln in lines if _UNDONE_LINE_RE.match(ln)]
    if not picked:
        for i, ln in enumerate(lines):
            if _CAUSE_HEADING_RE.match(ln.strip()):
                body = []
                for nxt in lines[i + 1:]:
                    if nxt.lstrip().startswith("#"):
                        break
                    if nxt.strip():
                        body.append(nxt.strip())
                picked = [ln.strip().lstrip("#").strip() + ":"] + body if body else []
                break
    if not picked:
        picked = [
            ln.strip() for ln in lines
            if re.match(r'\s*"?[*_`]{0,2}PARTIAL', ln) or re.match(_STATUS_FIELD + "PARTIAL", ln, re.I)
        ]
    return " | ".join(picked)[:_UNDONE_CAP]


def _is_delivered(text: str) -> bool:
    return bool(_DELIVERED_REPORT_RE.search(text)) and not _UNDELIVERED_REPORT_RE.search(text)


def _reviewed_files(inline_review: dict, *, need_pass: bool) -> Optional[set]:
    """``prep.slice_files`` when the review has no unresolved finding or confinement violation (and,
    with ``need_pass``, a PASS delivery verdict); None otherwise. Absent evidence is not a pass."""
    prep = inline_review.get("prep")
    delivery = inline_review.get("delivery")
    integration = inline_review.get("integration")
    unrostered = need_pass and _delivery_not_rostered(inline_review)
    if not (isinstance(prep, dict) and isinstance(integration, dict)):
        return None
    if need_pass and not (unrostered or isinstance(delivery, dict)):
        return None
    files = prep.get("slice_files")
    if (
        (need_pass and not unrostered and delivery.get("verdict") != "PASS")
        or integration.get("unresolved") or integration.get("confinement_violations")
        or not isinstance(files, list)
    ):
        return None
    return {f for f in files if isinstance(f, str)}


def _grade(
    root: Path, request, incomplete: set, inline_review: dict, ledger, verdict: object, held: set,
    artifacts, record_dir_rel: Optional[str], run_dir: Optional[Path], sizing_rel: Optional[str],
    cache: dict, manifest=None,
) -> dict:
    """``{chunk id: reason}`` for incomplete chunks the run's own evidence clears: ``outside-footprint``
    (the executor's ``gate-blocker: outside-footprint`` line, every file in a passed review's slices),
    ``ledger-met`` (a passed ledger plus a clean review, report readable and not BLOCKED) and
    ``credited:`` (the EM's ``workflow.revise credit_chunks``; a hold still bars it)."""
    from coordinator_core.ops.dispatch_emit import credit_gate, gate_owed

    out: dict = {}
    reviewed = _reviewed_files(inline_review, need_pass=True)
    clean = _reviewed_files(inline_review, need_pass=False)
    met = _ledger_met_ids(verdict)
    ledger_ids: list = []
    ledger_files: dict = {}
    blocked = set(held)
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(root, chunk.report)
        prefix_files = _own_prefix_files(root, chunk, cache)
        if reviewed is not None and text is not None and _PARTIAL_REPORT_RE.search(text) and prefix_files is not None:
            blocker = gate_owed.match(text, gate_owed.KIND_OUTSIDE_FOOTPRINT)
            files = set(chunk.paths) | set(prefix_files)
            if blocker is not None and files and files <= reviewed:
                out[chunk.id] = f"outside-footprint{blocker[1].rstrip()}"[:_UNDONE_CAP]
                continue
        if ledger is None or (text is not None and _is_delivered(text)):
            continue
        ledger_ids.append(chunk.id)
        if text is None or prefix_files is None:
            blocked.add(chunk.id)
            continue
        own = set(chunk.paths) | set(prefix_files)
        if _BLOCKED_REPORT_RE.search(text) and not credit_gate.remnant_supersedes_blocked(
            own, _remnant_record(root, run_dir)
        ):
            blocked.add(chunk.id)
        else:
            ledger_files[chunk.id] = own
    reason = ("ledger-met: " + ", ".join(met))[:_UNDONE_CAP]
    if clean is not None:
        from coordinator_core.ops.review_stamp import PRODUCT_FILE_EXCLUDED_PREFIXES

        # review.prep's slice_files never list a non-product path, so a review cannot cover one.
        clean = clean | {
            f for fs in ledger_files.values() for f in fs if f.startswith(PRODUCT_FILE_EXCLUDED_PREFIXES)
        }
    for cid in credit_gate.ledger_regradable(ledger_ids, blocked, ledger_files, clean):
        out.setdefault(cid, reason)

    credits = _read_credits(root, artifacts, record_dir_rel, run_dir, sizing_rel)
    if credits:
        known = []
        for chunk in request.chunks:
            if chunk.id not in incomplete or chunk.id not in credits or chunk.id in out:
                continue
            text = _read_rel(root, chunk.report) if chunk.report else None
            if text is None or not _is_delivered(text):
                known.append(chunk.id)
        for cid, evidence in credit_gate.credit_regradable(known, credits, held).items():
            out[cid] = f"credited: {evidence}"[:_UNDONE_CAP]
    if ledger is not None:
        for cid in _writes_empty_ledger_met(root, request, incomplete, verdict, held, manifest):
            out.setdefault(cid, reason)
    return out


def _writes_empty_ledger_met(root: Path, request, incomplete: set, verdict: object, held: set, manifest) -> list:
    """Incomplete rows that write nothing (a verification row) whose every register row the ledger marks
    ``met`` with ``met_by`` evidence: the ledger is the only judge of such a row, so it is complete."""
    if manifest is None or not isinstance(verdict, dict):
        return []
    items = verdict.get("items")
    met = {
        str(i["id"]) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("status") == "met" and i.get("id") is not None and i.get("met_by")
    }
    if not met:
        return []
    rows = {r.id: r for r in manifest.rows}
    out: list = []
    for chunk in request.chunks:
        row = rows.get(chunk.id)
        if (
            chunk.id not in incomplete or chunk.id in held or row is None or chunk.paths or chunk.prefixes
            or row.writes or row.kept_from or row.adopts_stranded or not row.register_rows
            or not set(row.register_rows) <= met
        ):
            continue
        text = _read_rel(root, chunk.report) if chunk.report else None
        if text is not None and _BLOCKED_REPORT_RE.search(text):
            continue
        out.append(chunk.id)
    return out


def _read_credits(root: Path, artifacts, record_dir_rel: Optional[str], run_dir: Optional[Path], sizing_rel: Optional[str]) -> dict:
    """``{row id: evidence}`` the EM credited via ``workflow.revise credit_chunks``: record dirs, the run dir, the sizing."""
    from coordinator_core.ops.dispatch_emit import run_credit

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    sources = [root / d for d in reversed(dirs)]
    if run_dir is not None:
        sources.append(run_dir)
    credits = run_credit.read(sources)
    if sizing_rel:
        credits = {**run_credit.credits_for_sizing(root, sizing_rel), **credits}
    return credits


def _credited_absent_chunks(
    plan_text: Optional[str], request, credits: dict, held: set, incomplete: set
) -> list:
    """``ChunkCommit`` per plan row the EM credited that the request does not carry: a refire's request
    names only the rows it re-ran, so a row credited from the first fire would otherwise never land.
    Rows closed on the spine (``NON_DISPATCHABLE_DISPOSITIONS``) and held rows are skipped; a credited row
    still ``open`` whose files already landed is re-listed, and lands nothing."""
    if not credits or plan_text is None:
        return []
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit import credit_gate
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit
    from coordinator_core.ops.dispatch_emit.spine_read import (
        NON_DISPATCHABLE_DISPOSITIONS,
        load_rows_memo,
        with_canonical_disposition,
    )

    loaded = load_rows_memo(plan_text)
    if loaded.status is not LocateStatus.LOCATED:
        return []
    present = {c.id for c in request.chunks}
    out: list = []
    for raw in loaded.rows:
        row = with_canonical_disposition(raw)
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        rid = row["id"]
        if rid in present or rid in incomplete or row.get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
            continue
        if rid not in credit_gate.credit_regradable([rid], credits, held):
            continue
        writes = row.get("writes")
        paths = tuple(
            PurePosixPath(p.replace("\\", "/")).as_posix()
            for p in (writes if isinstance(writes, list) else ()) if isinstance(p, str) and p.strip()
        )
        if paths:
            out.append(ChunkCommit(id=rid, title=str(row.get("title") or rid), paths=paths))
    return out


def _partial_chunks(root: Path, request, incomplete: set, blockers: dict) -> list:
    """Incomplete chunks whose executor itself reported DONE: the row is incomplete only because the
    delivery verdict failed, and its files were reviewed with the wave. A PARTIAL row's undone text is
    recorded in ``blockers``."""
    out: list = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(root, chunk.report)
        if text is None:
            continue
        if _is_delivered(text):
            out.append(chunk)
        elif _PARTIAL_REPORT_RE.search(text):
            blockers[chunk.id] = _undone_summary(text)
    return out


def _released_ids(params: dict) -> set:
    """Chunk ids named in ``params.release_held``: held entries dropped from the withholding."""
    rel = params.get("release_held")
    return {r for r in (rel if isinstance(rel, list) else []) if isinstance(r, str)}


def _held_ids(params: dict) -> set:
    held = params.get("held")
    ids = {h["chunk"] for h in (held if isinstance(held, list) else []) if isinstance(h, dict) and isinstance(h.get("chunk"), str)}
    return ids - _released_ids(params)


def _held_reasons(params: dict) -> dict:
    held = params.get("held")
    released = _released_ids(params)
    return {
        h["chunk"]: h.get("reason") or ""
        for h in (held if isinstance(held, list) else [])
        if isinstance(h, dict) and isinstance(h.get("chunk"), str) and h["chunk"] not in released
    }


def _held_write_paths(root: Path, request, held_ids: set) -> dict:
    """``{path: [held chunk ids]}`` of every ``writes:`` path a held chunk declares: the marker's
    chunk paths plus the plan spine row of the same id."""
    out: dict = {}
    for chunk in request.chunks:
        if chunk.id in held_ids:
            for p in chunk.paths:
                out.setdefault(p, []).append(chunk.id)
    text = _read_rel(root, request.plan_path)
    if text is not None:
        import yaml

        from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

        located = locate_fenced_block(text.replace("\r\n", "\n"))
        try:
            rows = (yaml.safe_load(located.body) or []) if located.status == LocateStatus.LOCATED else []
        except yaml.YAMLError:
            rows = []
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and str(row.get("id")) in held_ids:
                for p in row.get("writes") or ():
                    if isinstance(p, str):
                        ids = out.setdefault(PurePosixPath(p.replace("\\", "/")).as_posix(), [])
                        if str(row["id"]) not in ids:
                            ids.append(str(row["id"]))
    return out


def _reverted_paths(params: dict, inline_review: object) -> frozenset:
    """Paths the run's test-runner or review recorded as reverted; a reverted path absent from the
    worktree is a revert, never a declared deletion."""
    holders: list = [params.get("targeted_tests")]
    if isinstance(inline_review, dict):
        holders += [inline_review.get("tests"), inline_review.get("integration")]
    found: set = set()
    for holder in holders:
        listed = holder.get("reverted") if isinstance(holder, dict) else None
        if isinstance(listed, list):
            found.update(p.replace("\\", "/") for p in listed if isinstance(p, str) and p)
    return frozenset(found)


# ---- peer holds -------------------------------------------------------------------------------


def _own_run_seniority(root: Path, run_id: object, script_path: object) -> Optional[tuple]:
    """``(minted_at, run_id)`` of the landing run's claimed token, else its emission receipt's
    ``emitted_at`` (same wall clock as a token's ``minted_at``); None when neither resolves. Never a
    file mtime. Seniority drops only a junior peer's declaration."""
    from coordinator_core.ops.dispatch_emit import run_token

    if isinstance(run_id, str):
        record = run_token._read(run_token._record_path(root, run_id, claimed=True))
        if record is not None and isinstance(record.get("minted_at"), (int, float)):
            return (record["minted_at"], run_id)
    if not isinstance(script_path, str) or not script_path:
        return None
    script = Path(script_path)
    script = script if script.is_absolute() else root / script
    key = run_id if isinstance(run_id, str) else script.stem
    from datetime import datetime

    from coordinator_core.ops.dispatch_emit.emission_receipt import _read_receipt

    receipt = _read_receipt(script)
    try:
        return (datetime.fromisoformat(receipt["emitted_at"]).timestamp(), key)
    except (TypeError, KeyError, ValueError):
        return None


def _submitter_session(root: Path, run_id: object) -> str:
    """The session that submitted the run (its stage record's ``session_id``), else ``""``. One read."""
    if not isinstance(run_id, str):
        return ""
    try:
        from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage

        sid = (read_run_stage(root, run_id) or {}).get("session_id")
    except Exception:  # noqa: BLE001 -- an unreadable stage record names no submitter
        return ""
    return sid if isinstance(sid, str) else ""


def _firing_sessions(root: Path, run_id: object, session_id: Optional[str]) -> set:
    """Sessions that are this landing's own hand: the landing caller, the run's submitting session, the session on the run's token and the run's recorded emitter."""
    own = {session_id} if session_id else set()
    submitter = _submitter_session(root, run_id)
    if submitter:
        own.add(submitter)
    if isinstance(run_id, str):
        try:
            from coordinator_core.ops.dispatch_emit import run_token
            from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

            emitter = recorded_emitter(root, run_id)
            if emitter:
                own.add(emitter)

            for claimed in (True, False):
                record = run_token._read(run_token._record_path(root, run_id, claimed=claimed))
                sid = record.get("session_id") if isinstance(record, dict) else None
                if isinstance(sid, str) and sid:
                    own.add(sid)
        except Exception:  # noqa: BLE001 -- an unreadable token names no extra self session
            pass
    return own


def _prior_pass_sessions(root: Path, run_id: object) -> set:
    """Sessions the run's own records name as having acted for it in an earlier pass: the stage-record
    session (and its ``adopted_from``), token session and recorded emitter of the run and of each
    ``resumed_from`` predecessor. One small read per hop, capped by the ledger hop cap; no spawn."""
    out: set = set()
    if not isinstance(run_id, str):
        return out
    try:
        from coordinator_core.ops.dispatch_emit import run_token
        from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage, record_dir
        from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

        chain: tuple = ()
        try:
            # `_resumed_chain` reads only `run.json`: no ledger file, no predecessors, and no ledger import.
            if (record_dir(root, run_id) / "run.json").is_file():
                from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

                chain = tuple(_resumed_chain(root, run_id))
        except ValueError:  # an unsafe id names no ledger
            chain = ()
        for rid in (run_id, *chain):
            stage = read_run_stage(root, rid) or {}
            out.update(s for s in (stage.get("session_id"), *(stage.get("adopted_from") or ())) if isinstance(s, str) and s)
            emitter = recorded_emitter(root, rid)
            if emitter:
                out.add(emitter)
            for claimed in (True, False):
                record = run_token._read(run_token._record_path(root, rid, claimed=claimed))
                sid = record.get("session_id") if isinstance(record, dict) else None
                if isinstance(sid, str) and sid:
                    out.add(sid)
    except Exception:  # noqa: BLE001 -- an unreadable record names no extra self session
        pass
    return out


def _peer_held_left(peer_held: dict) -> dict:
    """``{"peer_held_left": [{path, sessions}]}`` for every peer-held path, sorted; empty when none."""
    if not peer_held:
        return {}
    return {"peer_held_left": [{"path": p, "sessions": sorted(s)} for p, s in sorted(peer_held.items())]}


PEER_CLAIM_LOOKUP_DEADLINE_S = 0.3


def _bounded_claim_lookup(claim_index, paths: list, cwd: str) -> dict:
    """`claim_index.lookup` on a daemon thread held to `PEER_CLAIM_LOOKUP_DEADLINE_S` of wall time;
    a stalled read answers every path UNANSWERABLE (no hold) while the thread finishes on its own."""
    import threading

    box: list = []
    done = threading.Event()

    def work() -> None:
        try:
            box.append(claim_index.lookup(paths, cwd=cwd))
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            box.append(exc)
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    if not done.wait(PEER_CLAIM_LOOKUP_DEADLINE_S):
        return {path: [claim_index.UNANSWERABLE] for path in paths}
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


def _peer_held(
    root: Path, paths: Sequence[str], own_sid: Optional[str], run_id: object, script_path: object,
    *, plan_rel: Optional[str] = None, run_base: Optional[str] = None, minted_before: Optional[tuple] = None,
    self_sessions: Collection = (), declaration_only: Optional[set] = None,
) -> dict:
    """``{path: [peer session ids]}`` (DR-462) a live peer session's touch claim or a senior live peer
    run's declared writes holds over ``paths``; ``minted_before`` is ``_own_run_seniority``'s answer.
    ``declaration_only`` is an out-set: it receives the held paths no live touch claim holds.
    A touch claim by ``own_sid`` or any of ``self_sessions`` (the run's firing session) is self, never a
    peer; another run's declared writes still hold even from that session, except runs minted under the
    run's submitting session (`_submitter_session`), which are this run's siblings and never hold it.
    A bare declaration never holds a path this run's own checkpoint landed; a live touch claim still
    does. A per-run artifact (`co_declared_settle.is_per_run_artifact`) is never held by a run declaration:
    the hunk gate settles it. A claim read that fails answers no hold."""
    held: dict = {}
    if not paths:
        return held
    try:
        from coordinator_core.session import claim_index, liveness, touch_record

        if own_sid is None:
            from coordinator_core.session import core as session_core

            try:
                own_sid = session_core.resolve_session_id(str(root))
            except Exception:  # noqa: BLE001 -- an unresolved caller holds nothing of its own
                own_sid = None
        found = _bounded_claim_lookup(claim_index, list(paths), str(root))
        for path in paths:
            claimants = found.get(path, [])
            if claim_index.UNANSWERABLE in claimants:
                continue
            kinds = (getattr(found, "recorded_kind", None) or {}).get(path, {})
            for sid in claimants:
                if sid == own_sid or sid in self_sessions or not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                    continue
                try:
                    live = liveness.session_live(sid, str(root))
                    abandoned = liveness.session_abandoned(sid, str(root)) if live else False
                except Exception:  # noqa: BLE001 -- indeterminate liveness holds nothing
                    continue
                if live and not abandoned:
                    held.setdefault(path, []).append(sid)
    except Exception:  # noqa: BLE001 -- advisory: a failed lookup holds nothing, and no reply key reports the gap
        pass
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import own_checkpoint_paths

        claims = warp_run_deliverable.live_run_write_claims(
            root, list(paths), exclude_session=_submitter_session(root, run_id), cwd=str(root),
            minted_before=minted_before,
            exclude_run=run_id if isinstance(run_id, str) else None,
        )
        own = (
            own_checkpoint_paths.checkpoint_landed_paths(root, plan_rel, run_base, list(claims or ()))
            if claims else frozenset()
        )
    except Exception:  # noqa: BLE001 -- see above
        claims = None
        own = frozenset()
    from coordinator_core.ops.dispatch_emit.co_declared_settle import is_per_run_artifact

    for path, sids in sorted((claims or {}).items()):
        if path in own or is_per_run_artifact(path):
            continue
        if declaration_only is not None and path not in held:
            declaration_only.add(path)
        for sid in sids:
            if sid not in held.setdefault(path, []):
                held[path].append(sid)
    return held


# ---- blob drift -------------------------------------------------------------------------------


from coordinator_core.ops.dispatch_emit.terminal_commit_review import (  # noqa: E402 -- the single own-fix helper copy
    _epoch_of,
    _fixes_integrated_at,
    _reviewer_applied_fixes,
    _wave_slice_files,
)


CHANGED_AFTER_REVIEW = "changed-after-review"


def _changed_after_review(root: Path, rel: str, reviewed_hash: object) -> bool:
    """True when the worktree bytes of a reviewed path no longer match its ``reviewed_sha256`` entry."""
    from coordinator_core.session import touch_record

    return isinstance(reviewed_hash, str) and touch_record.compute_content_hash(root / rel) != reviewed_hash


def _recorded_fingerprints(
    root: Path, session_id: str, candidates: Sequence[str], carried: Sequence[str], *, excluded: set,
    reviewed: Optional[dict], reviewed_at: Optional[float], fixes_integrated_at: Optional[float],
    wave_paths: frozenset, review_events: Optional[dict] = None, run_started_at: Optional[float] = None,
) -> dict:
    """``{path: sha256}`` the run expects each candidate path (of ``carried``) to carry: the single
    ``terminal_commit_review._recorded_fingerprints`` with no deleted paths."""
    from coordinator_core.ops.dispatch_emit.terminal_commit_review import _recorded_fingerprints as single

    return single(
        root, session_id, candidates, carried, (), excluded=excluded, reviewed=reviewed,
        reviewed_at=reviewed_at, fixes_integrated_at=fixes_integrated_at, wave_paths=wave_paths,
        review_events=review_events, run_started_at=run_started_at,
    )


def _submitted_at(root: Path, run_id: Optional[str]) -> Optional[float]:
    """The run's ``submission.json`` ``submitted_at`` epoch, else None."""
    from coordinator_core.ops.dispatch_emit.register_gate import run_started_at

    return run_started_at(root, run_id)


def _review_since(root: Path, run_id: Optional[str], inline_review: object) -> Optional[float]:
    """Epoch seconds the run began: its ``submission.json`` ``submitted_at``, else the committer time of
    its base commit (the review prep's ``run_base_sha``, else the recorded run base). None when neither
    resolves."""
    from coordinator_core.ops.dispatch_emit.register_gate import RESOLUTION_RUN_ID, run_started_at

    started = run_started_at(root, run_id)
    if started is not None:
        return started
    prep = inline_review.get("prep") if isinstance(inline_review, dict) else None
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _SIZING_OBJECT_RESOLUTION.match(base)) and run_id and RESOLUTION_RUN_ID.match(run_id):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(root, run_id)
    if not (isinstance(base, str) and _SIZING_OBJECT_RESOLUTION.match(base)):
        return None
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    epoch = (commit_meta(resolve_git_common_dir(root), base) or {}).get("committer_epoch")
    return float(epoch) if isinstance(epoch, int) and not isinstance(epoch, bool) else None


def _resumed_review_source(root: Path, inline_review: object, run_id: Optional[str], session_id: Optional[str]) -> tuple:
    """``(run_id, session_id)`` whose review writes this landing reads: the halted run named by
    ``inline_review[RESUMED_RUN_ID_KEY]`` with its recorded emitter when that id is safe and its record
    dir exists; else the caller's own pair."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RESUMED_RUN_ID_KEY, record_dir

    resumed = inline_review.get(RESUMED_RUN_ID_KEY) if isinstance(inline_review, dict) else None
    if not (isinstance(resumed, str) and resumed and safe_id(resumed)):
        return run_id, session_id
    try:
        if not record_dir(root, resumed).is_dir():
            return run_id, session_id
    except ValueError:
        return run_id, session_id
    from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

    return resumed, recorded_emitter(root, resumed) or session_id


def _review_write_events(root: Path, session_id: Optional[str], run_id: Optional[str], inline_review: object) -> dict:
    """``{path: (sha256, epoch)}`` of each path a review agent of this run wrote: the newest hashed write
    in that agent's sink, one sink walk per distinct agent. A path whose agent's newest write is hashless,
    or whose source is unreadable, has no entry."""
    if not session_id:
        return {}
    from coordinator_core.ops.dispatch_emit import review_edit_source
    from coordinator_core.session import core, touch_record

    read_run_id, read_session = _resumed_review_source(root, inline_review, run_id, session_id)
    try:
        writes = review_edit_source.session_review_writes(root, read_session, _review_since(root, read_run_id, inline_review))
        hub = core.sessions_dir(str(root)) if writes else None
    except Exception:  # noqa: BLE001 -- an unreadable source is "no agent events", never a refusal
        return {}
    if not writes or not hub:
        return {}
    by_agent: dict = {}
    for path, agent_id in writes.items():
        by_agent.setdefault(agent_id, set()).add(path.replace("\\", "/").removeprefix("./"))
    out: dict = {}
    for agent_id, paths in by_agent.items():
        newest: dict = {}
        try:
            for member in touch_record.discover_family(touch_record.sink_path(Path(hub) / ".agents" / agent_id)):
                for line in touch_record.iter_complete_lines(member.read_bytes()):
                    try:
                        ev = touch_record.decode_line(line)
                    except touch_record.MalformedRecordLine:
                        continue
                    if (
                        ev.verb == touch_record.VERB_TOUCH and ev.kind != touch_record.KIND_READ
                        and ev.path in paths and ev.timestamp >= newest.get(ev.path, (None, float("-inf")))[1]
                    ):
                        newest[ev.path] = (ev.content_hash, ev.timestamp)
        except OSError:
            continue
        out.update({p: v for p, v in newest.items() if v[0]})
    return out


def _reconcile_prep(
    root: Path, session_id: Optional[str], run_id: Optional[str], request, inline_review: object
) -> tuple:
    """Replace the relayed ``inline_review["prep"]`` with the reply ``review.prep`` stored on disk for
    this run, in place: disk truth the engine wrote wins over a review agent's trimmed copy.

    Returns ``(reviewed_sha256, recorded_at_epoch, matched)``: ``matched`` is true only when a stored
    reply replaced the prep; ``({}, None, False)`` otherwise."""
    none: tuple = ({}, None, False)
    if not isinstance(inline_review, dict) or not session_id or not run_id:
        return none
    prep = inline_review.get("prep")
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _SIZING_OBJECT_RESOLUTION.match(base)):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(root, run_id)
    if not base:
        return none
    from coordinator_core.ops import review_prep

    reply = review_prep.stored_reply_for_run(
        root, session_id, base, run_key=run_id,
        within=[p for c in request.chunks for p in c.paths], covering=True,
    )
    if reply is None:
        return none
    inline_review["prep"] = {
        "run_base_sha": reply["run_base_sha"],
        "product_files": reply["product_files"],
        "foreign_claims": reply["foreign_claims"],
        "slice_files": [f for s in reply["slices"] or [] if isinstance(s, dict) for f in s.get("files") or []],
    }
    inline_review["prep_sidecar"] = reply["sidecar_path"]
    reviewed = reply.get("reviewed_sha256")
    return (reviewed if isinstance(reviewed, dict) else {}), _epoch_of(reply.get("recorded_at")), True


# ---- trailers ---------------------------------------------------------------------------------


def _run_closed_lines(root: Path, run_id: object, params: dict) -> list:
    """``Checkpoint-Run-Closed:`` lines for every base the landing run's checkpoints carry; empty when none resolves."""
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines
    from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

    return run_closed_trailer_lines(closing_bases(root, run_id if isinstance(run_id, str) else None, params))


def _review_trailer(record_abs: Optional[Path]) -> list:
    """``["Review: <sha256 of the record>"]`` when the machine-local review record is readable, else ``[]``."""
    if record_abs is None:
        return []
    try:
        return [f"Review: {hashlib.sha256(record_abs.read_bytes()).hexdigest()}"]
    except OSError:
        return []


def _ask_trailers(root: Path, plan_path: Optional[str], record_abs: Optional[Path], receipts: list) -> list:
    """The trailers an ask run's product commit carries in place of the process records it leaves
    out: ``Falsifier: <pytest node>``, ``Review: <sha256>`` of the review record and one
    ``Completion-Receipt: <id>`` each."""
    out = []
    plan_text = _read_rel(root, plan_path)
    if plan_text is not None:
        from coordinator_core.ops.dispatch_emit.emit import _prime_exit_criterion_falsifier

        falsifier = _prime_exit_criterion_falsifier(plan_text) or {}
        node = _PYTEST_NODE.search(str(falsifier.get("how") or ""))
        if node:
            out.append(f"Falsifier: {node.group(0)}")
    out.extend(_review_trailer(record_abs))
    if receipts:
        from coordinator_core.completion_receipts.model import RECEIPT_TRAILER

        out.extend(f"{RECEIPT_TRAILER}: {fm['receipt_id']}" for fm, _prose in receipts)
    return out


def _subject(contributing: list, fallback: str) -> str:
    ids = ", ".join(c.id for c in contributing)
    titles = "; ".join(c.title for c in contributing if c.title.strip())
    if not ids:
        return fallback
    return f"{ids}: {titles}" if titles else ids


# ---- the close job ----------------------------------------------------------------------------


def _leg(status: str, detail: str = "", root=None):
    from coordinator_core.ops.dispatch_emit.run_close import LegResult, scrub_detail

    return LegResult(status, scrub_detail(detail, root))


def _launch_close_job(root: Path, key: str, batons: list, memo: Optional[str]) -> bool:
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    script = engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py"
    params = json.dumps({"claimed_batons": list(batons), "source_memo": memo, "run_id": key})
    return spawn_detached(
        str(engine), str(script), [key, "--root", str(root), "--params", params], site_free=True,
    )


def _close_job_run_base(root: Path, prep_base: object, run_id: Optional[str]) -> Optional[str]:
    """The 40-hex base the close-job record carries: the review prep's ``run_base_sha``, else the base the
    run's first bind recorded. None when neither is a full sha."""
    if isinstance(prep_base, str) and _SHA40.fullmatch(prep_base):
        return prep_base
    if not run_id:
        return None
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

    recorded = recorded_run_base(root, run_id)
    return recorded if isinstance(recorded, str) and _SHA40.fullmatch(recorded) else None


def _close_job(root: Path, ctx: dict) -> dict:
    """Write the run's close-job record, launch the job, and return ``{leg: LegResult}`` for each
    leg: queued ones as the record reads them, preset ones ``not-applicable``."""
    from datetime import datetime, timedelta

    from coordinator_core.ops.ceremony import close_background
    from coordinator_core.ops.dispatch_emit.completion_receipt import _handoff_claims
    from coordinator_core.ops.dispatch_emit.run_close import read_close_job, write_close_job
    from coordinator_core.ops.dispatch_emit.source_memo_ref import resolve_source_memo

    key, presets = ctx["key"], ctx["presets"]
    legs: dict = dict(presets)
    queue = [leg for leg in close_background.BACKGROUND_LEGS if leg not in presets]
    memo = resolve_source_memo(root, ctx["plan_rel"])
    batons = list(_handoff_claims(root, ctx.get("session_id")))
    own = _run_own_baton(root, ctx["sibling_run_id"])
    if own and own not in batons:
        batons.append(own)
    deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
    try:
        write_close_job(
            root, key, queue, deadline,
            session_id=ctx.get("session_id"), plan_rel=ctx["plan_rel"], source_memo=memo,
            claimed_batons=batons, review_receipt=ctx.get("review_receipt"),
            run_base_sha=ctx.get("run_base_sha"), terminal_sha=ctx["sha"],
            criterion=ctx.get("criterion"), falsifier_broken=ctx.get("falsifier_broken"),
            inputs=ctx["inputs"],
        )
    except (OSError, ValueError) as exc:
        legs.update({leg: _leg("failed", f"close job record not written: {type(exc).__name__}") for leg in queue})
        return legs
    if not _launch_close_job(root, key, batons, memo):
        legs.update({leg: _leg("failed", "close job did not start") for leg in queue})
        return legs
    legs.update(read_close_job(root, key, datetime.now().astimezone(), queue))
    return legs


def _run_own_baton(root: Path, run_id: str) -> Optional[str]:
    """The baton the run's gate minted and recorded, when it is a file inside the handoffs home."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton
    from coordinator_core.session.record_homes import home_dir

    rel = read_run_baton(root, run_id)
    if rel is None:
        return None
    target = contained_path(root / rel, [Path(home_dir(str(root), "handoffs"))])
    return rel if target is not None and target.is_file() else None


def _still_unlanded(root: Path, inputs: dict) -> list:
    """The record's ``inputs.unlanded`` rows whose recorded paths still differ from HEAD, in one status
    read for every row. A row with no recorded paths, or a status that cannot be read, stays unlanded."""
    rows = [str(r) for r in inputs.get("unlanded") or []]
    recorded = inputs.get("unlanded_paths")
    recorded = recorded if isinstance(recorded, dict) else {}
    paths = sorted({p for r in rows for p in (recorded.get(r) or []) if isinstance(p, str)})
    if not paths:
        return rows
    from coordinator_core.git.scoped_worktree_status import scoped_worktree_status

    snap = scoped_worktree_status(root, paths, [], [])
    if snap is not None:
        dirty = set(snap[0])
    else:
        from coordinator_core.git.run import git_out

        out = git_out(["-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all", "--", *paths])
        if out is None:
            return rows
        dirty = set()
        entries = iter(out.split("\0"))
        for entry in entries:
            if len(entry) > 3:
                dirty.add(entry[3:])
                if entry[0] in "RC":
                    next(entries, None)  # -z rename/copy: the source path is its own NUL field
    return [r for r in rows if not recorded.get(r) or any(p in dirty for p in recorded[r])]


def _requeue_close(root: Path, params: dict, *, key: Optional[str] = None, criterion: Optional[dict] = None) -> None:
    """A repeat call after a landing re-queues the close legs the first call's job never recorded and
    refreshes the record's ``criterion`` when this call carries a different one. ``key`` names the
    close-job record (default: the call's script stem); ``criterion`` the call's own verdict (default:
    ``params.inline_review.criterion``). Never raises."""
    try:
        from datetime import datetime, timedelta

        from coordinator_core.ops.ceremony import close_background
        from coordinator_core.ops.dispatch_emit import run_close

        key = key or PurePosixPath(str(params.get("script_path", "")).replace("\\", "/")).stem
        path = root / run_close.close_job_path(key)
        if not path.is_file():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        legs = record.get("legs") if isinstance(record.get("legs"), dict) else {}
        if criterion is None:
            review = params.get("inline_review")
            criterion = review.get("criterion") if isinstance(review, dict) else None
        criterion = criterion if isinstance(criterion, dict) else None
        recorded_inputs = record.get("inputs")
        recorded_unlanded = recorded_inputs.get("unlanded") if isinstance(recorded_inputs, dict) else None
        still_unlanded = _still_unlanded(root, recorded_inputs) if recorded_unlanded else None
        cleared = still_unlanded is not None and len(still_unlanded) < len(recorded_unlanded)
        if criterion is not None and still_unlanded:
            criterion = {**criterion, "status": "not_met"}
        unfinished = close_background.owed_on_reclose(
            {leg: v.get("status") for leg, v in legs.items() if isinstance(v, dict)}
        )
        newer = criterion is not None and criterion != record.get("criterion")
        if not unfinished and not newer and not cleared:
            return
        deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
        run_close.amend_close_job(
            root, key, criterion=criterion if newer else None, queue=unfinished, deadline=deadline,
            unlanded=still_unlanded if cleared else None,
        )
        batons = record.get("claimed_batons") if isinstance(record.get("claimed_batons"), list) else []
        if unfinished:
            _launch_close_job(root, key, batons, record.get("source_memo"))
    except Exception:  # noqa: BLE001 -- the landing stands; the close legs read as failed
        return


# ---- the product landing ----------------------------------------------------------------------


def _recorded_post_checkpoint(
    root: Path, run_ids: Sequence[object], ask_run_dir: Optional[Path]
) -> dict:
    """``{path: run id}`` of the ``post_checkpoint`` hunks recorded in kept.json of each named run's
    record dir (``ask_run_dir`` stands in for the landing run's own). An unsafe or absent dir reads as none."""
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    out: dict = {}
    for run_id in dict.fromkeys(r for r in run_ids if isinstance(r, str) and r):
        try:
            rdir = ask_run_dir if ask_run_dir is not None and ask_run_dir.name == run_id else record_dir(root, run_id)
        except ValueError:
            continue
        for rel in kept_work.read(rdir)[kept_work.POST_CHECKPOINT_KEY]:
            path = kept_work._norm(rel)
            if path:
                out.setdefault(path, run_id)
    return out


def _land_product(root: Path, repo_root: Path, params: dict, token_state: dict) -> dict:
    """Land one run's product; see the module. ``token_state`` carries the claimed run token."""
    stranded: dict = {}
    reply = _land_inner(root, repo_root, params, stranded, token_state)
    close = token_state.pop("since_halt_close", None)
    if close is not None:
        if reply.get("committed"):
            reply = {**reply, "since_halt": close["since_halt"]}
            if not reply.get("paths"):
                reply[ALREADY_LANDED_KEY] = True
        elif reply.get("nothing_to_commit") and not reply.get("refused") and not reply.get("error"):
            reply = {**reply, **close, "nothing_to_commit": False}
    if reply.get("refused") not in _STRANDED_UNKNOWN_REFUSALS and not reply.get(ALREADY_LANDED_KEY):
        reply.setdefault("stranded", {c: sorted(ps) for c, ps in stranded.items()})
    return _with_peer_drift(root, reply)


_PLAIN_REF = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")


def _peer_ref_present(root: Path, drift_mod) -> bool:
    """False only when the peer-publish ref is a plain ``remote/branch`` name that no loose ref
    file or ``packed-refs`` line carries, so the ``git rev-list`` could only fail (no drift). Any
    other shape, or a reftable store, answers True and leaves the probe to git. Zero spawns."""
    _, common = drift_mod._git_dirs(Path(root))
    ref = drift_mod._configured_ref(Path(root)) or drift_mod.DEFAULT_PEER_PUBLISH_REF
    if common is None or not _PLAIN_REF.fullmatch(ref) or (common / "reftable").exists():
        return True
    names = [f"refs/remotes/{ref}", f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/{ref}"]
    if any((common / n).is_file() for n in names):
        return True
    try:
        packed = (common / "packed-refs").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    wanted = set(names)
    return any(line.rsplit(" ", 1)[-1] in wanted for line in packed if line and line[0] not in "#^")


def _with_peer_drift(root: Path, reply: dict) -> dict:
    """Attach ``peer_publish_drift`` and a fold note when HEAD lacks commits on the peer-publish ref.

    Surface only, never a refusal; a failed probe leaves the reply untouched.
    """
    try:
        # Dynamic on purpose: the percolate package is withheld from the OSS mirror, and a static
        # import here makes every bin CLI reaching land_run a publish-deny (publish-allowlist-generate
        # --verify-bin-deny). The ImportError on a clone is absorbed by the except below.
        import importlib

        drift_mod = importlib.import_module("coordinator_core.percolate.peer_publish_drift")
        if not _peer_ref_present(root, drift_mod):
            return reply
        drift = drift_mod.peer_publish_drift(root)
    except Exception:
        return reply
    if drift is None:
        return reply
    return {
        **reply,
        "peer_publish_drift": {"ref": drift.ref, "missing": drift.missing, "sample": list(drift.sample)},
        "peer_publish_drift_note": f"HEAD lacks {drift.missing} commit(s) on {drift.ref}; fold with: git merge {drift.ref}",
    }


LANDING_TREE_DIFFERS = "landing-tree-differs-from-measured"
VERIFY_OVERLAY_RED = "verify-overlay-red"
VERIFY_OVERLAY_UNVERIFIED = "verify-overlay-unverified"
_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


def _verify_reach_dirt(root: Path, record: dict, product: Sequence[str], deleted: Sequence[str]) -> dict:
    """Dirty tracked paths the landing set's tests can read, outside the landing set: the record's
    ``reach_dirt`` unioned with a land-time check. Attribution is the caller's (`_split_reach_dirt`)."""
    from coordinator_core.ops.dispatch_emit.test_reach import foreign_dirt, reach_paths, seeds_for_landing

    landing = {*product, *deleted}
    recorded = record.get("reach_dirt") if isinstance(record.get("reach_dirt"), dict) else {}
    dirt = {p: b for p, b in recorded.items() if isinstance(p, str) and p not in landing}
    dirt.update(foreign_dirt(root, reach_paths(root, seeds_for_landing(root, landing)), landing))
    return dirt


def _split_reach_dirt(
    root: Path, reach_dirt: dict, art_run_id: Optional[str], plan_rel: Optional[str], own_sid: Optional[str],
    rid: object, script_path: object, **peer_kw,
) -> tuple[dict, dict]:
    """``(refusing, foreign)``: the reach dirt no run claims, and ``{path: holder}`` for dirt another unlanded
    run holds (live claim or declared writes) or withholds as halted. Only the unattributed remainder refuses."""
    paths = sorted(reach_dirt)
    foreign = {p: str(h) for p, h in foreign_halted_withheld(root, art_run_id, plan_rel, paths).items()}
    held = _peer_held(root, [p for p in paths if p not in foreign], own_sid, rid, script_path, plan_rel=plan_rel, **peer_kw)
    foreign.update({p: ", ".join(sorted(s)) for p, s in held.items() if s})
    return {p: b for p, b in reach_dirt.items() if p not in foreign}, foreign


def _measured_record(root: Path, run_id: Optional[str]) -> Optional[dict]:
    """The run's measured-rev record; None when it has none, ``{"unreadable": True}`` when it will not parse."""
    if not run_id:
        return None
    try:
        record = measured_rev_record_path(root, run_id)
    except ValueError:
        return None
    if not record.is_file():
        return None
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _measured_strs(record: Optional[dict], key: str) -> list:
    value = record.get(key) if record else None
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _run_owned(root: Path, run_id: Optional[str]):
    """The run's own artifacts (delivery note, sizings, record dir); written after the measured-rev
    record, so never evidence of landing-tree drift."""
    from coordinator_core.ops.dispatch_emit import run_artifact_ownership

    return run_artifact_ownership.own_artifact_paths(root, run_id) if run_id else run_artifact_ownership.OwnArtifacts()


def _hunk_blocks(base: list, new: list) -> "collections.Counter":
    import collections

    from coordinator_core.git.since_base_overlap import _opcodes

    return collections.Counter(
        (tuple(base[i1:i2]), tuple(new[j1:j2])) for tag, i1, i2, j1, j2 in _opcodes(base, new) if tag != "equal"
    )


def _own_hunks(root: Path, rel: str, base_blob: str, content: bytes) -> Optional["collections.Counter"]:
    """The run's own hunks in *content*: its changes over the write baseline, minus the changes that
    only revert the baseline's peer dirt toward HEAD. None when a side is unreadable."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit
    from coordinator_core.git.since_base_overlap import _head_sha, _lines
    from coordinator_core.ops.dispatch_emit import kept_work

    common = resolve_git_common_dir(root)
    base_obj = git_objects.read_object(common, base_blob)
    head = _head_sha(root)
    head_lines = _lines(common, _blob_at_commit(common, head, rel)) if head else None
    form = kept_work.checkin_form(root, rel, content)
    base_form = None if base_obj is None else kept_work.checkin_form(root, rel, base_obj[1])
    if base_form is None or head_lines is None or form is None:
        return None
    base_lines = base_form.splitlines(keepends=True)
    return _hunk_blocks(base_lines, form.splitlines(keepends=True)) - _hunk_blocks(base_lines, head_lines)


def _landed_bytes(root: Path, rel: str, settled: dict) -> Optional[bytes]:
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    blob = settled.get(rel)
    if blob:
        obj = git_objects.read_object(resolve_git_common_dir(root), blob)
        return None if obj is None else obj[1]
    try:
        return (root / rel).read_bytes()
    except OSError:
        return None


def _own_hunks_agree(root: Path, rel: str, measured_blob: object, baseline: Mapping, settled: dict) -> bool:
    """True when every own hunk measured in *rel* is still landed unchanged, so the blob difference is
    peer edits only (a peer hunk added after measure is not the run's and never refuses it). False on
    a changed or missing own hunk, an unreadable side, or a path with no baseline."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    base_blob = baseline.get(rel)
    if not base_blob or not isinstance(measured_blob, str):
        return False
    measured = git_objects.read_object(resolve_git_common_dir(root), measured_blob)
    landed = _landed_bytes(root, rel, settled)
    if measured is None or landed is None:
        return False
    try:
        as_measured = _own_hunks(root, rel, base_blob, measured[1])
        as_landed = _own_hunks(root, rel, base_blob, landed)
        if as_measured is None or as_landed is None:
            return False
        return not (as_measured - as_landed) and (bool(as_measured) or as_measured == as_landed)
    except Exception:
        return False


def _peer_dirt_only(root: Path, rel: str, baseline: Mapping) -> bool:
    """True when the worktree file holds none of the run's own hunks (all of its difference from
    HEAD predates the run), so there is nothing of the run's to measure. False without a baseline."""
    base_blob = baseline.get(rel)
    if not base_blob:
        return False
    try:
        got = _own_hunks(root, rel, base_blob, (root / rel).read_bytes())
    except Exception:
        return False
    return got is not None and not got


def _drop_lost_baseline_verified(root: Path, refusals: list, measured: Optional[Mapping]) -> list:
    """`refusals` minus each path whose only refusal is ``unreadable`` (its baseline blob is gone, so
    no own/other split exists) and whose worktree still checks in to the blob the run measured and
    verified: that path lands as measured, as a path with no baseline does. Any other refusal, or a
    worktree that drifted from the measured blob, stays refused."""
    if not refusals or measured is None or measured.get("unreadable"):
        return refusals
    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    from coordinator_core.git.commit import checkin_blob_sha

    other = {p for p, _run, header in refusals if header != "unreadable"}
    kept = []
    for p, run, header in refusals:
        if header == "unreadable" and p not in other:
            want = blobs.get(p)
            try:
                if isinstance(want, str) and checkin_blob_sha(root, p) == want:
                    continue
            except Exception:
                pass
        kept.append((p, run, header))
    return kept


def _verified_preexisting(root: Path, rel: str, measured_blob: object, baseline: Mapping) -> bool:
    """True when *rel* holds none of the run's own hunks and its worktree still checks in to the
    measured blob: pre-existing dirt the run measured and verified, which lands as measured."""
    from coordinator_core.git.commit import checkin_blob_sha

    if not isinstance(measured_blob, str) or not _peer_dirt_only(root, rel, baseline):
        return False
    try:
        return checkin_blob_sha(root, rel) == measured_blob
    except Exception:
        return False


def _measured_rev_call(run_id: Optional[str]) -> str:
    """The invoke line that re-measures the run's landing set: `paths` is empty because the op unions
    in the run's own recorded writes."""
    return "coordinator-invoke dispatch.measured_rev '" + json.dumps({"run_id": run_id or "<run_id>", "paths": []}) + "'"


def _byte_cause(old: bytes, new: bytes) -> str:
    """Why two byte strings whose lines compare equal still differ; empty when they are identical."""
    if old == new:
        return ""
    if old.replace(b"\r\n", b"\n") == new.replace(b"\r\n", b"\n"):
        return "line endings differ (CRLF vs LF)"
    if old.rstrip(b"\r\n") == new.rstrip(b"\r\n"):
        return "the trailing newline differs"
    return "bytes differ within the lines"


def _mismatch_detail(
    root: Path, rel: str, blobs: Mapping, settled: Mapping, run_id: Optional[str] = None, omitted: bool = False,
) -> str:
    """One path's refusal line: full measured vs landing blob ids, the first differing line (or, when
    every line is equal, the byte cause or the omission), and the one remedy for the cause. *omitted*:
    the commit does not carry *rel* at all."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.commit import checkin_blob_sha
    from coordinator_core.git.git_dir import resolve_git_common_dir

    measured, landing = blobs.get(rel), settled.get(rel)
    try:
        worktree = checkin_blob_sha(root, rel)
    except Exception:
        worktree = None
    landing = landing or worktree
    common = resolve_git_common_dir(root)

    def raw(blob: object) -> Optional[bytes]:
        obj = git_objects.read_object(common, blob) if isinstance(blob, str) else None
        return None if obj is None else obj[1]

    old_raw, new_raw = raw(measured), raw(landing)
    if new_raw is None and not settled.get(rel):
        try:
            from coordinator_core.ops.dispatch_emit import kept_work

            form = kept_work.checkin_form(root, rel, (root / rel).read_bytes())
            new_raw = None if form is None else form
        except OSError:
            pass
    first = ""
    if measured and measured == landing:
        first = "; the bytes are identical"
    elif old_raw is not None and new_raw is not None:
        old, new = old_raw.splitlines(), new_raw.splitlines()
        at = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
        if at >= len(old) and at >= len(new):
            cause = _byte_cause(old_raw, new_raw)
            first = f"; every line is equal ({cause})" if cause else "; the bytes are identical"
        else:
            def got(seq: list) -> str:
                return seq[at].decode("utf-8", "replace")[:80] if at < len(seq) else "<end of file>"

            first = f"; first difference at line {at + 1}: measured {got(old)!r}, landing {got(new)!r}"
    if omitted:
        first += "; the commit omits this path, which the measured record carries and HEAD does not hold"
    if measured and worktree == measured:
        remedy = "the worktree still holds the measured bytes, so hand-commit this path whole"
    else:
        remedy = f"re-run dispatch.measured_rev over the landing set with {_measured_rev_call(run_id)} and re-verify"
    return f"{rel}: measured {measured}, landing {landing}{first}; remedy: {remedy}"


def _live_peer_holders(
    root: Path, paths: Sequence[str], own_sid: Optional[str], run_id: object, script_path: object,
    plan_rel: Optional[str], run_base: Optional[str], minted_before: Optional[tuple], self_sessions: Collection,
) -> dict:
    """``{path: "live peer run <id>[, ...]" or "live peer session <sid>[, ...]"}`` for each of *paths* a
    live peer holds (``_peer_held``); empty when none. Run ids come from the one held-token pass."""
    held = _peer_held(
        root, paths, own_sid, run_id, script_path, plan_rel=plan_rel, run_base=run_base,
        minted_before=minted_before, self_sessions=self_sessions,
    )
    if not held:
        return {}
    try:
        from coordinator_core.ops import warp_run_deliverable

        scan = warp_run_deliverable._live_claim_scan(
            root, list(held), "", str(root), minted_before, run_id if isinstance(run_id, str) else None,
        ) or {}
    except Exception:  # noqa: BLE001 -- attribution is advisory; the hold itself is already known
        scan = {}
    out: dict = {}
    for path, sids in held.items():
        runs = sorted({rid for _at, rid, _sid in scan.get(path, ())})
        out[path] = (
            "live peer run " + ", ".join(runs) if runs else "live peer session " + ", ".join(sorted(sids))
        )
    return out


def _worktree_blob(root: Path, rel: str) -> Optional[str]:
    """The check-in blob id of the worktree file, None when unreadable."""
    from coordinator_core.git.commit import checkin_blob_sha

    try:
        return checkin_blob_sha(root, rel)
    except Exception:  # noqa: BLE001 -- unreadable bytes never satisfy the record
        return None


def _measured_mismatch(
    root: Path, record: dict, product: Sequence[str], deleted: Sequence[str], settled: dict, own=None,
    baseline: Optional[Mapping] = None, owned_now=None, no_change=None,
) -> list:
    """Paths where the product changes a commit would introduce (``product`` path -> blob, ``deleted``
    removals) differ from the record's ``blobs``/``removed``. A record path the commit omits counts
    unless HEAD already holds the recorded blob. Paths *own* (an ``OwnArtifacts``) covers never count.
    ``owned_now`` (paths -> the subset whose bytes are the run's own last write) drops a landing path
    the run's review/fix leg rewrote after the record; a foreign rewrite stays. ``no_change`` (paths ->
    the subset the run provably never changed, ``_run_made_no_change``) drops a landing path whose
    difference is a peer's alone."""
    from coordinator_core.git.commit import checkin_blob_sha

    blobs = record.get("blobs") if isinstance(record.get("blobs"), dict) else {}
    removed = set(_measured_strs(record, "removed"))
    head = _head_entries(root, [*blobs, *removed])
    landing, gone = set(product), set(deleted)
    differing = {p for p in landing if blobs.get(p) != (settled.get(p) or checkin_blob_sha(root, p))}
    differing |= {p for p in gone if p not in removed}
    differing |= {
        p for p, blob in blobs.items()
        if p not in landing and p not in gone and (head.get(p) or (None, None))[1] != blob
        and _worktree_blob(root, p) != blob
    }
    differing |= {p for p in removed if p not in gone and p in head}
    if own is not None:
        differing = {p for p in differing if not own.covers(p)}
    if owned_now is not None and differing & landing:
        differing -= set(owned_now(sorted(differing & landing)))
    if no_change is not None and differing & landing:
        differing -= set(no_change(sorted(differing & landing)))
    if baseline:
        differing = {
            p for p in differing
            if not (p in landing and _own_hunks_agree(root, p, blobs.get(p), baseline, settled))
        }
    return sorted(differing)


def _run_made_no_change(root: Path, run_id: Optional[str], baseline: Optional[Mapping], owned_now):
    """A ``paths -> never-changed subset`` callable: those baselined paths where no blob the run's
    record dir holds (kept chunks, post-checkpoint, measured-rev) carries an own hunk over the write
    baseline and ``owned_now`` finds no run write, so any difference from HEAD is a peer's. A missing or
    unreadable record, blob or baseline keeps the path out."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import drift_attribution

    def never_changed(paths):
        if not baseline or not run_id or not safe_id(run_id):
            return []
        wanted = [p for p in paths if baseline.get(p)]
        if not wanted:
            return []
        try:
            common = resolve_git_common_dir(root)
            recorded = drift_attribution.run_recorded_blobs(record_dir(root, run_id), wanted)
            written = set(owned_now(wanted)) if owned_now is not None else set()
            out = []
            for p in wanted:
                if p in written:
                    continue
                objs = [git_objects.read_object(common, b) for b in recorded.get(p, [])]
                if not objs or any(o is None for o in objs):
                    continue
                hunks = [_own_hunks(root, p, baseline[p], o[1]) for o in objs]
                if all(h is not None and not h for h in hunks):
                    out.append(p)
            return out
        except Exception:
            return []

    return never_changed


def _own_bytes_now(root: Path, run_dir, session_id, run_id, inline_review):
    """A ``paths -> run-owned subset`` callable: those paths whose bytes are the run session's or a
    review agent's last recorded write (``drift_attribution.classify``), not bounded by the run
    record's start. Hash-only; the ledgers are read when called."""
    from coordinator_core.ops.dispatch_emit import drift_attribution

    def owned(paths):
        hashes = {p: [sha] for p, (sha, _at) in _review_write_events(root, session_id, run_id, inline_review).items()}
        return drift_attribution.classify(
            root, run_dir if run_dir is not None else root, paths, [], session_id=session_id,
            own_hashes=hashes, run_started_at=0.0,
        )[drift_attribution.RUN_OWNED]

    return owned


def _reap_measured_export(root: Path, run_id: Optional[str]) -> None:
    if run_id and safe_id(run_id):
        import shutil

        shutil.rmtree(root / "scratch" / "measured-rev" / run_id, ignore_errors=True)


def _land_inner(root: Path, repo_root: Path, params: dict, stranded: dict, token_state: dict) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.git.git_state import head_branch
    from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest, delivery_note_locate
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
        plan_deliverable_id,
        plan_trailer_deliverable_ids,
        valid_deliverable_id,
    )
    from coordinator_core.ops.dispatch_emit.landing_remedy import (
        blob_drift_remedy,
        REVIEW_CARRIED_REASON,
        param_withholds,
        release_held_call,
        review_carried_withholds,
        review_only_anchor,
    )
    from coordinator_core.ops.dispatch_emit.shipped_claims import SELF_APPLIED_WITHHOLDS

    task_output = params.get("task_output_path") if isinstance(params, dict) else None
    reclose = _reclose_params(root, params)
    if reclose is not None:
        return _reclose(root, reclose)
    if isinstance(params, dict) and isinstance(params.get("task_output_path"), str):
        try:
            params = {
                **params_from_task_output(params["task_output_path"], root, params.get("run_id")),
                **{k: v for k, v in params.items() if k != "task_output_path"},
            }
        except ValueError as exc:
            from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel

            recorded = terminal_commit_params_channel.recorded_land_params(root, params.get("run_id"))
            if recorded is None:
                note = "" if isinstance(params.get("run_id"), str) else (
                    "; pass run_id to recover from the run's recorded land params"
                )
                return _error(str(exc) + note)
            params = {**recorded, **{k: v for k, v in params.items() if k != "task_output_path"}}
    if isinstance(params, dict) and not params.get("script_path"):
        from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
        from coordinator_core.ops.dispatch_emit.halted_record_commit import commit_halted_record
        from coordinator_core.ops.dispatch_emit.halted_run_route import land_only_commit_route

        payload = land_only_commit_route(root, params.get("run_id"))
        if payload is not None:
            return commit_halted_record(root, root, {**params, HALTED_RECORD_PARAM: payload})
        halted = halted_refusal(root, params.get("run_id"), "run outcome")
        if halted is not None:
            return _error(halted, refused="halted-run")
    missing = _missing_run_outcome(params, task_output if isinstance(task_output, str) else None)
    if missing is not None:
        return missing
    refusal = validate_params(contract.OP_NAME, params, _PARAM_FIELDS)
    if refusal is not None:
        return _error(refusal["error"])

    script_path = params["script_path"]
    incomplete = set(params["incomplete_chunks"]) - set(params.get("landed_chunks") or [])
    inline_review = params.get("inline_review")
    session_id = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    if params.get("run_token") is not None:
        answered = _redeem_or_answer(root, params.get("run_id"), params["run_token"], script_path, token_state)
        if answered is not None:
            if answered.get(ALREADY_LANDED_KEY):
                if not incomplete:
                    from coordinator_core.ops.dispatch_emit.run_close import settle_run_stage

                    settle_run_stage(root, params.get("run_id"))
                _requeue_close(root, params)
            return answered
        token_state["claim"] = (root, params.get("run_id"), params["run_token"], script_path)

    script = contained_path(root / script_path, [root])
    if script is None:
        return _error(f"params.script_path {script_path!r} does not resolve under the caller's own worktree")
    script_source = "script"
    try:
        script_text = script.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        from coordinator_core.ops.dispatch_emit.emission_receipt import receipt_landing_text

        script_text = receipt_landing_text(script)
        if not script_text:
            return _error(
                f"script-missing: {script_path!r} is gone and its receipt carries no landing markers "
                f"({exc}); run: python -m coordinator_core.ops.dispatch_emit.stranded_part_rebuild "
                f"--script {script_path} --task-output <task output path> --inventory <tranche record>",
                refused="script-missing",
            )
        script_source = "receipt"
    except OSError as exc:
        return _error(f"cannot read params.script_path {script_path!r}: {exc}")

    land_deadline.check("land_run.script_read")
    from coordinator_core.ops.dispatch_emit.commit_request import MARKER_PREFIX

    manifest = manifest_abs = ask_run_dir = request = None
    try:
        manifest_rel = parse_manifest_marker(script_text)
        if manifest_rel is not None:
            if any(ln.startswith(MARKER_PREFIX) for ln in script_text.splitlines()):
                return _error(
                    "script carries both an inline commit-request marker and an ask-run-manifest marker; "
                    "emit one",
                    refused="both-markers",
                )
            run_root = root / RUN_DIR_ROOT
            manifest_abs = contained_path(root / manifest_rel, [root / RECORD_DIR_ROOT, run_root])
            if manifest_abs is None:
                return _error(
                    f"ask-run-manifest path {manifest_rel!r} does not resolve under {RECORD_DIR_ROOT} "
                    f"or {RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _error(f"cannot read ask-run manifest {manifest_rel!r}: {exc!r}")
            ask_run_dir = manifest_abs.parent
            request_abs = contained_path(root / manifest.marker_path, [run_root])
            if request_abs is None:
                return _error(
                    f"manifest marker_path {manifest.marker_path!r} does not resolve under {RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                request = parse_marker(request_abs.read_text(encoding="utf-8"))
            except OSError as exc:
                return _error(f"cannot read manifest marker_path {manifest.marker_path!r}: {exc}")
        else:
            request = parse_marker(script_text)
    except MalformedCommitRequestError as exc:
        return _error(f"malformed commit request: {exc}", refused="malformed-request")
    if request is None:
        request = _anchor_request(root, params, inline_review)
    if request is None:
        return {"committed": False, "nothing_to_commit": True}
    anchor_only = not request.chunks
    withheld_by_gate: dict = {}
    if manifest is not None:
        withheld_by_gate.update({g.id: g.reason for g in manifest.gated})
        withheld_by_gate.update({o.id: f"owed: {o.detail}" for o in manifest.owed})
    recovery_ids = list(dict.fromkeys(params.get(RECOVERY_WITHHELD_PARAM) or ()))
    recovery_chunks: dict = {}
    recovery_reasons: dict = {}
    if recovery_ids:
        recovery_chunks = _recovery_chunks(root, ask_run_dir)
        from coordinator_core.ops.dispatch_emit import credit_gate as _cg

        mended = set(_cg.recovery_blocks_superseded(
            recovery_ids, recovery_chunks, {c.id: set(c.paths) for c in request.chunks},
            _remnant_record(root, ask_run_dir),
        ))
        recovery_ids = [i for i in recovery_ids if i not in mended]
    if recovery_ids:
        recovery_reasons, unbacked = _recovery_backing(recovery_chunks, recovery_ids)
        if unbacked:
            return _error(
                f"params.{RECOVERY_WITHHELD_PARAM} names chunks the run's recovery.json does not record as "
                f"exhausted, blocked or held: {', '.join(unbacked)}. Re-run the ledger recovery or "
                "drop them from the param",
                refused="recovery-withheld-unbacked",
                unbacked=unbacked,
            )
        withheld_by_gate.update(recovery_reasons)
    recovery_dependents: dict = {}

    rid = params.get("run_id")
    sibling_run_id = (
        rid if isinstance(rid, str) and rid
        else ask_run_dir.name if ask_run_dir is not None
        else PurePosixPath(script_path.replace("\\", "/")).stem
    )
    from coordinator_core.ops.workflow_fire.finalizer import review_owner as _review_owner
    reviewed_bytes, reviewed_at, prep_from_disk = _reconcile_prep(root, _review_owner(session_id), sibling_run_id, request, inline_review)
    product_only = ask_run_dir is not None
    record_dir_rel: Optional[str] = None
    if ask_run_dir is not None:
        try:
            rel = ask_run_dir.relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = None
        if rel is not None and (rel == RECORD_DIR_ROOT or rel.startswith(RECORD_DIR_ROOT + "/")):
            record_dir_rel = rel
    rows_untouched = anchor_only or request.review_only
    plan_text = _read_rel(root, request.plan_path)
    applies = bool(request.plan_path) and not rows_untouched
    kind, plan_governs = _close_kind(plan_text) if applies else ("plan-run", True)

    if inline_review is None:
        return _error(
            "params.inline_review is absent -- the run carries no review-stage output. Run the "
            "digest's commit line verbatim (or pass task_output_path = the Workflow run's output file, "
            "named in its completion notification); else re-emit "
            "with a review stage and re-fire" + route_suffix(root, params.get("run_id")),
            refused="unreviewed",
        )
    if not inline_review.get("integration_stem") or inline_review.get("slices") is None:
        return _error(
            "params.inline_review is missing integration_stem and/or slices -- no review-stage output "
            f"to land against (got: {inline_review!r}). Use the digest's commit line "
            "verbatim",
            refused="unreviewed",
        )
    floor_code = review_floor.floor_code_paths(request, inline_review, prep_from_disk)
    floor_notes = []
    floor_reason = review_floor.floor_reason(floor_code, review_floor.verdict_count(inline_review, root))
    if floor_reason is not None:
        floor_notes.append(floor_reason)
    prep_rev = inline_review.get("prep")
    slice_files = prep_rev.get("slice_files") if isinstance(prep_rev, dict) else None
    if floor_code and isinstance(slice_files, list) and slice_files:
        declared_files = {p for c in request.chunks for p in c.paths}
        gap = review_floor.slice_gap_paths([p for p in floor_code if p in declared_files], slice_files)
        gap_reason = review_floor.slice_gap_reason(gap)
        if gap_reason is not None:
            floor_notes.append(gap_reason)

    from coordinator_core.ops.dispatch_emit import requirements_ledger

    ledger = None
    open_rows: list = []
    fence: dict = {}
    ledger_advisory: list = []
    if ask_run_dir is not None:
        unmet, ledger, open_rows = _ledger_gate(
            root, ask_run_dir, params.get("ledger_verdict"), manifest_abs,
            [g.id for g in manifest.gated], requirements_ledger.owed_rows(manifest.owed),
            plan_text, inline_review, params.get(TERMINAL_RESUME_LEDGER_PARAM), request.plan_path, fence,
            recovery_reasons, code_paths=floor_code, advisories=ledger_advisory,
        )
        if unmet is not None:
            if not (unmet.get(ALREADY_LANDED_KEY) and unmet.get("committed")):
                return unmet
            token_state["since_halt_close"] = unmet
    ledger_met = None
    if ledger is not None and not open_rows:
        met_ids = _ledger_met_ids(params.get("ledger_verdict"))
        ledger_met = ("ledger-met: " + ", ".join(met_ids))[:_UNDONE_CAP] if met_ids else None

    observed_branch = head_branch(root)
    if observed_branch == "HEAD":
        return _error(
            "HEAD is detached; check out a branch and re-fire, or commit by hand",
            refused="detached-head", observed_branch="HEAD",
        )
    if observed_branch is None:
        return _error("HEAD is unreadable; repair the worktree and re-fire, or commit by hand", refused="branch-unreadable")
    if request.expected_branch is not None and request.expected_branch != observed_branch:
        return _error(
            f"run expected branch {request.expected_branch!r} but HEAD is on {observed_branch!r}; check out "
            f"{request.expected_branch} and re-fire, or commit by hand",
            refused="branch-mismatch", expected_branch=request.expected_branch, observed_branch=observed_branch,
        )

    marker_id = request.deliverable_id
    plan_id = plan_deliverable_id(plan_text) if plan_text is not None else None
    if marker_id is not None:
        bad = None
        if valid_deliverable_id(marker_id) is None:
            bad = "is not a valid dlv- id"
        elif request.plan_path and plan_text is None:
            bad = f"cannot be verified: plan_path {request.plan_path!r} is unreadable"
        elif request.plan_path and marker_id != plan_id:
            bad = "differs from the plan's deliverable_id"
        if bad is not None:
            return _error(
                f"marker deliverable_id {marker_id!r} {bad}",
                deliverable_id_marker=marker_id, deliverable_id_plan=plan_id,
            )
    elif request.plan_path and plan_id is not None:
        return _error(
            "marker carries no deliverable_id but the plan's is set",
            deliverable_id_marker=None, deliverable_id_plan=plan_id,
        )

    # ---- bookkeeping record and evidence rows, written before the commit so they ride it
    objective_gate: Optional[dict] = None
    if not anchor_only:
        _derive_judge_status(inline_review)
        objective_gate = _objective_gate(root, sibling_run_id, inline_review)
        if objective_gate is not None:
            _record_objective_outcome(root, sibling_run_id, inline_review, objective_gate)
    record: Optional[dict] = None
    record_abs: Optional[Path] = None
    bookkeeping_path: Optional[str] = None
    own_evidence_write: Optional[tuple] = None
    stage_returns = _stage_returns(inline_review)
    if (
        inline_review.get("plan_id") and isinstance(inline_review.get("wave_sidecar_paths"), list)
        and (inline_review.get("prep_sidecar") or stage_returns)
    ):
        if not session_id:
            return _error(
                "params.inline_review names a bookkeeping stem (wave_sidecar_paths/plan_id present) but "
                "params.session_id is missing or not canonical-UUID-shaped -- bookkeep_wave needs it to "
                "place the record"
            )
        from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave, landing_run_diff

        # The record dir's own withholds only: the superseded predecessors' dirs are read once `artifacts` exists.
        try:
            bk_withheld = {
                **_withhold_reasons(root, None, record_dir_rel),
                **param_withholds(params), **review_carried_withholds(request),
            }
        except ValueError:
            bk_withheld = dict(param_withholds(params))
        bk_withheld = {p: r for p, r in bk_withheld.items() if r not in SELF_APPLIED_WITHHOLDS}
        try:
            record = bookkeep_wave(
                [root / p for p in inline_review["wave_sidecar_paths"] if isinstance(p, str)],
                repo_root=root, session_id=session_id, plan_id=inline_review["plan_id"],
                prep_sidecar=inline_review.get("prep_sidecar"),
                record_stem=inline_review["integration_stem"], stage_returns=stage_returns,
                run_diff=landing_run_diff(stage_returns, bk_withheld), withheld=bk_withheld,
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silently dropped
            return _error(f"review_mint.bookkeep_wave failed: {exc}")
        record_abs = Path(record["sidecar_path"])
        try:
            bookkeeping_path = record_abs.relative_to(root).as_posix()
        except ValueError:
            bookkeeping_path = None
        if request.plan_path and record.get("review_firings"):
            own_evidence_write = _append_evidence(root, request.plan_path, record["review_firings"])

    cited_sizing = None if anchor_only else _cited_sizing(root, request.plan_path)

    # ---- the landed marker: sibling_hold reads usage.json at HEAD
    art_run_id = ask_run_dir.name if ask_run_dir is not None else rid
    if ask_run_dir is not None and not anchor_only:
        # Trap: written before the refusal checks below; a refused landing leaves usage.json dirty.
        _stamp_usage(root, ask_run_dir.name)

    from coordinator_core.ops.dispatch_emit import run_artifacts, run_token

    artifacts = None
    if not anchor_only and isinstance(art_run_id, str) and art_run_id:
        artifacts = _within_handoffs_home(root, run_artifacts.candidate_paths(
            root, art_run_id, sizing_rel=cited_sizing, plan_rel=request.plan_path,
            doc_paths=params.get(run_artifacts.DOC_PATHS_PARAM) or (),
        ))

    # ---- grading: which chunks are credited
    cache: dict = {}
    held_now = _held_ids(params)
    known_ids = {c.id for c in request.chunks}
    sizing_for_credit = cited_sizing or (next(iter(artifacts.by_class.get("sizing", ())), None) if artifacts else None)
    regraded = _grade(
        root, request, incomplete, inline_review, ledger, params.get("ledger_verdict"), held_now, artifacts,
        record_dir_rel, ask_run_dir, sizing_for_credit, cache, manifest,
    )
    incomplete -= set(regraded)
    held_ids = held_now
    incomplete |= held_ids & known_ids
    incomplete |= set(recovery_reasons)
    if recovery_reasons:
        recovery_dependents = _recovery_dependents(root, request, incomplete, recovery_reasons, recovery_chunks)
        incomplete |= set(recovery_dependents)
        withheld_by_gate.update(recovery_dependents)
    recovery_out = set(recovery_reasons) | set(recovery_dependents)
    recovery_paths = {
        _norm_rel(p): recovery_reasons[cid]
        for cid in recovery_reasons for p in (recovery_chunks.get(cid) or {}).get("paths") or () if isinstance(p, str)
    }
    recovery_reply: dict = {}
    if recovery_out:
        recovery_open = set(recovery_reasons.values())
        recovery_reply = {
            "recovery": {
                "withheld": {**recovery_reasons, **recovery_dependents},
                "open_rows": [str(r["id"]) for r in open_rows if r.get("reason") in recovery_open],
            },
            "withheld": {**recovery_reasons, **recovery_dependents, **recovery_paths},
        }
    stranded.update({c.id: list(c.paths) for c in request.chunks if c.id in incomplete})
    blockers: dict = {}
    partial = [c for c in _partial_chunks(root, request, incomplete, blockers) if c.id not in held_ids | recovery_out]
    done = [c for c in request.chunks if c.id not in incomplete]
    credited_absent = _credited_absent_chunks(
        plan_text, request,
        _read_credits(root, artifacts, record_dir_rel, ask_run_dir, sizing_for_credit), held_ids, incomplete,
    )
    credited_chunks = done + partial + credited_absent

    held_paths = _held_write_paths(root, request, held_ids) if held_ids else {}
    if held_paths:
        overlap: dict = {}
        for chunk in credited_chunks:
            for p in list(chunk.paths) + (_own_prefix_files(root, chunk, cache) or []):
                if p in held_paths:
                    entry = overlap.setdefault(p, {"held": held_paths[p], "landed": []})
                    if chunk.id not in entry["landed"]:
                        entry["landed"].append(chunk.id)
        if overlap:
            shown = "; ".join(
                f"{p} (held {', '.join(e['held'])}; landed {', '.join(e['landed'])})" for p, e in sorted(overlap.items())
            )
            return _error(
                f"path(s) declared by both a held chunk and a landed chunk cannot be committed consistently: "
                f"{shown}. Re-fire to land the landed chunks and release the held chunk(s): "
                f"{release_held_call(contract.OP_NAME, params.get('task_output_path'), {h for e in overlap.values() for h in e['held']})}"
                "; or list the landed chunk in params.held",
                refused="held-landed-overlap", overlap=overlap,
            )

    # ---- membership (i): credited rows' declared writes and their own-prefix files
    owner: dict = {}  # path -> credited chunk ids declaring it
    for chunk in credited_chunks:
        files = _own_prefix_files(root, chunk, cache)
        if files is None and chunk in done:
            return _error(
                f"chunk {chunk.id!r} declares prefixes {list(chunk.prefixes)} but its report {chunk.report!r} "
                "carries no `created-under-prefix:` list -- list it in params.incomplete_chunks to commit the rest"
            )
        for p in [*chunk.paths, *(files or [])]:
            owner.setdefault(p, [])
            if chunk.id not in owner[p]:
                owner[p].append(chunk.id)
    declared = list(owner)
    inherited: dict = {}
    if ask_run_dir is not None:
        inherited = {
            p: origin for p, origin in inherited_scope(root, params.get(TERMINAL_RESUME_LEDGER_PARAM)).items()
            if (root / p).is_file()
        }
        declared += [p for p in inherited if p not in owner]

    # ---- membership (ii) and (iii): artifacts and superseded predecessors
    recorded: list = []
    if ask_run_dir is not None and isinstance(params.get(TERMINAL_RESUME_LEDGER_PARAM), dict):
        recorded = [p for p in _resume_recorded_paths(root, ask_run_dir, inline_review) if p not in owner and p not in inherited]
    if ask_run_dir is not None:
        recorded += [
            p for p in fence.get("breach") or () if p not in owner and p not in inherited and p not in recorded
        ]
    recorded += [
        p for p in reviewed_bytes
        if isinstance(p, str) and p not in owner and p not in inherited and p not in recorded
    ]
    measured_run = ask_run_dir.name if ask_run_dir is not None else None
    measured = _measured_record(root, measured_run)
    measured_carries = {*_measured_strs(measured, "paths"), *_measured_strs(measured, "removed")}
    recorded += [
        p for p in _measured_strs(measured, "paths") if p not in owner and p not in inherited and p not in recorded
    ]
    unmeasured: list = []
    recorded_pc = _recorded_post_checkpoint(
        root, [art_run_id, _resumed_review_source(root, inline_review, rid, session_id)[0]], ask_run_dir,
    )
    literals = [*declared, *recorded, *(artifacts.paths() if artifacts is not None else ())]
    if bookkeeping_path:
        literals.append(bookkeeping_path)
    literals.extend(recorded_pc)
    land_deadline.mark("land_run.dirty_scan")
    changed, ignored, _head = _dirty_among(root, literals)
    land_deadline.mark("land_run.superseded_scan")
    superseded: tuple = ()
    if artifacts is not None and sizing_for_credit:
        superseded, extra_changed = _superseded(root, art_run_id, sizing_for_credit, artifacts, run_token)
        if superseded:
            artifacts = artifacts.with_superseded(superseded, RECORD_DIR_ROOT)
            changed |= extra_changed
    art_landable = None
    if artifacts is not None:
        landable = run_artifacts.landable(
            root, artifacts, changed, [p for p in ignored if p in set(artifacts.paths())], session_id=session_id,
        )
        classes = _PRODUCT_ARTIFACT_CLASSES | (_ASK_ARTIFACT_CLASSES if product_only else frozenset())
        art_landable = run_artifacts.Landable(
            present=tuple(p for p in landable.present if artifacts.class_of(p) in classes),
            deleted=tuple(p for p in landable.deleted if artifacts.class_of(p) in classes),
            withheld=landable.withheld, contest_unknown=landable.contest_unknown,
        )
        landable_artifacts = run_artifacts.RunArtifacts(
            run_id=artifacts.run_id,
            by_class={c: ps for c, ps in artifacts.by_class.items() if c in classes},
            record_dirs=artifacts.record_dirs, root=artifacts.root,
        )
    else:
        landable_artifacts = None

    # ---- the landing set, with every named withhold
    paths: list = []
    deleted: list = []
    reverted = _reverted_paths(params, inline_review)
    withhold_reason: dict = {}  # declared path -> reason it stays out
    em_withheld = _withhold_reasons(root, artifacts, record_dir_rel)
    try:
        em_withheld.update(param_withholds(params))
        carried_paths = review_carried_withholds(request)
        em_withheld.update(carried_paths)
    except ValueError as exc:
        return _error(f"withhold_paths refused: {exc}")
    from coordinator_core.ops.ceremony.commit_gates import is_never_commit
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import sibling_write

    footprint_breach = set(fence.get("breach") or ())
    declared_changed = [p for p in declared if p in changed and p not in ignored]
    land_deadline.mark("land_run.foreign_withheld_scan")
    foreign_holders: dict = {}
    foreign_held = foreign_halted_withheld(root, art_run_id, request.plan_path, declared_changed, foreign_holders)
    land_deadline.mark("land_run.halted_release_scan")
    own_seniority = _own_run_seniority(root, rid, script_path)
    auto_landed_holders = _halted_only_release(
        root, art_run_id or sibling_run_id, request.plan_path, foreign_held, declared, own_seniority,
    )
    if auto_landed_holders:
        foreign_held = {}
    measured_released = _measured_released_holds(root, measured, foreign_held, foreign_holders)
    for p in measured_released:
        foreign_held.pop(p)
    # A halted holder keeps only its own hunks: the landing's hunks that overlap none of them land.
    halted_clear: dict = {}  # path -> (clear hunks, holder id, some hunk stays withheld)
    if foreign_held and not _merge_in_progress(root):
        from coordinator_core.ops.dispatch_emit import halted_hold_release

        for p, (hunks, partial) in halted_hold_release.hunks_clear_of_holders(
            root, {p: foreign_holders[p] for p in foreign_held if p in foreign_holders},
        ).items():
            halted_clear[p] = (hunks, foreign_held.pop(p), partial)
    own_halt_reason = _withheld_of(root, params.get(TERMINAL_RESUME_LEDGER_PARAM) or {}) if inherited else {}

    def _own_inherited(p: str) -> bool:
        return p in inherited and p not in foreign_held

    for p in declared_changed:
        if is_never_commit(p):
            withhold_reason[p] = "never-commit"
        elif sibling_write(p, root) is not None:
            withhold_reason[p] = "sibling-repo"
        elif p in em_withheld and em_withheld[p] not in SELF_APPLIED_WITHHOLDS and not (
            _own_inherited(p) and inherited[p] == ORIGIN_PREDECESSOR_WITHHELD
            and em_withheld[p] == own_halt_reason.get(p)
        ):
            withhold_reason[p] = REVIEW_CARRIED_REASON if p in carried_paths else "withheld"
        elif p in foreign_held:
            withhold_reason[p] = f"withheld by {foreign_held[p]}"
        elif p in footprint_breach and not _own_inherited(p) and p not in measured_carries:
            withhold_reason[p] = OUTSIDE_ROW_FOOTPRINT
            if measured is not None:
                unmeasured.append(p)
    recorded_reason: dict = {}  # recorded (chunk-less) path -> reason it stays out
    recorded_changed = [
        p for p in recorded if p in changed and p not in ignored and (root / p).exists()
    ]
    for p in recorded_changed:
        if is_never_commit(p):
            recorded_reason[p] = "never-commit"
        elif sibling_write(p, root) is not None:
            recorded_reason[p] = "sibling-repo"
        elif p in em_withheld and em_withheld[p] not in SELF_APPLIED_WITHHOLDS:
            recorded_reason[p] = REVIEW_CARRIED_REASON if p in carried_paths else "withheld"
        elif p in footprint_breach and p not in measured_carries:
            recorded_reason[p] = OUTSIDE_ROW_FOOTPRINT
            if measured is not None:
                unmeasured.append(p)
        elif p in reverted:
            recorded_reason[p] = "reverted"
        elif p in reviewed_bytes and _changed_after_review(root, p, reviewed_bytes[p]):
            recorded_reason[p] = CHANGED_AFTER_REVIEW
    candidates = [p for p in declared_changed if p not in withhold_reason]
    candidates += [p for p in recorded_changed if p not in recorded_reason and p not in candidates]
    art_present = [p for p in (art_landable.present if art_landable else ()) if p not in candidates]
    art_deleted = list(art_landable.deleted) if art_landable else []
    art_dropped: dict = {}
    for p in [*art_present, *art_deleted]:
        if is_never_commit(p):
            art_dropped[p] = "never-commit"
        elif p in em_withheld and em_withheld[p] not in SELF_APPLIED_WITHHOLDS:
            art_dropped[p] = f"withhold_paths: {em_withheld[p]}"
    process_withheld: dict = {}
    if artifacts is not None:
        from coordinator_core.ops.dispatch_emit import process_artifact_policy

        _kept, process_withheld = process_artifact_policy.withhold_process_artifacts(root, artifacts)
        if process_withheld and bookkeeping_path:
            process_withheld[bookkeeping_path] = process_artifact_policy.WITHHELD_REASON
        art_dropped.update(process_withheld)
    art_present = [p for p in art_present if p not in art_dropped]
    art_deleted = [p for p in art_deleted if p not in art_dropped]

    def _exclude_withheld() -> None:
        if process_withheld:
            process_artifact_policy.exclude_locally(root, sorted(process_withheld), artifacts.record_dirs)

    carried_probe = [*candidates, *art_present, *art_deleted]
    prep = inline_review.get("prep")
    run_base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    decl_only: set = set()
    peer_held = _peer_held(
        root, carried_probe, session_id, rid, script_path,
        plan_rel=request.plan_path, run_base=run_base if isinstance(run_base, str) else None,
        minted_before=own_seniority, self_sessions=_firing_sessions(root, rid, session_id),
        declaration_only=decl_only,
    )
    from coordinator_core.ops.dispatch_emit import co_declared_settle as _cds

    # Hold scan repeats inside settle(); eligibility here is advisory, the post-_integrity loop re-withholds.
    deferred = _cds.settle_eligible(
        root, sibling_run_id, [p for p in candidates if p in decl_only and (root / p).is_file()],
        own_declared=declared, own_plan=request.plan_path, minted_before=own_seniority,
    )
    for p in candidates:
        if p in peer_held and p not in deferred:
            withhold_reason[p] = "peer-held"
            if p in recorded_changed:
                recorded_reason[p] = "peer-held"
    candidates = [p for p in candidates if p not in withhold_reason]
    art_withheld = {p: "peer-held" for p in [*art_present, *art_deleted] if p in peer_held}
    art_present = [p for p in art_present if p not in art_withheld]
    art_deleted = [p for p in art_deleted if p not in art_withheld]

    declared_writes = {p for c in credited_chunks for p in c.paths}
    absent = [p for p in candidates if not (root / p).exists()]
    dropped_absent: list = []
    reverted_absent: list = []
    if absent:
        from coordinator_core.git.commit import partition_declared_deletions

        split = partition_declared_deletions(root, absent)
        if split is None:
            return _error(f"cannot resolve HEAD tree spine to classify absent path(s) {absent}")
        tracked_at_head, absent_from_head = split
        undeclared = [p for p in tracked_at_head if p not in declared_writes]
        if undeclared:
            return _error(
                "path(s) gone from the worktree but still tracked at HEAD are not in any DONE chunk's "
                f"declared writes, an undeclared deletion: {undeclared}"
            )
        reverted_absent = [p for p in tracked_at_head if p in reverted]
        deleted = sorted(p for p in tracked_at_head if p not in reverted)
        dropped_absent = list(absent_from_head)
        gone = set(deleted) | set(reverted_absent) | set(dropped_absent)
        candidates = [p for p in candidates if p not in gone]
    paths = list(dict.fromkeys([*candidates, *art_present]))
    deleted = sorted(set(deleted) | set(art_deleted))
    paths = [p for p in paths if p not in deleted]
    run_baseline = manifest.write_baseline if manifest is not None else {}
    if unmeasured:
        own_covers = _run_owned(root, art_run_id).covers
        unmeasured = [p for p in unmeasured if not own_covers(p)]
        if unmeasured:
            own_now = set(_own_bytes_now(root, ask_run_dir, session_id, sibling_run_id, inline_review)(unmeasured))
            unmeasured = [p for p in unmeasured if p not in own_now]
        unmeasured = [p for p in unmeasured if not _peer_dirt_only(root, p, run_baseline)]
    if unmeasured:
        unchanged = set(_run_made_no_change(
            root, measured_run, run_baseline, _own_bytes_now(root, ask_run_dir, session_id, sibling_run_id, inline_review),
        )(unmeasured))
        unmeasured = [p for p in unmeasured if p not in unchanged]
    if unmeasured:
        held_now = _peer_held(
            root, unmeasured, session_id, rid, script_path,
            plan_rel=request.plan_path, run_base=run_base if isinstance(run_base, str) else None,
            minted_before=own_seniority, self_sessions=_firing_sessions(root, rid, session_id),
        )
        unmeasured = [p for p in dict.fromkeys(unmeasured) if p not in held_now]
    if unmeasured:
        _reap_measured_export(root, measured_run)
        return _error(
            f"landing refused: {', '.join(unmeasured)} changed outside the declared writes and the run's "
            "measured-rev record does not carry it, so the commit would differ from the tree that was tested. "
            f"Re-measure with {_measured_rev_call(measured_run)} and re-verify, or revert the path",
            refused=LANDING_TREE_DIFFERS, paths=unmeasured,
        )
    if (
        bookkeeping_path and not product_only and bookkeeping_path not in paths
        and bookkeeping_path not in process_withheld
        and not machine_local_paths([bookkeeping_path]) and (root / bookkeeping_path).is_file()
    ):
        paths.append(bookkeeping_path)
    named_out = {
        *withhold_reason, *recorded_reason, *art_withheld, *art_dropped,
        *(p for p, why in em_withheld.items() if why not in SELF_APPLIED_WITHHOLDS),
    }
    unclaimed = sorted(
        p for p in recorded_pc
        if p in changed and p not in ignored and p not in paths and p not in deleted and p not in named_out
    )
    if unclaimed:
        shown = "; ".join(f"{p} (kept.json of {recorded_pc[p]})" for p in unclaimed)
        return _error(
            f"the run's own post-checkpoint hunks are dirty and not in this landing: {shown}. "
            "Re-stage so the kept-review chunk carries them, or withhold them with a reason: "
            "workflow.revise withhold_paths",
            refused="own-post-checkpoint-hunks-unclaimed", paths=unclaimed,
        )
    if token_state.get("since_halt_close") is not None and not candidates and not (set(deleted) - set(art_deleted)):
        _exclude_withheld()
        return {
            "committed": False, "nothing_to_commit": True,
            "run_artifacts": {"landed": [], "withheld": {**art_withheld, **art_dropped}},
            "stranded": {c: sorted(ps) for c, ps in stranded.items()},
            **_peer_held_left(peer_held),
            **recovery_reply,
        }

    # ---- credit gate and Row-Unlanded
    unlanded: list = []  # (row id, path, reason)
    for chunk in credited_chunks:
        for p in [*chunk.paths, *(_own_prefix_files(root, chunk, cache) or [])]:
            reason = withhold_reason.get(p)
            if reason is not None and (chunk.id, p, reason) not in unlanded:
                unlanded.append((chunk.id, p, reason))
    landing_now = set(paths) | set(deleted)
    contributing = [
        c for c in credited_chunks
        if any(p in landing_now for p in [*c.paths, *(_own_prefix_files(root, c, cache) or [])])
    ]
    unlanded_rows = {row for row, _p, _r in unlanded}
    absent_dropped = set(dropped_absent)
    landed_ids = set(params.get("landed_chunks") or []) | {
        c.id for c in done
        if c not in contributing and c.id not in unlanded_rows and not (c.paths and set(c.paths) <= absent_dropped)
    }
    from coordinator_core.ops.dispatch_emit import credit_gate

    if own_evidence_write:
        evidence_rows = _own_evidence_rows(done, own_evidence_write[0])
        landed_ids |= evidence_rows
        for cid in evidence_rows:
            stranded.pop(cid, None)

    gate_stranded = {c: list(ps) for c, ps in stranded.items()}
    from coordinator_core.ops.dispatch_emit import shared_path_landing as _spl

    if not set(paths) - set(art_present):  # a commit of only the run record, its product held by another live session
        for row, p, _holders in _spl.sibling_held_product(
            unlanded, peer_held, root=root, own_session=session_id,
            own_run_id=str(sibling_run_id or rid), own_plan=request.plan_path,
        ):
            if row not in landed_ids:
                gate_stranded.setdefault(row, []).append(p)
    if credit_gate.no_credited_landing(set(gate_stranded), {c.id for c in contributing}, landed_ids):
        shown = "; ".join(f"{cid}: {', '.join(ps)}" for cid, ps in sorted(gate_stranded.items()))
        why = "".join(f" {cid} blocker: {text}." for cid, text in sorted(blockers.items()) if cid in stranded)
        held_now = _live_peer_holders(
            root, sorted({p for ps in gate_stranded.values() for p in ps if p in peer_held}), session_id, rid,
            script_path, request.plan_path, run_base if isinstance(run_base, str) else None, own_seniority,
            _firing_sessions(root, rid, session_id),
        )
        if held_now:
            why += " Held: " + "; ".join(f"{p} by {who}" for p, who in sorted(held_now.items())) + "."
        return _error(
            f"no product chunk lands in this commit; stranded work stays uncommitted: {shown}.{why} "
            f"Credit one: workflow.revise run_id={sibling_run_id} credit_chunks=[{{chunk, evidence}}], "
            "then re-fire; or finish or land the stranded chunks, then re-fire",
            refused="no-credited-chunk-landed", **recovery_reply,
        )
    for row, p, _reason in unlanded:
        stranded.setdefault(row, [])
        if p not in stranded[row]:
            stranded[row].append(p)

    from coordinator_core.ops.dispatch_emit import shared_path_landing

    withheld_product: list = []
    if ledger is not None:
        held_product = shared_path_landing.sibling_held_product(
            unlanded, peer_held, root=root, own_session=session_id,
            own_run_id=str(sibling_run_id or rid), own_plan=request.plan_path,
        )
        if held_product:
            return _error(
                shared_path_landing.refusal_message(held_product, str(sibling_run_id or rid), root),
                refused=shared_path_landing.SHARED_PATH_SIBLING_HELD,
                stranded={c: sorted(ps) for c, ps in stranded.items()},
                **_peer_held_left(peer_held),
            )
        withheld_product = shared_path_landing.withheld_product_paths(unlanded)
        if withheld_product:
            ledger_met = None
    ledger_partial: dict = {}
    if unlanded and isinstance(params.get("ledger_verdict"), dict):
        from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as _tcl

        served = {r.id: tuple(r.register_rows) for r in manifest.rows} if manifest is not None else {}
        downgraded = _tcl.downgrade_unlanded_items(params["ledger_verdict"], unlanded, served)
        if downgraded is not params["ledger_verdict"]:
            ledger_partial = {
                str(new["id"]): list(new.get("remaining") or [])
                for old, new in zip(params["ledger_verdict"]["items"], downgraded["items"]) if old is not new
            }
            if ledger_met:
                met_ids = _ledger_met_ids(downgraded)
                ledger_met = ("ledger-met: " + ", ".join(met_ids))[:_UNDONE_CAP] if met_ids else None
            if ask_run_dir is not None:
                _tcl.rewrite_note_verdicts(
                    root / delivery_note_locate(root, ask_run_dir.name), downgraded, inline_review, root,
                )

    # A review that applied fixes anchors its trailer on the plan path even when the product matches HEAD.
    fixes_anchor = bool(inline_review.get("fixes")) and bool(request.plan_path) and bool(_review_trailer(record_abs))
    if not paths and not deleted:
        if params.get("allow_empty") is True or ((anchor_only or request.review_only or fixes_anchor) and _review_trailer(record_abs)):
            paths = [p for p in declared if p not in absent_dropped and (root / p).is_file()]
            if not paths and request.plan_path and request.plan_path not in process_withheld and (root / request.plan_path).is_file():
                paths = [request.plan_path]
        if not paths and not deleted:
            _exclude_withheld()
            return {
                "committed": False, "nothing_to_commit": True,
                "run_artifacts": {"landed": [], "withheld": {**art_withheld, **art_dropped}},
                "stranded": {c: sorted(ps) for c, ps in stranded.items()},
                **_peer_held_left(peer_held),
                **recovery_reply,
                **({"resume_recorded": {"landed": [], "withheld": dict(sorted(recorded_reason.items()))}}
                   if recorded_reason else {}),
            }

    zero_path = _zero_path_prefix_dirty(
        root, [c for c in credited_chunks if c.id not in unlanded_rows], landing_now | set(paths) | set(deleted), cache,
    )
    if zero_path:
        shown = "; ".join(f"{cid}: {', '.join(ps)}" for cid, ps in sorted(zero_path.items()))
        return _error(
            f"credited chunk(s) landed no path while files sit under their prefix: {shown}. "
            "List them under `created-under-prefix:` in the report, or list the chunk in "
            "params.incomplete_chunks, then re-fire",
            refused=credit_gate.REFUSED_CREDITED_PREFIX_ZERO_PATHS,
            unlanded=zero_path,
        )

    # ---- receipts, integrity, drift expectations
    land_deadline.mark("land_run.receipts")
    from datetime import datetime, timezone

    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit.completion_receipt import _remove, build_run_receipts, write_run_receipts

    receipt_paths: list = []
    receipts_built: list = []
    credited_ids = [c.id for c in credited_chunks if c.id not in unlanded_rows]
    try:
        receipts_built = [] if anchor_only else build_run_receipts(
            root, request, done_ids=[c.id for c in done], incomplete_ids=sorted(incomplete),
            record=record, script_path=script_path, session_id=session_id, base_sha=head_sha(root),
            branch=observed_branch, now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            local=product_only,
        )
        receipt_paths = write_run_receipts(root, receipts_built) if receipts_built else []
    except Exception as exc:  # noqa: BLE001 -- write_run_receipts already removed its partial writes
        return _error(f"completion receipt write failed: {exc}", refused="receipt-write-failed")
    if not product_only:
        paths.extend(p for p in receipt_paths if p not in paths)

    # `changed` covers only dirty declared paths, narrower than terminal_commit's worktree-wide snapshot.
    landing_carried = set(paths) | set(deleted)
    residue_py = sorted(
        p for p in changed if p.endswith(".py") and p not in landing_carried and (root / p).is_file()
    )
    deleted_notes: list = []
    settled_stage: dict = {}
    try:
        land_deadline.check("land_run.integrity")
        findings = _integrity(
            root, params, paths, deleted, record_abs, sibling_run_id, request.plan_path, residue_py=residue_py,
            own_declared=declared, session_id=session_id, minted_before=own_seniority,
            deleted_notes=deleted_notes,
            released_holders=(
                frozenset(auto_landed_holders or ()) | {h for _hunks, h, _p in halted_clear.values()}
                | {h for hs in measured_released.values() for h in hs}
            ),
            settled=settled_stage,
        )
    except land_deadline.LandDeadlineExceeded:
        _remove(root, receipt_paths)
        raise
    for p in sorted(deferred):
        if p in settled_stage:
            peer_held.pop(p, None)
        elif p in paths:
            paths.remove(p)
            withhold_reason[p] = "peer-held"
            named_out.add(p)
            if p in recorded_changed:
                recorded_reason[p] = "peer-held"
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate as _phg

    # A per-run artifact still co-declared after settle is withheld unless its sibling is halted; an
    # UNREADABLE_PEER_CLAIMS sibling has unknown halt state and withholds too.
    for f in findings:
        if (
            f.reason != _phg.PEER_HUNKS_CO_DECLARED or f.path not in paths or f.path in settled_stage
            or not _cds.is_per_run_artifact(f.path)
        ):
            continue
        sibling = f.detail.partition(":")[0]
        if sibling != _phg.UNREADABLE_PEER_CLAIMS and _cds.sibling_halted(root, sibling):
            continue
        paths.remove(f.path)
        withhold_reason[f.path] = "peer-held"
        named_out.add(f.path)
        if f.path in recorded_changed:
            recorded_reason[f.path] = "peer-held"
    for p, (hunks, holder, _partial) in list(halted_clear.items()):
        if p not in paths:
            continue
        if p in settled_stage:  # a second staging claim on the path: the holder's hold stands whole
            paths.remove(p)
            withhold_reason[p] = f"withheld by {holder}"
            foreign_held[p] = holder
            del halted_clear[p]
            continue
        settled_stage[p] = hunks
    if foreign_held:
        from coordinator_core.ops.dispatch_emit import landing_integrity as li, peer_hunk_gate

        # Non-retired job: provenance only. The claims a halted holder's withheld path carries are
        # recorded in the reply for the commit agent; they never refuse (DR-474).
        findings += [
            li.Finding(
                p, peer_hunk_gate.PEER_HUNKS_CO_DECLARED,
                (
                    f"{sibling}: a peer's claims could not be read, so the path is held"
                    if sibling == peer_hunk_gate.UNREADABLE_PEER_CLAIMS
                    else f"{sibling}: also declared by that unlanded run, whose hunks a path commit cannot split off"
                ),
            )
            for p, sibling in peer_hunk_gate.co_declared_peer_holds(
                root, sibling_run_id, sorted(foreign_held), declared, own_plan=request.plan_path,
                minted_before=own_seniority,
            )
        ]
    from coordinator_core.ops.dispatch_emit import landing_integrity as _li_claims

    findings, claim_provenance = _li_claims.split_claim_provenance(findings)
    carried_targets, tests_without_source = _carry_withheld_symbol_targets(
        root, claim_provenance, paths, deleted,
        barred={*held_paths, *em_withheld, *withhold_reason, *art_withheld, *art_dropped, *recorded_reason},
        session_id=session_id, rid=rid, script_path=script_path, plan_rel=request.plan_path,
        run_base=run_base, minted_before=own_seniority, own_run_ids=(rid, sibling_run_id),
    )
    paths.extend(carried_targets)
    tests_without_source = {
        **_tests_importing_dropped_modules(
            root, [p for p in paths if p not in tests_without_source],
            {
                p for p in [*declared_changed, *art_withheld, *art_dropped]
                if p.endswith(".py") and not _is_test_path(p) and p not in paths and p not in deleted
                and (root / p).is_file()
            },
        ),
        **tests_without_source,
    }
    if tests_without_source:
        paths[:] = [p for p in paths if p not in tests_without_source]
        claim_provenance = [
            f for f in claim_provenance
            if not (f.reason == _li_claims.IMPORT_CLOSURE_UNTRACKED and f.path in tests_without_source)
        ]
        withhold_reason.update(tests_without_source)
        named_out.update(tests_without_source)
    if findings:
        _remove(root, receipt_paths)
        first = findings[0]
        remedy = ""
        from coordinator_core.ops.dispatch_emit import peer_hunk_gate

        hold_pairs = [
            (f.path, f.detail.partition(":")[0]) for f in findings
            if f.reason == peer_hunk_gate.PEER_HUNKS_CO_DECLARED
        ]
        if hold_pairs:
            remedy = ". " + peer_hunk_gate.refusal_message(hold_pairs, sibling_run_id, root=root)
        if first.reason == "since-base-overlap":
            from coordinator_core.ops import warp_run_deliverable
            from coordinator_core.ops.dispatch_emit import sibling_claims

            remedy += ". " + sibling_claims.reland_remedy(warp_run_deliverable.record_sizing_rel(root, sibling_run_id))
        from coordinator_core.ops.dispatch_emit.landing_targeted_tests import retest_step

        return _error(
            f"landing refused {first.reason}: {first.path} ({first.detail}){remedy}",
            refused=first.reason,
            integrity=[{"path": f.path, "reason": f.reason, "detail": f.detail} for f in findings],
            remedy_step=retest_step(findings, params),
        )

    reach_summary: dict = {}
    shipped_summary: dict = {}
    if ask_run_dir is not None and token_state.get("since_halt_close") is None:
        try:
            note_text = (root / delivery_note_locate(root, ask_run_dir.name)).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            note_text = None
        from coordinator_core.ops.dispatch_emit.reach_claims import REACH_CLAIM_REFUSED, reach_gate
        from coordinator_core.ops.dispatch_emit.shipped_claims import refused_code, shipped_gate

        gate_withheld = {
            p: (
                f"held chunk {', '.join(held_paths[p])}" if p in held_paths and why == "withheld"
                else f"withheld by the EM: {em_withheld[p]}" if p in em_withheld and why == "withheld"
                else why
            )
            for p, why in {
                **{p: "withheld" for p in held_paths}, **art_withheld, **art_dropped, **recorded_reason,
                **withhold_reason,
            }.items()
            if not (_own_inherited(p) and p not in withhold_reason)
        }
        refusal, reach_summary = reach_gate(
            root, note_text, landing_paths=paths, deleted_paths=deleted, withheld_paths=gate_withheld,
        )
        if refusal is not None:
            _remove(root, receipt_paths)
            return _error(refusal, refused=REACH_CLAIM_REFUSED, reach_claims=reach_summary)
        own_run_ids = {r for r in (rid, sibling_run_id) if isinstance(r, str) and r}
        row_of = {
            p: (cid, f"executor PARTIAL: {blockers[cid]}" if cid in blockers else "incomplete")
            for cid, ps in stranded.items() for p in ps
        }

        def _owners(claimed: Sequence[str]) -> dict:
            """``{path: owner}`` for claimed paths another run withholds (halted index) or a live peer holds (``_peer_held``)."""
            wanted = set(claimed)
            from coordinator_core.ops.dispatch_emit import halted_withheld_index

            found = {
                p: str(run) for p, run in halted_withheld_index.withheld_by_path(root).items()
                if p in wanted and run not in own_run_ids
            }
            held = _peer_held(
                root, [p for p in claimed if p not in found], session_id, rid, script_path,
                plan_rel=request.plan_path, run_base=run_base if isinstance(run_base, str) else None,
                minted_before=own_seniority,
            )
            found.update({p: ", ".join(sorted(s)) for p, s in held.items() if s})
            return found

        from coordinator_core.ops.dispatch_emit.in_run_widen import widened_paths

        refusal, shipped_summary = shipped_gate(
            root, note_text, landing_paths=paths, deleted_paths=deleted, withheld_paths=gate_withheld,
            own_paths=set(declared), row_of=row_of, owners=_owners, run_id=sibling_run_id or rid,
            widened=widened_paths(ask_run_dir) if ask_run_dir is not None else (),
        )
        if refusal is not None:
            from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import is_refusal

            code = refused_code(shipped_summary)
            if is_refusal(code):
                _remove(root, receipt_paths)
                return _error(refusal, refused=code, shipped_claims=shipped_summary)
            claim_provenance.append(_li_claims.Finding("", code, refusal))

    expected: dict = {}
    reread_artifacts: list = []
    if session_id is not None:
        fixes_at, wave_paths = _fixes_integrated_at(root, inline_review)
        if fixes_at is None:
            fixes_at, wave_paths = review_only_anchor(root, request, inline_review)
        note_rel = delivery_note_locate(root, art_run_id) if isinstance(art_run_id, str) and art_run_id else None
        expected = _recorded_fingerprints(
            root, session_id, [p for c in done for p in c.paths], [*paths],
            excluded={*receipt_paths, bookkeeping_path, note_rel},
            reviewed=reviewed_bytes, reviewed_at=reviewed_at, fixes_integrated_at=fixes_at,
            wave_paths=wave_paths,
            review_events=_review_write_events(root, session_id, sibling_run_id, inline_review),
            run_started_at=_submitted_at(root, art_run_id),
        )
        for ev_path, ev_before, ev_after in ([own_evidence_write] if own_evidence_write else []):
            if ev_path in paths and expected.get(ev_path) == ev_before:
                expected[ev_path] = ev_after
        if expected and isinstance(art_run_id, str) and art_run_id:
            from coordinator_core.ops.dispatch_emit import run_artifact_ownership

            owned = run_artifact_ownership.own_artifact_paths(root, art_run_id)
            reread_artifacts = owned.drop_expectations(expected)

    # ---- the message
    subject = (
        f"review anchor: {request.plan_path} -- delivery PASS, no product files" if anchor_only
        else _subject(contributing, f"review trail: {inline_review['integration_stem']}")
    )
    paragraphs = [subject]
    if deleted:
        paragraphs.append("Removes declared write(s): " + ", ".join(deleted))
    trailers = []
    trailers.extend(f"Targeted-Tests-Note: {n}" for n in deleted_notes)
    if request.deliverable_id:
        trailers.append(f"Deliverable-Id: {request.deliverable_id}")
    if plan_text is not None:
        trailers.extend(
            f"Deliverable-Id: {did}" for did in plan_trailer_deliverable_ids(plan_text) if did != request.deliverable_id
        )
    resolves = None if anchor_only or incomplete or unlanded_rows else _resolving_sizing(root, request.plan_path)
    if resolves is not None:
        trailers.append(f"Resolves-Sizing: {resolves['id']} {resolves['tshirt']}")
    if credited_ids:
        trailers.append("Lands-Chunks: " + ", ".join(credited_ids))
    trailers.extend(f"{contract.UNLANDED_ROW_TRAILER}: {row} {p} {reason}" for row, p, reason in unlanded)
    trailers.extend(
        "Requirement-Open: " + " ".join((str(r["id"]) + " " + str(r["reason"])).split()) for r in open_rows
    )
    trailers.extend(
        f"Verified-Only: {rid_} -- {' '.join(ev['command'].split())} -> {' '.join(ev['observed'].split())}"
        for rid_, ev in sorted(_verified_only(params.get(TERMINAL_RESUME_LEDGER_PARAM)).items())
    )
    trailers.append(
        f"Inline-Review: applies {inline_review['integration_stem']} -- execute-review: "
        f"{inline_review['slices']} slices, {inline_review.get('fixes')} fixes"
    )
    trailers.extend(review_floor.floor_trailer(n) for n in floor_notes)
    if ledger is not None:
        trailers.append(f"Ledger-Receipt: {review_floor.receipt(review_floor.verdict_count(inline_review, root))}")
    trailers.extend(_run_closed_lines(root, art_run_id or sibling_run_id, params))
    if product_only:
        trailers.extend(_ask_trailers(root, request.plan_path, record_abs, receipts_built))
    else:
        trailers.extend(_review_trailer(record_abs))
    review_trailed = any(t.startswith("Review: ") for t in trailers)
    paragraphs.append("\n".join(trailers))
    message = "\n\n".join(paragraphs)

    # ---- the commit
    from coordinator_core.git.commit import (
        BlobDriftRefused,
        CommitRefused,
        NothingToCommit,
        commit_paths,
        hash_worktree_blobs_via_spawn,
    )
    from coordinator_core.git.index_write import IndexStaleAfterCommit
    from coordinator_core.git.ref_cas_retry import is_ref_moved, retry_ref_moved

    index_stale: list = []

    from coordinator_core.git import merge_owner
    from coordinator_core.ops.dispatch_emit import merge_landing

    commit_set, commit_deleted = paths, deleted
    merge_plan = merge_landing.landing_plan(
        root, paths, deleted, product=set(declared), owner_run_id=sibling_run_id or "",
    )
    merge_artifacts: dict = {}
    merge_keep: set = set()
    if merge_plan is not None and merge_plan.get("refusal"):
        _remove(root, receipt_paths)
        return _error(merge_plan["refusal"], **merge_plan["extra"])
    if merge_plan is not None and not merge_plan.get(merge_landing.FOREIGN_KEY):
        commit_set, commit_deleted = merge_plan["paths"], merge_plan["deleted"]
        merge_artifacts = merge_plan["artifacts"]
        merge_keep = set(commit_set)
    else:
        merge_plan = None

    excluded_unmeasured: list = []
    if (
        measured is not None and merge_plan is None and not measured.get("unreadable")
        and not (anchor_only or request.review_only)
    ):
        recorded = {
            *(measured.get("blobs") if isinstance(measured.get("blobs"), dict) else ()),
            *_measured_strs(measured, "removed"),
        }
        carried = {*art_present, *receipt_paths, bookkeeping_path}
        excluded_unmeasured = [p for p in commit_set if p not in carried and p not in recorded]
        if excluded_unmeasured:
            row_writes = {w for row in (manifest.rows if manifest is not None else ()) for w in row.writes}
            excluded_unmeasured = [p for p in excluded_unmeasured if p not in row_writes]
        if excluded_unmeasured:
            own_covers = _run_owned(root, art_run_id).covers
            excluded_unmeasured = [p for p in excluded_unmeasured if not own_covers(p)]
        if excluded_unmeasured:
            own_now = set(_own_bytes_now(root, ask_run_dir, session_id, sibling_run_id, inline_review)(excluded_unmeasured))
            excluded_unmeasured = sorted(p for p in excluded_unmeasured if p not in own_now)
        if excluded_unmeasured:
            commit_set = [p for p in commit_set if p not in excluded_unmeasured]

    settled_blobs: dict = {}
    if settled_stage and merge_plan is not None:
        _remove(root, receipt_paths)
        return _error(
            f"own-hunk staging cannot ride a merge commit: {', '.join(sorted(settled_stage))}",
            refused="own-hunk-stage-refused",
        )
    if settled_stage:
        from coordinator_core.ops.dispatch_emit import co_declared_settle, own_hunk_stage

        try:
            diffs = own_hunk_stage.read_diff(root, [p for p in settled_stage if p in commit_set])
            moved = own_hunk_stage.check_head_unchanged(root, diffs)
            if moved is not None:
                _remove(root, receipt_paths)
                return _error(
                    f"{moved} moved in HEAD while its own hunks were staged; re-fire the landing",
                    refused="own-hunk-head-moved",
                )
            settled_blobs = {
                p: co_declared_settle.settled_blob(root, p, d.head_bytes, settled_stage[p]) for p, d in diffs.items()
            }
        except own_hunk_stage.OwnHunkRefused as exc:
            _remove(root, receipt_paths)
            return _error(f"own-hunk staging refused: {exc}", refused="own-hunk-stage-refused")

    unclaimed_base = {
        p: b for p, b in (manifest.write_baseline if manifest is not None else {}).items()
        if b is not None and p in commit_set and p not in settled_blobs and (root / p).is_file()
    }
    if unclaimed_base:
        from coordinator_core.ops.dispatch_emit import own_hunk_stage, unclaimed_dirt_stage

        try:
            diffs = own_hunk_stage.read_diff(root, list(unclaimed_base))
            own_blobs, own_refusals = unclaimed_dirt_stage.own_only_blobs(
                root, str(sibling_run_id or rid), list(unclaimed_base), unclaimed_base,
            )
            moved = own_hunk_stage.check_head_unchanged(root, diffs)
        except own_hunk_stage.OwnHunkRefused as exc:
            _remove(root, receipt_paths)
            return _error(f"own-hunk staging refused: {exc}", refused="own-hunk-stage-refused")
        if moved is not None:
            _remove(root, receipt_paths)
            return _error(
                f"{moved} moved in HEAD while its own hunks were staged; re-fire the landing",
                refused="own-hunk-head-moved",
            )
        own_refusals = _drop_lost_baseline_verified(root, own_refusals, measured)
        if own_refusals:
            _remove(root, receipt_paths)
            return _error(
                "own-hunk staging refused: "
                + "; ".join(f"{p} ({run or 'unattributed'}): {header}" for p, run, header in own_refusals),
                refused="own-hunk-stage-refused",
            )
        if own_blobs and measured is not None and not measured.get("unreadable"):
            verified = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
            own_blobs = {
                p: b for p, b in own_blobs.items() if not _verified_preexisting(root, p, verified.get(p), unclaimed_base)
            }
        if own_blobs and merge_plan is not None:
            _remove(root, receipt_paths)
            return _error(
                f"own-hunk staging cannot ride a merge commit: {', '.join(sorted(own_blobs))}",
                refused="own-hunk-stage-refused",
            )
        settled_blobs = {**settled_blobs, **own_blobs}
    if settled_blobs and measured is not None and merge_plan is None and not measured.get("unreadable"):
        verified = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
        settled_blobs = {
            p: b for p, b in settled_blobs.items()
            if not (isinstance(verified.get(p), str) and _worktree_blob(root, p) == verified[p])
        }

    # An "inconclusive" overlay (no seeds, timeout, collection error) lands with an advisory; only red,
    # missing and mismatch refuse.
    reach_dirt_foreign: dict = {}
    reach_dirt_overlay: dict = {}
    if measured is not None and merge_plan is None and not (anchor_only or request.review_only):
        non_product = {*art_present, *receipt_paths, bookkeeping_path}
        differing = (
            ["measured-rev.json (unreadable)"] if measured.get("unreadable") else _measured_mismatch(
                root, measured, [p for p in commit_set if p not in non_product],
                [p for p in commit_deleted if p not in set(art_deleted)], settled_blobs,
                own=_run_owned(root, art_run_id),
                baseline=manifest.write_baseline if manifest is not None else None,
                owned_now=_own_bytes_now(root, ask_run_dir, session_id, sibling_run_id, inline_review),
                no_change=_run_made_no_change(
                    root, measured_run, manifest.write_baseline if manifest is not None else None,
                    _own_bytes_now(root, ask_run_dir, session_id, sibling_run_id, inline_review),
                ),
            )
        )
        if differing:
            _remove(root, receipt_paths)
            _reap_measured_export(root, measured_run)
            mblobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
            peers = {} if measured.get("unreadable") else _live_peer_holders(
                root, list(differing), session_id, rid, script_path, request.plan_path,
                run_base if isinstance(run_base, str) else None, own_seniority,
                _firing_sessions(root, rid, session_id),
            )
            return _error(
                "landing refused: the product changes this commit would introduce differ from the measured-rev "
                "record: " + " | ".join(
                    d if measured.get("unreadable") else (
                        f"{d}: measured {mblobs.get(d)}, held by {peers[d]}: land after it; "
                        "the measured blob may carry its unfinished hunks, so commit nothing of this path by hand"
                        if d in peers else _mismatch_detail(
                            root, d, mblobs, settled_blobs, measured_run, omitted=d not in commit_set,
                        )
                    )
                    for d in differing
                ),
                refused=LANDING_TREE_DIFFERS, paths=differing,
            )
        reach_dirt = (
            {} if measured.get("unreadable") else _verify_reach_dirt(
                root, measured, [p for p in commit_set if p not in non_product],
                [p for p in commit_deleted if p not in set(art_deleted)],
            )
        )
        if reach_dirt:
            reach_dirt, reach_dirt_foreign = _split_reach_dirt(
                root, reach_dirt, art_run_id, request.plan_path, session_id, rid, script_path,
                run_base=run_base if isinstance(run_base, str) else None,
                minted_before=own_seniority, self_sessions=_firing_sessions(root, rid, session_id),
            )
        if reach_dirt:
            reach_dirt_foreign.update({p: "unattributed" for p in reach_dirt})
        if reach_dirt_foreign:
            from coordinator_core.ops.dispatch_emit import clean_overlay_verify

            overlay = (
                clean_overlay_verify.check_verdict(root, measured_run, measured)
                if measured_run and safe_id(measured_run)
                else clean_overlay_verify.OverlayVerdict("missing", "the landing has no measured run id")
            )
            reach_dirt_overlay = {"verdict": overlay.verdict, "seeds": list(overlay.seeds)}
            if overlay.verdict in ("inconclusive", "missing"):
                reach_dirt_overlay["advisory"] = f"clean-overlay verdict {overlay.verdict}: {overlay.detail}"
                # The landing is recoverable so it proceeds, but unverified: the trailer is what a release
                # gate reads to list the commits the full-tree regate alone covers.
                why = " ".join(str(overlay.detail).split())[:200]
                body = message.rstrip("\n")
                last = body.rsplit("\n\n", 1)[-1].splitlines()
                # Join an existing trailer block: git parses trailers from the last paragraph only.
                sep = "\n" if last and all(_TRAILER_LINE.match(ln) for ln in last) else "\n\n"
                message = f"{body}{sep}Overlay: {overlay.verdict} — {why}\n"
            elif overlay.verdict != "green":
                _remove(root, receipt_paths)
                _reap_measured_export(root, measured_run)
                return _error(
                    f"landing refused: peer dirt is in the verified tests' reach and the clean-overlay verdict "
                    f"is {overlay.verdict}: {overlay.detail}. Run "
                    f"{clean_overlay_verify.remedy_call(measured_run)} in the verify stage, then re-land",
                    refused=VERIFY_OVERLAY_RED if overlay.red else VERIFY_OVERLAY_UNVERIFIED,
                    reach_dirt_foreign=reach_dirt_foreign,
                    paths=sorted(reach_dirt_foreign),
                )

    try:
        land_deadline.check("land_run.commit")
    except land_deadline.LandDeadlineExceeded:
        _remove(root, receipt_paths)
        raise

    def _attempt():
        # Trap: checked once before the commit; an adopter arriving after it is not stopped here.
        if token_state.get("claim") and _claim_adopted(token_state.get("identity"), token_state["claim"]):
            raise _ClaimAdopted()
        from coordinator_core.ops.workflow_fire.finalizer import authorize_product
        authorize_product(root, commit_set, commit_deleted, reviewed_bytes,
                          host_metadata={'owned-landable-artifacts': art_present,
                                         'terminal-receipts': receipt_paths,
                                         'bookkeeping-record': [bookkeeping_path]},
                          supplied_blobs=settled_blobs, merging=merge_plan is not None)
        try:
            return commit_paths(
                root, commit_set, message, deleted_paths=commit_deleted,
                supplied_blobs=settled_blobs or None,
                expected_content_sha256=(
                    {k: v for k, v in expected.items() if k in merge_keep and k not in settled_blobs}
                    if merge_plan is not None else {k: v for k, v in expected.items() if k not in settled_blobs}
                ) or None,
                finish_merge=merge_plan is not None,
                allow_empty=params.get("allow_empty") is True or ((anchor_only or request.review_only or fixes_anchor) and review_trailed),
                blob_fallback=lambda ps: hash_worktree_blobs_via_spawn(ps, cwd=root),
            )
        except CommitRefused as lost_exc:
            if is_ref_moved(lost_exc):
                return lost_exc
            raise

    # Receipt files are run-unique, so no peer commit can have touched them.
    own_receipts = set(receipt_paths)
    overlap_paths = [p for p in [*commit_set, *commit_deleted] if p not in own_receipts]
    drift_moved = False
    own_moved = False
    while True:
        try:
            outcome, overlap = retry_ref_moved(
                root, overlap_paths, _attempt, lambda r: isinstance(r, CommitRefused)
            )
            if isinstance(outcome, CommitRefused):
                if overlap:
                    _remove(root, receipt_paths)
                    return _error(f"{outcome} {_OVERLAP_REMEDY}", refused=overlap)
                raise outcome
            break
        except _ClaimAdopted:
            return _error(
                "the run token claim was adopted by a later call past this call's op budget; "
                "this call committed nothing",
                refused="run-token-adopted",
            )
        except BlobDriftRefused as exc:
            drift = [
                {"path": d.path, "basis": d.basis, "expected": d.expected, "actual": d.actual} for d in exc.drift
            ]
            real_drift = [d["path"] for d in drift]
            if not own_moved and session_id is not None and expected:
                # An executor's Write/Edit reaches no session ledger: the newest session event is its
                # pre-run read. Paths whose bytes are the run's reviewed, ledgered or recorded blobs
                # are the run's own; the expectation moves to them once, foreign bytes keep refusing.
                from coordinator_core.ops.dispatch_emit.own_drift import _own_drift_actuals, _own_record_dir

                own_moved = True
                own_now = _own_drift_actuals(
                    root, ask_run_dir, drift, declared, session_id, None, reviewed_bytes,
                    _own_record_dir(root, art_run_id, ask_run_dir), sibling_run_id,
                )
                if own_now:
                    expected = {**expected, **own_now}
                    continue
            if not drift_moved:
                from coordinator_core.ops.dispatch_emit.comment_only_drift import tolerated_drift

                # A comment-only edit after the review read the bytes is not a different reviewed
                # product: the expectation moves once and the drift rides a trailer.
                tolerated, real = tolerated_drift(
                    root, inline_review.get("prep_sidecar"), drift, untrusted_paths=[*commit_set, *commit_deleted]
                )
                if tolerated and not real:
                    expected = {**expected, **{t["path"]: t["actual"] for t in tolerated}}
                    message = message.rstrip("\n") + "\n" + "\n".join(
                        f"Comment-Only-Drift: {t['path']} {t['expected'][:12]}->{t['actual'][:12]}" for t in tolerated
                    ) + "\n"
                    drift_moved = True
                    continue
                real_drift = real or real_drift
            _remove(root, receipt_paths)
            return _error(
                blob_drift_remedy(
                    str(exc), root, sibling_run_id, contract.OP_NAME, params.get("task_output_path"), real_drift,
                ),
                refused="blob-drift",
                blob_drift=drift,
            )
        except NothingToCommit as exc:
            _remove(root, receipt_paths)
            return _error(str(exc), nothing_to_commit=True)
        except IndexStaleAfterCommit as exc:
            if exc.outcome is None:
                return _error(f"{exc} (the commit landed; resolve its sha with `git log -1`)")
            outcome = exc.outcome
            index_stale = list(exc.paths)
            from coordinator_core.git.commit import _reset_index_paths

            if _reset_index_paths(root, index_stale):
                index_stale = []
            break
        except CommitRefused as exc:
            _remove(root, receipt_paths)
            return _error(str(exc), refused="commit")
        except Exception as exc:  # noqa: BLE001 -- an unreadable index or ref is a refusal, nothing landed
            _remove(root, receipt_paths)
            return _error(f"{type(exc).__name__}: {exc}", refused="commit")

    sha = outcome.sha
    _reap_measured_export(root, measured_run)
    review_gap: list = []
    declared_left: dict = {}
    gap_dirty: set = set()
    review_code = list(manifest.review_code_paths) if manifest is not None else []
    gap_literals = list(dict.fromkeys([*review_code, *sorted(declared_writes)]))
    if gap_literals:
        try:
            gap_changed, gap_ignored, _gap_head = _dirty_among(root, gap_literals)
            gap_dirty = gap_changed - gap_ignored
            review_gap = sorted(p for p in gap_dirty if p in set(review_code))
            declared_left = {
                p: "declared write left dirty after the commit, neither landed nor withheld"
                for p in sorted(gap_dirty)
                if p in declared_writes and p not in set(review_code) and p not in named_out
                and not (p in halted_clear and p in settled_stage)  # the holder's hunks stay by design
            }
        except Exception:  # noqa: BLE001 -- the commit stands; an unreadable store reports no gap
            review_gap = []
            declared_left = {}
            gap_dirty = set()
    review_gap = sorted({*review_gap, *declared_left})
    if review_gap:
        ledger_met = None
        credited_ids = []
        withheld_product = sorted({*withheld_product, *review_gap})
    merge_artifacts_reply: Optional[dict] = None
    if merge_plan is not None:
        merge_owner.clear(root)
        merge_artifacts_reply = _land_merge_artifacts(root, merge_artifacts, message, sha)
    _exclude_withheld()
    token_state["landed"] = True
    if ask_run_dir is not None and not incomplete and not review_gap:
        from coordinator_core.ops.dispatch_emit.ask_contract import stamp_run_stage_at

        stamp_run_stage_at(ask_run_dir, "landed", warp_run=sibling_run_id)
    _settle_token(token_state, landed=True, receipt={"sha": sha, "rows": credited_ids, "paths": list(paths)})
    if sibling_run_id:
        try:
            from coordinator_core.ops.dispatch_emit import halted_withheld_index

            halted_withheld_index.record_declared(root, sibling_run_id, [])
        except Exception:  # noqa: BLE001 -- the commit stands
            pass
    if applies:
        from coordinator_core.ops.dispatch_emit.run_close import kind_presets, sibling_release

        siblings = None
        try:
            siblings = sibling_release(root, sibling_run_id, sha)
        except Exception:  # noqa: BLE001 -- the commit stands
            pass
    else:
        siblings = None
    origin_landed: list = []
    if request.plan_path:
        from coordinator_core.ops.dispatch_emit import halted_hold_release

        # Never raises: the commit stands whatever the marker rewrite does.
        origin_landed = halted_hold_release.mark_plan_origins_landed(
            root, request.plan_path, sha, list(paths), exclude_run=sibling_run_id,
        )

    halted_auto_landed = (
        _record_halted_auto_landed(root, art_run_id or sibling_run_id, sha, session_id)
        if auto_landed_holders else None
    )

    # ---- the reply
    reply: dict = {
        "committed": True, "sha": sha, "error": None, "refused": None,
        "receipts": receipt_paths,
        "review_stamp": "deferred", "plan_status": "deferred", "sizing_shipped": None,
    }
    if script_source == "receipt":
        reply["script_source"] = script_source
    if merge_artifacts_reply is not None:
        reply["merge_parent"] = outcome.merge_parent
        reply["merge_artifacts"] = merge_artifacts_reply
    if reach_summary:
        reply["reach_claims"] = reach_summary
    if shipped_summary:
        reply["shipped_claims"] = shipped_summary
    if claim_provenance:
        reply["provenance"] = _li_claims.provenance_entries(claim_provenance)
    if deleted_notes:
        reply["targeted_tests_note"] = "; ".join(deleted_notes)
    reply.update(_fence_reply(fence))
    reply.update(_peer_held_left(peer_held))
    if reach_dirt_foreign:
        reply["reach_dirt_foreign"] = reach_dirt_foreign
        if reach_dirt_overlay:
            reply["clean_overlay_verify"] = reach_dirt_overlay
    expanded = sorted(
        p for p in paths
        if p in measured_carries and p not in inherited and (p in footprint_breach or p not in owner)
    )
    if expanded:
        reply["scope_expanded"] = expanded
    if withheld_product:
        reply[shared_path_landing.LEDGER_WITHHELD_PRODUCT] = withheld_product
        reply["exit_criterion_not_stamped"] = (
            f"ledger rows not reported met: commit omits changed file(s) {withheld_product}"[:_UNDONE_CAP]
        )
    reply.update(recovery_reply)
    landed_now_set = {*paths, *deleted}
    foreign_named = {p: why for p, why in withhold_reason.items() if p not in landed_now_set}
    foreign_named.update({
        p: f"withheld by {holder}" for p, (_h, holder, partial) in halted_clear.items()
        if partial and p in settled_stage
    })
    if declared_left or foreign_named or tests_without_source:
        reply["withheld"] = {
            **(reply.get("withheld") or {}), **foreign_named, **tests_without_source, **declared_left,
        }
    reply[contract.REPLY_CREDITED_CHUNKS] = sorted(set(credited_ids) & (set(regraded) | {c.id for c in credited_absent}))
    if recorded_changed:
        landed_now = set(paths)
        stale_reviewed = {p: r for p, r in recorded_reason.items() if r == CHANGED_AFTER_REVIEW}
        if stale_reviewed:
            reply["withheld"] = {**(reply.get("withheld") or {}), **stale_reviewed}
        reply["resume_recorded"] = {
            "landed": sorted(p for p in recorded_changed if p in landed_now),
            "withheld": dict(sorted(recorded_reason.items())),
        }
    from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed

    stamp_landed(script, sha)
    if index_stale:
        reply["index_stale"] = sorted(index_stale)
    if siblings is not None:
        reply["siblings"] = siblings
    if origin_landed:
        reply["origin_landed"] = origin_landed
    if halted_auto_landed is not None:
        reply["halted_auto_landed"] = {"holders": auto_landed_holders, **halted_auto_landed}
    criterion = inline_review.get("criterion")
    if isinstance(criterion, dict) and criterion.get("status"):
        reply["criterion_status"] = "not_met" if unlanded_rows or review_gap else criterion["status"]
    if ledger_partial:
        reply["ledger_partial"] = ledger_partial
    if ledger_advisory:
        reply["ledger_advisory"] = ledger_advisory
    if excluded_unmeasured:
        reply["excluded_unmeasured"] = excluded_unmeasured
    if objective_gate is not None:
        reply["objective_gate"] = {k: objective_gate[k] for k in ("outcome", "exit_reason", "unmet_axes", "summary")}
    reasons = _incomplete_reasons(
        incomplete, held_now, _held_reasons(params), withheld_by_gate, blockers, regraded, unlanded
    )
    if reasons:
        reply["incomplete_reasons"] = reasons
    if stranded or reasons:
        plan_arg = request.plan_path or params.get("plan_path") or "<plan>"
        run_text = params.get("task_output_path") or "<run-output-file>"
        command = f"emit-dispatch-workflow --plan {plan_arg} --only-incomplete {run_text}"
        reply["next"] = (
            f"stranded files for {', '.join(sorted(stranded))}: keep them in tree, re-emit with {command}"
            if stranded else f"re-emit the incomplete rows with {command}"
        )
    if landable_artifacts is not None:
        landed_now = set(paths) | set(deleted)
        withheld_art = {**(dict(art_landable.withheld) if art_landable else {}), **art_withheld, **art_dropped}
        withheld_art.update(process_withheld)
        reply["run_artifacts"] = {
            "landed": sorted(p for p in landable_artifacts.paths() if p in landed_now),
            "withheld": dict(sorted({**withheld_art, **foreign_named}.items())),
        }
        if reread_artifacts:
            reply["run_artifacts"]["reread"] = reread_artifacts
        artifact_paths = set(landable_artifacts.paths())
        still = [p for p in changed if p in artifact_paths and p not in recovery_paths]
        residue = run_artifacts.residue(landable_artifacts, still, landed_now, withheld_art)
        if residue:
            reply["run_artifact_residue"] = residue
            reply["error"] = (
                "run artifact(s) still dirty after the landing: "
                + ", ".join(f"{p} ({cls})" for p, cls in residue.items())
            )

    from coordinator_core.ops.dispatch_emit.product_residue import declared_product_residue

    product_residue = declared_product_residue(
        {p for p in declared_writes if p not in named_out and not (p in halted_clear and p in settled_stage)},
        gap_dirty, set(paths) | set(deleted),
    )
    if product_residue:
        reply["product_residue"] = product_residue
        reply["ok"] = False
        product_error = "declared product write(s) still dirty after the landing: " + ", ".join(product_residue)
        reply["error"] = f"{product_error}; {reply['error']}" if reply.get("error") else product_error

    from coordinator_core.git import residue_classes

    reply.update(residue_classes.dirty_engine_residue(
        root,
        exclude=set(paths) | set(deleted) | set(recovery_paths)
        | (set(landable_artifacts.paths()) if landable_artifacts is not None else set()),
    ))

    # ---- the close job
    if applies:
        presets = kind_presets(kind, plan_governs=plan_governs)
        review_record = bookkeeping_path if (bookkeeping_path and (root / bookkeeping_path).is_file()) else None
        if review_record is None and record_abs is not None:
            try:
                review_record = record_abs.relative_to(root).as_posix()
            except ValueError:
                review_record = None
        halt_sha = (params.get(TERMINAL_RESUME_LEDGER_PARAM) or {}).get("halt_sha")
        inputs: dict = {"credited_rows": credited_ids}
        if isinstance(halt_sha, str) and _SHA40.fullmatch(halt_sha):
            inputs["resume_halt_sha"] = halt_sha
        if review_record:
            inputs["review_record"] = review_record
        if ledger_met:
            inputs["ledger_met"] = ledger_met
        if ledger is not None:
            inputs["ledger_judged"] = [str(i.id) for i in ledger.items]
        unfinished = sorted(incomplete | unlanded_rows | held_ids)
        if unfinished:
            inputs["incomplete"] = unfinished
        if unlanded_rows:
            inputs["unlanded"] = sorted(unlanded_rows)
            inputs["unlanded_paths"] = {
                row: sorted({p for r, p, _reason in unlanded if r == row}) for row in sorted(unlanded_rows)
            }
        if request.review_only and request.review_open_rows and request.review_run_base:
            inputs["review_only"] = {
                "open_rows": list(request.review_open_rows), "run_base": request.review_run_base,
                "rows": [{"id": c.id, "paths": list(c.paths)} for c in request.chunks if c.id in set(request.review_open_rows)],
            }
        inputs["landed_paths"] = sorted({*paths, *deleted})
        blocked_rows: list = []
        if sibling_run_id and request.plan_path:
            try:
                from coordinator_core.ops.dispatch_emit.sibling_blockers import sibling_blockers

                blocked_rows = sibling_blockers(root, sibling_run_id, request.plan_path)
            except Exception:  # noqa: BLE001 -- the commit stands
                blocked_rows = []
        if blocked_rows:
            inputs["sibling_blocked"] = blocked_rows
        from coordinator_core.ops.dispatch_emit.run_close import own_close_key

        ctx = {
            "key": own_close_key(sibling_run_id, PurePosixPath(script_path.replace("\\", "/")).stem),
            "sibling_run_id": sibling_run_id,
            "presets": presets, "plan_rel": request.plan_path, "session_id": session_id, "sha": sha,
            "review_receipt": review_record, "run_base_sha": _close_job_run_base(root, run_base, sibling_run_id),
            "criterion": (
                {**criterion, "status": "not_met"} if isinstance(criterion, dict) and (unlanded_rows or review_gap)
                else criterion if isinstance(criterion, dict) else None
            ),
            "falsifier_broken": _falsifier_broken_tells(params.get("falsifier_broken"), request.plan_path),
            "inputs": inputs,
        }
        try:
            legs = _close_job(root, ctx)
        except Exception as exc:  # noqa: BLE001 -- the commit stands; the legs read as failed
            legs = {}
            reply["close"] = None
            reply["error"] = reply.get("error") or f"close job not started: {type(exc).__name__}: {exc}"
        if legs or "close" not in reply:
            legs.setdefault("review", _leg("queued", "queued by the review-stamp leg"))
            from coordinator_core.ops.dispatch_emit.run_close import build_close_block, receipt_inputs
            from coordinator_core.session.record_homes import home_dir

            sizing_rel = _cited_sizing(root, request.plan_path)
            sizing_text = _read_rel(root, sizing_rel) if sizing_rel else None
            arm, tshirt = receipt_inputs(sizing_text)
            reply["close"] = build_close_block(
                ctx["key"], True, legs, None, kind=kind, parts=_close_parts_of(sizing_text, ledger_met),
                run_dir=Path(home_dir(str(root), "warp")) / sibling_run_id if sibling_run_id else None,
                arm=arm, tshirt=tshirt,
            )
            if siblings is not None:
                reply["close"]["siblings"] = siblings
            if blocked_rows and isinstance(reply["close"], dict):
                close_siblings = dict(reply["close"].get("siblings") or {})
                close_siblings["blocked_rows"] = blocked_rows
                reply["close"]["siblings"] = close_siblings
    return reply


def _close_parts_of(sizing_text: Optional[str], ledger_met: Optional[str] = None):
    """The run's `Close` read off the register of the plan's cited sizing text and the ledger's met
    observation; no sizing means no parts."""
    from coordinator_core.ops.dispatch_emit.run_close_parts import close_from_sizing_text

    return close_from_sizing_text(sizing_text, ledger_met)


def _falsifier_broken_tells(marks: object, plan_path: Optional[str]) -> Optional[list]:
    """The fired tells when the run's pre-dispatch review called ``plan_path``'s falsifier BROKEN,
    else None. Plans match as repo-relative POSIX paths."""
    if not plan_path or not isinstance(marks, list):
        return None

    def norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    want = norm(plan_path)
    for mark in marks:
        key = mark.get("plan") if isinstance(mark, dict) else None
        tells = mark.get("tells") if isinstance(mark, dict) else None
        if isinstance(key, str) and norm(key) == want:
            return [str(t) for t in tells] if isinstance(tells, list) else []
    return None


def _incomplete_reasons(
    incomplete: set, held: set, held_text: dict, withheld_by_gate: dict, blockers: dict, regraded: dict,
    unlanded: list,
) -> dict:
    """``{row id: why}`` for every row still open after the landing."""
    reasons: dict = {}
    for cid in sorted(set(incomplete) | set(held)):
        if cid in held:
            reasons[cid] = f"held: {held_text.get(cid, '')}"
        elif cid in withheld_by_gate:
            reasons[cid] = withheld_by_gate[cid]
        elif cid in blockers:
            reasons[cid] = f"executor_partial: {blockers[cid]}" if blockers[cid] else "executor_partial"
        else:
            reasons[cid] = "incomplete_unreported"
    for row, path, reason in unlanded:
        reasons.setdefault(row, f"unlanded: {path} ({reason})")
    return reasons


def _own_evidence_rows(done: list, evidence_path: str) -> set:
    """Ids of DONE chunks whose whole footprint is the evidence sidecar this call changed (an
    incomplete row is never credited by an append, whatever its paths)."""
    return {c.id for c in done if c.paths and set(c.paths) <= {evidence_path}}


def _append_evidence(root: Path, plan_path: str, firings: list) -> Optional[tuple]:
    """Append the review rows to the plan's evidence sidecar; ``(path, bytes before, bytes after)``
    when this call changed it. A failed append is not a refusal: the review record still lands."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.ops.criterion_evidence import _criterion_evidence_record
    from coordinator_core.session import touch_record

    try:
        if main_worktree_root(root).resolve() != root.resolve():
            return None
        plan_pp = PurePosixPath(plan_path)
        ev_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SUFFIX))
        before = touch_record.compute_content_hash(root / ev_rel)
        written = _criterion_evidence_record({"plan_path": plan_path, "reviews": firings}, repo_root=root)
        if isinstance(written, dict) and written.get("error"):
            return None
        after = touch_record.compute_content_hash(root / ev_rel)
        return (ev_rel, before, after) if after and after != before else None
    except (OSError, ValueError):
        return None


def _stamp_usage(root: Path, run_id: str) -> None:
    """Stamp ``usage.json`` ``as_of: terminal-commit``: ``sibling_hold`` reads it at HEAD as the
    landed marker. The token figures are the close job's."""
    from coordinator_core.session.claimed_write import replace_text

    target = root / RECORD_DIR_ROOT / run_id / _USAGE_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            body = None
        body = dict(body) if isinstance(body, dict) else {"unsettled": []}
        body["as_of"] = "terminal-commit"
        replace_text(target, json.dumps(body, indent=2, sort_keys=True) + "\n")
    except OSError:
        return


def _within_handoffs_home(root: Path, artifacts):
    from coordinator_core.ops.dispatch_emit.land_run_halt import within_handoffs_home

    return within_handoffs_home(root, artifacts)


def _sizing_stamp_floor(sizing_name: str) -> str:
    """The lowest run stamp that can bind ``sizing_name``: a sizing file name opens with its creation
    date and a run cannot bind a sizing before it exists, so the stamp may be a day early (zone slack).
    ``""`` (no bound) for a name without a valid date prefix."""
    from datetime import date, timedelta

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-", sizing_name)
    if m is None:
        return ""
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - timedelta(days=1)
    except (ValueError, OverflowError):
        return ""
    return day.strftime("%Y%m%d") + "T000000-000000"


def _superseded(root: Path, run_id: str, sizing_rel: str, artifacts, run_token) -> tuple:
    """``(superseded run ids, their changed record files)``: dirty record dirs of earlier runs bound to
    ``sizing_rel``. A record-dir listing finds the earlier same-sizing dirs; one scoped in-process status
    runs over those only, for a run id that carries a stamp tail; a gap in that status skips the scan.
    Trap: test_land_run_off_commit_path spies only on the two corpus scans it names, not on this one."""
    import os

    from coordinator_core.ops.dispatch_emit import run_artifacts

    if not re.search(r"\d{8}T\d{6}-[0-9a-f]{6}$", run_id or ""):
        return (), set()
    from coordinator_core.git.scoped_worktree_status import scoped_worktree_status

    live = {str(r["run_id"]) for r in run_token.held_records(root) if isinstance(r.get("run_id"), str)}
    try:
        listing = [e.name for e in os.scandir(root / RECORD_DIR_ROOT) if e.is_dir()]
    except OSError:
        return (), set()
    mine = run_artifacts._STAMP_TAIL.search(run_id).group(0)
    needle = sizing_rel.replace("\\", "/").rsplit("/", 1)[-1].encode("utf-8")
    floor = _sizing_stamp_floor(needle.decode("utf-8", "replace"))
    candidates: list = []
    base = f"{root}{os.sep}{RECORD_DIR_ROOT}{os.sep}"
    for n in listing:
        tail = run_artifacts._STAMP_TAIL.search(n)
        if tail is None or tail.group(0) >= mine or tail.group(0) < floor:
            continue
        try:
            with open(f"{base}{n}{os.sep}run.json", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if needle in raw or not needle.isascii():
            candidates.append(f"{RECORD_DIR_ROOT}/{n}/run.json")
    bound = run_artifacts.superseded_runs(root, run_id, sizing_rel, candidates, live_run_ids=live)
    if not bound:
        return (), set()
    snap = scoped_worktree_status(root, [], [f"{RECORD_DIR_ROOT}/{n}" for n in bound], [])
    if snap is None:
        return (), set()
    names = run_artifacts.superseded_runs(root, run_id, sizing_rel, snap[0], live_run_ids=live)
    extra = {p for p in snap[0] if any(p.startswith(f"{RECORD_DIR_ROOT}/{n}/") for n in names)}
    return names, extra


def _withhold_reasons(root: Path, artifacts, record_dir_rel: Optional[str]) -> dict:
    """``{path: reason}`` the EM withheld through ``workflow.revise withhold_paths``, read from this
    run's record dir and its superseded predecessors' (the run's own entries win)."""
    from coordinator_core.ops.dispatch_emit import run_withhold

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    return run_withhold.read(root / d for d in reversed(dirs))


def _land_merge_artifacts(root: Path, artifacts: dict, message: str, merge_sha: str) -> dict:
    """One ordinary single-parent commit for the run artifacts the merge commit left out; the
    merge commit stands whatever this returns. Twin of terminal_commit._land_merge_artifacts;
    test_land_run_finishes_a_merge pins the two messages equal."""
    if not artifacts["paths"] and not artifacts["deleted"]:
        return {"committed": False, "nothing_to_commit": True}
    from coordinator_core.git.commit import commit_paths, hash_worktree_blobs_via_spawn

    subject = (message.splitlines() or ["run artifacts"])[0]
    try:
        out = commit_paths(
            root, artifacts["paths"],
            f"{subject} (run artifacts)\n\nFollows merge commit {merge_sha[:12]}.\n",
            deleted_paths=artifacts["deleted"],
            blob_fallback=lambda ps: hash_worktree_blobs_via_spawn(ps, cwd=root),
        )
    except Exception as exc:  # noqa: BLE001 -- the merge already landed; report, never raise
        return {"committed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"committed": True, "sha": out.sha}


def _carry_withheld_symbol_targets(
    root: Path, provenance: Sequence, paths: Sequence[str], deleted: Sequence[str], *, barred: Collection,
    session_id, rid, script_path, plan_rel, run_base, minted_before, own_run_ids: Sequence,
) -> tuple:
    """``(carry, dropped)``. ``carry``: each uncommitted ``.py`` a landed file reads a name from (the
    withheld-symbol pair), so the commit never leaves HEAD calling a name only the worktree defines.
    A target another run withholds, a peer holds, or the landing already carries or removes stays out
    and its provenance entry remains the record. ``dropped`` maps a landing importer whose target stays
    out (withheld, held or barred) to ``TEST_WITHOUT_SOURCE: <target>`` for a test and
    ``LOST_DEFINITION: <target>`` otherwise; the caller removes it from the landing."""
    from coordinator_core.authoring_leaks import import_closure
    from coordinator_core.ops.dispatch_emit import halted_withheld_index, landing_integrity as li

    pairs = import_closure.withheld_symbol_pairs(
        root, [
            (f.path, _OWNER_SUFFIX.sub("", f.detail))
            for f in provenance if f.reason == li.IMPORT_CLOSURE_UNTRACKED
        ],
    )
    if not pairs:
        return [], {}
    halted = halted_withheld_index.withheld_by_path(root)
    own = {str(r) for r in own_run_ids if r}
    out_of_landing = {
        t for t in set(pairs.values())
        if t not in paths and t not in deleted and (root / t).is_file()
    }
    free = sorted(
        t for t in out_of_landing
        if t not in barred and not (t in halted and str(halted[t]) not in own)
    )
    held = _peer_held(
        root, free, session_id, rid, script_path, plan_rel=plan_rel,
        run_base=run_base if isinstance(run_base, str) else None, minted_before=minted_before,
    ) if free else {}
    carry = [t for t in free if not held.get(t)]
    stays_out = out_of_landing - set(carry)
    dropped = {
        imp: f"{TEST_WITHOUT_SOURCE if _is_test_path(imp) else li.LOST_DEFINITION}: {tgt}"
        for imp, tgt in sorted(pairs.items())
        if tgt in stays_out
    }
    return carry, dropped


TEST_WITHOUT_SOURCE = "test-without-source"


def _tests_importing_dropped_modules(root: Path, paths: Sequence[str], dropped: Collection[str]) -> dict:
    """``{test: TEST_WITHOUT_SOURCE: <module file>}`` for each landing test that imports a module file in
    ``dropped`` (a declared product ``.py`` the landing leaves out whole, mixed hunks included). The
    symbol-level carry misses a test that reads only names HEAD already defines but runs the dropped
    file's new behaviour."""
    if not dropped:
        return {}
    import ast

    from coordinator_core.authoring_leaks import import_closure as ic
    from coordinator_core.pyimports import collect_imports

    out: dict = {}
    for rel in paths:
        if not _is_test_path(rel):
            continue
        text = ic.read_text(root / rel)
        try:
            tree = ast.parse(text) if text else None
        except SyntaxError:
            tree = None
        if tree is None:
            continue
        for rec in collect_imports(tree)[0]:
            base = ic._absolute_base(rec, rel)
            if not base:
                continue
            pfx = ic.prefixes(rel)
            files = [*ic.module_files(base, pfx)]
            for name in rec.names:
                if name != "*":
                    files += ic.module_files(f"{base}.{name}", pfx)
            hit = next((f for f in files if f in dropped), None)
            if hit:
                out[rel] = f"{TEST_WITHOUT_SOURCE}: {hit}"
                break
    return out

# The detector suffixes a target another live run claims; the pair is the same, and that run's hold
# is a reason the target stays out, never a reason the importer lands without it.
_OWNER_SUFFIX = re.compile(r"; owned by live run \S+$")


def _is_test_path(rel: str) -> bool:
    """True for ``test_*.py``, ``*_test.py`` and any ``.py`` under a ``tests`` directory."""
    parts = rel.split("/")
    name = parts[-1]
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py") or "tests" in parts[:-1]
    )


def _integrity(
    root: Path, params: dict, paths: list, deleted: list, record_abs: Optional[Path], run_id: str,
    plan: Optional[str], *, residue_py: Sequence[str] = (), own_declared: Sequence[str] = (),
    session_id: Optional[str] = None, minted_before: Optional[tuple] = None,
    deleted_notes: Optional[list] = None, released_holders: frozenset = frozenset(),
    settled: Optional[dict] = None,
) -> list:
    """Pre-commit findings over the landing set: unparseable ``.py``, a ``.py`` importing what the
    commit leaves out of HEAD, a dirty declared ``.py`` the landing leaves behind that imports what
    it deletes, a blob dropping lines a foreign commit added since the run base, and a carried test
    verdict naming a run-caused failure. ``deleted_notes`` is an out-list: the test gate's
    run-deleted-targets line is appended when present. A co-declared hold by a ``released_holders``
    run is skipped; one ``co_declared_settle.settle`` settles is no finding and its own hunks land in
    the ``settled`` out-dict (``{path: [Hunk]}``)."""
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    skip = set(deleted)
    if record_abs is not None:
        try:
            skip.add(Path(record_abs).resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            pass
    live = [p for p in paths if p not in skip]
    from coordinator_core.ops.dispatch_emit import merge_landing

    owned = merge_landing.run_owned_merge(root, run_id)
    findings = li.unparseable_py(root, live)
    land_deadline.check("land_run._integrity.import_closure")
    # The merge commit carries every merge path, so an import of a merge-carried module is landed,
    # not uncommitted.
    if owned is not None:
        findings += li.import_closure_findings(
            root, sorted({*live, *owned.present}), sorted({*deleted, *owned.deleted}),
        )
    else:
        findings += li.import_closure_findings(root, live, deleted)
    try:
        from coordinator_core.authoring_leaks import import_closure

        findings += [
            li.Finding(f.path, li.IMPORT_CLOSURE_UNTRACKED, f"{f.line}: {f.detail}")
            for f in import_closure.residue_findings(root, residue_py, live, deleted)
        ]
    except Exception as exc:  # noqa: BLE001 -- fail closed: an unscannable residue refuses the landing
        findings.append(li.Finding(
            "", li.IMPORT_CLOSURE_UNTRACKED, f"residue import scan failed: {type(exc).__name__}: {exc}",
        ))
    land_deadline.check("land_run._integrity.since_base")
    base = li.run_base_for(root, run_id, params)
    # The merge owner skips the since-base scan for the merge's own paths only: a resolution that
    # takes the other side drops this side's lines by design. A path the merge does not move is
    # still scanned. No readable merge state exempts nothing (fail closed).
    if base is not None:
        scan = live
        if owned is not None:
            moved = {*owned.present, *owned.deleted}
            scan = [p for p in live if p not in moved]
        findings += li.since_base_findings(root, scan, base, run_id=run_id, plan=plan)
    land_deadline.check("land_run._integrity.peer_hunk_gate")
    from coordinator_core.ops.dispatch_emit import merge_ancestry_release, peer_hunk_gate

    from coordinator_core.ops.dispatch_emit import co_declared_settle

    # Non-retired job: hunk-level settlement (`stage` carries only this run's own hunks of a shared
    # path) and the provenance record of what stays co-declared. Never a refusal (DR-474).
    stage, still_held = co_declared_settle.settle(root, run_id, merge_ancestry_release.release(
        root, peer_hunk_gate.co_declared_peer_holds(
            root, run_id, live, own_declared, own_plan=plan, minted_before=minted_before,
        ),
    ), own_plan=plan, minted_before=minted_before)
    if settled is not None:
        settled.update(stage)
    for path, sibling in still_held:
        if sibling in released_holders:
            continue
        findings.append(li.Finding(
            path, peer_hunk_gate.PEER_HUNKS_CO_DECLARED,
            f"{sibling}: also declared by that unlanded run, whose hunks a path commit cannot split off",
        ))
    for path, stem, line in peer_hunk_gate.out_of_ask_holds(root, session_id or "", run_id, live):
        findings.append(li.Finding(path, peer_hunk_gate.OUT_OF_ASK_HUNKS, f"{stem}: {line}"))
    land_deadline.check("land_run._integrity.landing_test_gate")
    with land_deadline.excepted():
        tested, _check = li.landing_test_gate(root, params, run_id, live)
    if deleted_notes is not None and tested.deleted_note:
        deleted_notes.append(tested.deleted_note)
    return findings + tested
