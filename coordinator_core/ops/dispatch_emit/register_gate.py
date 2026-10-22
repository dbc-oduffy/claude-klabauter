"""
coordinator_core.ops.dispatch_emit.register_gate -- the requirement-register gate and write-back.

``register_gate`` demotes a ``met`` criterion to ``not_met`` when a register row the plan claims is
not judged met, wired and observed; ``register_writeback`` cascades the judge's per-row verdicts
into the cited sizing's register. ``register_target`` resolves which sizing a plan's register
lives in; ``run_started_at`` reads the run's submission time for the embedded-PM-turn check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load
from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel
from coordinator_core.session.record_homes import home_dir

RESOLUTION_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def sizing_file_rel(worktree_root: Path, cited: str) -> Optional[str]:
    guarded = contained_path(worktree_root / cited, [Path(home_dir(str(worktree_root), "sizings"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(worktree_root.resolve()).as_posix()


def register_target(
    worktree_root: Path, plan_path: Optional[str]
) -> tuple[Optional[str], frozenset, bool, dict]:
    """``(register_sizing_rel, claimed_row_ids, is_own, pins)``: the register sizing is the plan's
    ``register_claims.sizing`` when it names an existing sizing, else the cited ``sizing_object``;
    ``is_own`` is True when that is the plan's own ``sizing_object``; ``pins`` is the plan's
    ``register_claims.pins`` (empty when absent)."""
    text = read_rel(worktree_root, plan_path) if plan_path else None
    if text is None:
        return None, frozenset(), True, {}
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops import requirement_register as rr

    split = split_frontmatter(text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    own = sizing_file_rel(worktree_root, cited) if cited else None
    claims_rel, ids = rr.plan_claims(split.fm_text) if split else (None, frozenset())
    target = sizing_file_rel(worktree_root, claims_rel) if claims_rel else None
    pins = rr.plan_pins(split.fm_text) if split else {}
    if target is None:
        return own, ids, True, pins
    return target, ids, target == own, pins


def run_started_at(worktree_root: Path, run_id: Optional[str]) -> Optional[float]:
    """Epoch seconds the run was submitted (``submitted_at`` of its ``submission.json``), else None."""
    if not run_id or not RESOLUTION_RUN_ID.match(run_id):
        return None
    from coordinator_core.ops.warp_run_deliverable import SUBMISSION_FILE

    text = read_rel(worktree_root, f"{RUN_DIR_ROOT}/{run_id}/{SUBMISSION_FILE}")
    if text is None:
        return None
    try:
        at = json.loads(text).get("submitted_at")
    except (ValueError, AttributeError):
        return None
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else None


def withheld_register_rows(worktree_root: Path, plan_path: Optional[str], criterion: dict) -> frozenset:
    """Register row ids the judge carried open in ``criterion.withheld_clauses`` and the plan confirms:
    each is traced (``register_rows``) by a plan row the clause names that is not coded and still
    holds an uncleared ``external_gate`` or is ``needs_slot``. A row the plan does not confirm stays owed."""
    clauses = criterion.get("withheld_clauses")
    text = read_rel(worktree_root, plan_path) if plan_path and isinstance(clauses, list) and clauses else None
    if text is None:
        return frozenset()
    from coordinator_core.ops.plan_tasks_render import load_rows

    named = {
        str(r) for c in clauses if isinstance(c, dict) and isinstance(c.get("rows"), list) for r in c["rows"]
    }
    out: set = set()
    for row in load_rows("\n".join(text.splitlines())).rows:
        if not isinstance(row, dict) or str(row.get("id")) not in named or row.get("disposition") == "coded":
            continue
        gates = row.get("external_gate")
        if row.get("needs_slot") is not True and not any(
            isinstance(g, dict) and g.get("cleared") is not True for g in gates or ()
        ):
            continue
        out.update(str(r) for r in row.get("register_rows") or ())
    return frozenset(out)


def register_gate(
    worktree_root: Path, plan_path: Optional[str], inline_review: dict, ledger_judged: Sequence[str] = (),
    *, run_id: Optional[str] = None,
) -> Optional[str]:
    """Demote a ``met`` criterion to ``not_met`` when a row this plan claims in its
    cited register-bearing sizing is not judged met, wired, on its register surface
    with an observed entry point. Returns the failure reason (also set on the criterion),
    or ``None`` when the gate does not apply or every claimed row passes. Reads the
    judge's returned ``criterion.register_rows`` and the register on disk; never inspects code.
    A claimed row in ``withheld_register_rows`` stays open and never demotes. ``ledger_judged`` names rows the run's passed requirements ledger already judged: they are
    not owed to a plan, and a judge verdict of ``met`` on one needs no wired/observed_ref evidence. Embedded PM turns are owed only when cited or timestamped after the run's
    submission; ``run_id`` (else the sizing's ``resolution.run_id``) locates that submission.
    """
    criterion = inline_review.get("criterion")
    if not plan_path or not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None
    sizing_rel, claim_ids, is_own, pins = register_target(worktree_root, plan_path)
    if sizing_rel is None:
        return None
    from coordinator_core.ops import requirement_register as rr

    sizing_text = read_rel(worktree_root, sizing_rel)
    register = rr.read_register(sizing_text) if sizing_text is not None else None
    gaps: list = []
    if is_own and sizing_text is not None and "pm_verbatims" in sizing_text:
        if not run_id:
            try:
                resolution = rr._load(sizing_text).get("resolution")
                run_id = resolution.get("run_id") if isinstance(resolution, dict) else None
            except Exception:  # noqa: BLE001 -- no run start: only cited turns are owed
                run_id = None
        gaps = rr.uncovered_turns(sizing_text, run_started_at=run_started_at(worktree_root, run_id))
    if register is None and not gaps:
        return None
    claimed = rr.claimed_rows(register, plan_path, claim_ids) if register is not None else []
    drift = rr.pin_drift(register, pins) if register is not None and pins else []
    if drift:
        reason = rr.pin_drift_reason(drift)
        criterion["status"] = "not_met"
        criterion["reason"] = reason
        criterion["observation"] = reason
        return reason
    judged = {
        str(r.get(rr.ROW_ID)): r
        for r in (criterion.get(rr.JUDGE_ROWS_KEY) or [])
        if isinstance(r, dict) and r.get(rr.JUDGE_CLAIM, rr.CLAIM_THIS_PLAN) == rr.CLAIM_THIS_PLAN
    }
    ledger_ids = {str(i) for i in ledger_judged}
    withheld_open = withheld_register_rows(worktree_root, plan_path, criterion)
    ruled_out: frozenset = frozenset()
    if register is not None:
        from coordinator_core.ops.dispatch_emit import requirements_ledger

        ruled_out = frozenset(requirements_ledger.ruled_out_ids(register.rows))
    for row in claimed:
        rid, surface = row.get(rr.ROW_ID), row.get(rr.ROW_SURFACE)
        if str(rid) in ruled_out or str(rid) in withheld_open:
            continue
        got = judged.get(str(rid))
        if str(rid) in ledger_ids and (got is None or got.get(rr.ROW_STATUS) == rr.STATUS_MET):
            continue
        # Trap: admits a self-declared met row the judge never saw; the refusal for a row short of
        # met, wired and attributed is pinned by test_claimed_row_not_fully_met_on_disk_still_reports_not_judged.
        if (
            got is None
            and row.get(rr.ROW_STATUS) == rr.STATUS_MET
            and row.get(rr.ROW_WIRED) is True
            and any(isinstance(m, str) and m.strip() for m in (row.get(rr.ROW_MET_BY) or []))
        ):
            continue
        if got is None:
            why = "not judged"
        elif got.get(rr.ROW_STATUS) != rr.STATUS_MET:
            why = f"judged status {got.get(rr.ROW_STATUS)!r}, not met"
        elif got.get(rr.ROW_WIRED) is not True:
            why = "judged wired: false"
        elif not str(got.get(rr.JUDGE_OBSERVED_REF) or "").strip():
            why = "no observed entry point (observed_ref is empty)"
        else:
            if got.get(rr.ROW_SURFACE) != surface:
                note = (
                    f"register row {rid}: judged surface {got.get(rr.ROW_SURFACE)!r} "
                    f"differs from register surface {surface!r} (met and wired; reported, not refused)"
                )
                prior = criterion.get("observation") or ""
                if note not in prior:
                    criterion["observation"] = f"{prior}\n{note}".strip()
            continue
        reason = f"register row {rid} (surface {surface}): {why}"
        criterion["status"] = "not_met"
        criterion["reason"] = reason
        criterion["observation"] = reason
        return reason
    if gaps:
        turn, head = gaps[0]
        reason = f"PM turn {turn} is covered by no register row or unrelated_turns entry: {head}"
        criterion["status"] = "not_met"
        criterion["reason"] = reason
        criterion["observation"] = reason
        return reason
    if is_own and register is not None:
        orphan = rr.blocking_unclaimed(register, [r.get(rr.ROW_ID) for r in claimed] + list(ledger_judged))
        if orphan is not None:
            reason = f"register row {orphan}: owed and claimed by no plan"
            criterion["status"] = "not_met"
            criterion["reason"] = reason
            criterion["observation"] = reason
            return reason
    return None


def register_writeback(
    worktree_root: Path, plan_path: Optional[str], inline_review: Optional[dict], sha: str,
) -> dict:
    """Cascade the judge's per-row verdicts into the cited sizing's register and recompute its
    rollup, in place; the caller adds ``sizing`` to the coded-stamp commit's paths.

    Returns ``{}`` when nothing applies, ``{"register_sizing": rel}`` on a write, or
    ``{"register_writeback_error": reason}`` when the result fails the sizing schema (the file is
    left untouched). Never raises: the run's own commit has already landed.
    """
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not plan_path or not isinstance(criterion, dict):
        return {}
    from coordinator_core.ops import requirement_register as rr

    judged = [
        r for r in (criterion.get(rr.JUDGE_ROWS_KEY) or [])
        if isinstance(r, dict) and r.get(rr.JUDGE_CLAIM, rr.CLAIM_THIS_PLAN) == rr.CLAIM_THIS_PLAN
    ]
    sizing_rel, claim_ids, _, _ = (
        register_target(worktree_root, plan_path) if judged else (None, frozenset(), True, {})
    )
    if sizing_rel is None:
        return {}
    from datetime import datetime, timezone

    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    now = datetime.now(timezone.utc)

    def mutate(old: str) -> str:
        if rr.read_register(old) is None:
            raise MutateAbort("")
        new = rr.apply_verdicts(old, judged, plan_path, sha, now, claim_ids)
        if new == old:
            raise MutateAbort("")
        try:
            errors = validate_frontmatter(yaml_load(new) or {}, schema_path)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            raise MutateAbort(f"register write-back refused: {exc}")
        if errors:
            raise MutateAbort(f"register write-back refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(worktree_root / sizing_rel, mutate, repo_root=worktree_root)
    except MutateAbort as exc:
        reason = str(exc.args[0]) if exc.args else ""
        return {"register_writeback_error": reason} if reason else {}
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
        return {"register_writeback_error": f"register write-back refused: {exc!r}"}
    return {"register_sizing": sizing_rel}
