"""dispatch.ask_stage plan legs: the single plan parse, the spine digest, the `executing` stamp, the escaping-write
pre-check, and the per-row brief helpers (prior-report set-aside, kept-row checks, reviewer brief).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import halt_tree_evict, kept_work, requirements_ledger, resume_snapshot, row_returns
from coordinator_core.ops.dispatch_emit.ask_contract import SPINE_SUFFIX, record_dir
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError, _rel, _write
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
    own_gate_owners,
    provisioned_sibling_roots,
)
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import review_inputs, review_specs
from coordinator_core.ops.dispatch_emit.land_run import _DELIVERED_REPORT_RE, _UNDELIVERED_REPORT_RE
from coordinator_core.ops.dispatch_emit.self_dr_discharge import discharge_clauses
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.plan_tasks_render import load_rows


def _set_aside_prior_report(root: Path, report_rel: str) -> Optional[str]:
    """Rename a row report left by an earlier run to `<row id>.prior.md` beside it, replacing an
    older one, and return that repo-relative path; None when no report sits there. Rename only.
    Trap: without this a refired executor finds its own report and replays it."""
    report = root / report_rel
    if not report.is_file():
        return None
    prior = report.with_name(report.stem + ".prior.md")
    report.replace(prior)
    return _rel(root, prior)


def _kept_row_holds(root: Path, snap: Optional[resume_snapshot.Snapshot], row_id: str, report_rel: str) -> bool:
    """True when the gate's snapshot names `row_id` kept, the kept run's `kept.json` fingerprints
    still match the tree, no live note names the row, and the row's report reads DONE.
    Never raises: any unreadable fact is False and the row stages as a fresh execute."""
    if snap is None or not snap.kept_run or row_id not in snap.kept:
        return False
    try:
        run_dir = record_dir(root, snap.kept_run)
        halt_tree_evict.restore(root, run_dir)  # evicted bytes back first: kept.json fingerprints read them
        entry = kept_work.read(run_dir)["chunks"].get(row_id)
        if not kept_work.still_kept(root, entry, requirements_ledger.read_notes(run_dir), row_id, record_dir=run_dir):
            return False
        if not entry.get("paths"):
            return True  # writes-empty: DONE-ness was read from the persisted return
        text = (root / report_rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return bool(_DELIVERED_REPORT_RE.search(text)) and not _UNDELIVERED_REPORT_RE.search(text)


def _persisted_return_line(root: Path, kept_run: str, dep: str) -> str:
    """The brief line naming a kept dependency's persisted return; "" when the kept run holds none."""
    try:
        run_dir = record_dir(root, kept_run)
    except (OSError, ValueError):
        return ""
    if dep not in row_returns.read(run_dir):
        return ""
    where = row_returns.persisted_path(_rel(root, run_dir))
    return (
        f'Row {dep} is kept from run {kept_run}; its return is persisted at {where} under key "{dep}". '
        "Read it there; do not re-obtain it.\n"
    )


def _kept_brief(kept_run: str) -> str:
    return f"Kept from run {kept_run}: this row's finished product and DONE report stand; no executor runs.\n"


def _prior_report_line(prior_rel: str) -> str:
    return (
        f"Prior run's report, set aside: {prior_rel}. It is the previous run's history, never this "
        "run's answer; this row re-executes against the current requirements."
    )


def _discharge_clauses_for(plan_text: str, root: Path, raw_rows: list) -> dict[str, str]:
    """Self-DR discharge clauses keyed by row id; {} without a readable `sizing_object`."""
    split = split_frontmatter(plan_text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return {}
    try:
        sizing = load_sizing(root, cited)
    except SizingFireRefused:
        return {}
    return discharge_clauses(raw_rows, sizing)


def _reviewer_brief(root: Path, run_dir: Path, row_id: str, raw: Mapping) -> str:
    """The blinded reviewer row's brief: exactly the falsifier-integrity phase's spec prompt for
    the plan the row's `falsifier_review_of` names, then the skill-weight headroom clause and the
    no-prose-pinning rule. Writes the can-report-red report the prompt cites under the run dir
    first. Trap: no intent block, plan context or plan text may be added -- any of them un-blinds
    the reviewer. Raises AskStageError, failing the whole
    stage, when the target's verdict is already cached."""
    target = raw.get("falsifier_review_of")
    if not isinstance(target, str) or not target.strip():
        raise AskStageError(f"reviewer row {row_id}: names no falsifier_review_of")
    if raw.get("writes") != []:
        raise AskStageError(f"reviewer row {row_id}: a reviewer is read-only, its writes must be []")
    reviewed = contained_path(root / target.strip(), [root])
    if reviewed is None or not reviewed.is_file():
        raise AskStageError(f"reviewer row {row_id}: falsifier_review_of {target!r} is not a plan under the repo")
    report_dir = Path(_rel(root, run_dir)) / "can-report-red"
    inputs = review_inputs([_rel(root, reviewed)], repo_root=root, report_dir=report_dir)
    if not inputs:
        raise AskStageError(f"reviewer row {row_id}: {target!r} carries no falsifier block")
    item = inputs[0]
    if item.cached is not None:
        raise AskStageError(f"reviewer row {row_id}: a cached verdict needs no reviewer dispatch")
    if item.report_path is not None:
        report = contained_path(root / item.report_path, [root])
        if report is None:
            raise AskStageError(f"reviewer row {row_id}: can-report-red path escapes the repo root")
        _write(report, item.report_json or "")
    # Deferred: ask_compose imports this module at load.
    from coordinator_core.ops.dispatch_emit.ask_compose import SKILL_WEIGHT_HEADROOM_CLAUSE

    return (
        f"{review_specs(inputs)[0].prompt}\n\n{SKILL_WEIGHT_HEADROOM_CLAUSE} "
        "As a reviewer, do not pin prose in tests: never add or demand a test asserting the wording "
        "of a skill, command or brief; assert a stable marker token instead."
    )


PLAN_EXECUTING_STAMP_KEY = "plan_executing_stamp"


def stamp_plan_executing(root: Path, plan_rel: str) -> dict:
    """Advance the plan a warp run just staged from draft/reviewed/approved to `executing`; never raises.

    Replies `{plan, outcome}`: `stamped` (flipped in process; the run's next checkpoint or terminal commit
    carries it, so the stage leg spawns no git and makes no commit), `unchanged` (a minted spine, an unreadable plan, or any other status: left byte-identical) or
    `refused` (+ `error`; the staging stands). A CRLF plan keeps CRLF.

    Lives on the stage leg because staging is the one point every warp run passes, including a run that
    halts before close; `roadmap.plan_gate` counts `executing` as plan-approved. Called by
    `dispatch.ask_stage` and by `dispatch.ask_gate`'s resume legs, never inside `stage()`: its refusals
    raise and a stamp failure must not turn a staged manifest into one. No process spawn.
    """
    out = {"plan": plan_rel, "outcome": "unchanged"}
    if plan_rel.endswith(SPINE_SUFFIX):
        return out
    try:
        from coordinator_core.ops.dispatch_emit.plan_row_coding import advance_to_executing
        from coordinator_core.locked_write import MutateAbort, locked_rmw
        from coordinator_core.ops.plan_status_transition import _FLIPPABLE_STATUSES_ORDER

        plan = contained_path(root / plan_rel, [root])
        if plan is None or not plan.is_file():
            return out
        raw = plan.read_bytes()
        crlf = b"\r\n" in raw
        pre = _FLIPPABLE_STATUSES_ORDER[: _FLIPPABLE_STATUSES_ORDER.index("executing")]
        split = split_frontmatter(raw.decode("utf-8"))
        if split is None or read_fm_field_unquoted(split.fm_text, "status") not in pre:
            return out
        def flip(old: str) -> str:
            text = old.replace("\r\n", "\n")
            new = advance_to_executing(text)
            if new is text:
                raise MutateAbort("status moved before the stamp")
            return new.replace("\n", "\r\n") if crlf else new

        try:
            locked_rmw(plan, flip, repo_root=root)
        except MutateAbort:
            return out
        out["outcome"] = "stamped"
    except Exception as exc:  # noqa: BLE001 -- a stamp failure never halts a staged run
        out["outcome"] = "refused"
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


@dataclass(frozen=True)
class _PlanParse:
    """One parse of a plan: its text, raw rows by id, provisioned sibling roots, spine rows and exclusions."""

    plan_text: str
    raw_by_id: Mapping[str, dict]
    sibling_roots: Mapping[str, Path]
    rows: tuple
    exclusions: tuple


def _parse_plan(root: Path, plan_path: Path, plan_text: str) -> _PlanParse:
    """Parse `plan_text` once; `InvalidChangeKindError` from the spine read propagates."""
    raw_by_id = {
        raw["id"]: raw
        for raw in load_rows(plan_text).rows
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    sibling_roots = provisioned_sibling_roots(raw_by_id.values(), root)
    exclusions: list = []
    rows = read_spine(
        plan_path,
        exclusions=exclusions,
        cleared_gate_owners=own_gate_owners(raw_by_id.values(), root) | frozenset(sibling_roots),
    )
    return _PlanParse(plan_text, raw_by_id, sibling_roots, tuple(rows), tuple(exclusions))


def _escaping_write_refusal(
    root: Path, plan_rel: Optional[str], only_rows, *, parse_out: Optional[list] = None
) -> Optional[str]:
    """The `CrossRepoWriteError` text for a full-plan stage whose rows write outside every checkout, else None.
    Runs before the record dir exists, so a hard refusal leaves nothing on disk; `stage()` re-checks
    approval and grants. Any other failure is left for `stage()` to report. A successful parse is
    appended to `parse_out` for the caller to hand to `stage()`."""
    if not plan_rel or only_rows is not None:
        return None
    try:
        plan_path = contained_path(root / plan_rel, [root])
        if plan_path is None or not plan_path.is_file():
            return None
        parse = _parse_plan(root, plan_path, plan_path.read_text(encoding="utf-8"))
        if parse_out is not None:
            parse_out.append(parse)
        check_cross_repo_writes(parse.rows, root, approved=True)
    except CrossRepoWriteError as exc:
        return str(exc)
    except Exception:
        return None
    return None


def _spine_sha(root: Path, plan_rel: Optional[str]) -> Optional[str]:
    """sha256 over the plan's parsed task spine (rows, briefs included), or None when there is no readable plan.
    The plan's status stamp is outside the spine, so staging never moves it."""
    if not plan_rel:
        return None
    try:
        plan_path = contained_path(root / plan_rel, [root])
        if plan_path is None or not plan_path.is_file():
            return None
        from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

        result = load_rows_memo(plan_path.read_text(encoding="utf-8"))
        raw = json.dumps(list(result.rows or ()), sort_keys=True, default=str)
    except (OSError, ValueError, TypeError):
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
