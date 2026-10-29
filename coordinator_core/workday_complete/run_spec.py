"""Workday-complete's run spec: the ceremony as data for the shared ceremony driver.

The step table is the brief's `directives[]`, converted by `ceremony_common.run.envelope`; this
module adds the front door, the envelope step, the residue the DoE body runs outside the brief,
the rules for the brief's judgment points, the agent groups, the steps a decision opens, and the
receipt sections. `front_door(flags)` ends with the envelope step (`envelope_step` names it) and
`residue(flags)` is flag-dependent, so both are callables over the launch flags; the driver adds
`repo_root` and `today` to the mapping it hands them. Nothing here edits `brief.py` or calls
`apply()`.
"""
from __future__ import annotations

import functools
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.cadence.period_mode import CATCH_UP_FLAG, CONCLUDE_FLAG, PERIOD_MODE_KEY
from coordinator_core.ceremony_common.run import decide
from coordinator_core.ceremony_common.run.contract import (
    RUN_DIR_SUFFIX,
    Decision,
    StepSpec,
    step_from_directive,
)
from coordinator_core.ceremony_common.run.receipt import as_row
from coordinator_core.ceremony_common.run.spec import CeremonySpec

NAME = "workday-complete"
DISPATCH_KEY = "workday_complete_run"
TEMPLATE = "workflows/workday-start.mjs"
STEP_COSTS_PATH = Path(__file__).resolve().parent / "step_costs.json"

STAGE_FRONT_DOOR = "front-door"
STAGE_ENVELOPE = "envelope"
STAGE_LAUNCH = "launch"
STAGE_FINISH = "finish"
# Not a stage the driver runs: the envelope's `d_goal_close_day` carries `--decisions {}`, so
# `finish_steps` builds the one that runs.
STAGE_FINISH_BUILT = "finish-built"

GRANT_STEP_ID = "tier_u_grant"
ENVELOPE_STEP_ID = "assemble_brief"
NOT_COMPOSED_TARGET = "not-composed"
ADOPT_COMMIT_TARGET = "coordinator_core.ops.workday_complete_step2_5_dirty_tree:adopt_ambiguous"

# Typed by hand; the `workday_complete_run` row of `authz.dispatchable` is pinned equal to it.
CONSUMES_MANIFEST: frozenset[str] = frozenset({
    "workday-complete-args-and-validate",
    "workday-complete-reconcile",
    "workday-complete-step2_5-dirty-tree",
    "handoff-housekeeping",
    "workday-complete-step3-consolidate",
    "workday-complete-backfill-scan",
    "workday-complete-backfill-anchor",
    "workday-complete-close",
    "standup",
    "query-completions",
    "coordinator-queue-append",
    "prune-closed-bugs",
    "workday-start-advisory-counters",
    "check-weekly-staleness",
    "goal-close-day",
    "coordinator-ceremony-hook",
    "tier-u-grant-cli",
    "workday-complete-assemble",
    "structural-index-refresh",
    "day-coverage-sweep",
    "baton-drift-sweep",
    "check-auto-memory-drained",
    "coordinator-invoke",
})

DIRTY_TREE_POINT = "jp_step2_5_dirty_tree_ambiguous"
CLOSEOUT_POINT = "jp_day_goal_closeout"
BACKFILL_CAP_POINT = "jp_step3_5_backfill_cap"
ANALYST_POINT = "jp_step4b_analyst_dispatch"
CLUSTERING_POINT = "jp_step4_5_clustering_dispatch"
HEALTH_LEDGER_POINT = "jp_step4e_health_ledger_new_rows"

RULE_ONLY_MODE = "only-mode"
RULE_BACKFILL_CAP = "backfill-cap"
RULE_REFUSE = "refuse-decision"

# Directives ordered at or after `d_step3_consolidate` that commit, push or delete, so they stay
# behind it; the read-only ones after it run at launch because the agent groups read their output.
_FINISH_DIRECTIVES = frozenset({
    "d_step3_consolidate",
    "d_step3_5_backfill_anchor_a0",
    "d_step3_5_backfill_phase_b",
    "d_step7_5_prune_closed_bugs",
    "d_step9_changelog",
    "d_step10_5_post_command_hook",
})
_LAUNCH_DIRECTIVES = frozenset({
    "d_step1_validate",
    "d_step1_5_cruft_sweep",
    "d_step2_5_dirty_tree_scan",
    "d_step2_67_handoff_housekeeping",
    "d_step3_5_backfill_scan",
    "d_step4a_inventory",
    "d_step6_archive_audit_completions",
    "d_step8_improvement_queue_nudge",
    "d_step10_weekly_staleness",
})

_HEADINGS: Mapping[str, str] = {
    "d_step1_validate": "Validate",
    "d_step3_consolidate": "Branch state",
    "d_goal_close_day": "Day-goal closeout",
    "d_step10_weekly_staleness": "Weekly staleness",
    "d_step10_5_post_command_hook": "Post-ceremony hook",
    "day_coverage_sweep": "Commit coverage",
    "baton_drift_sweep": "Baton drift",
}
_OBSERVATIONS = "Observations"

_GAP_REASONS: Mapping[str, str] = {
    "rag_staleness_nudge": "reads tool names only a live session can see",
    "node_plugin_ecosystem_suite": "needs a Node runtime this repo's own work does not use",
    "emitted_workflow_sweep": "a /distill skill step with no CLI of its own",
    "claudemeta_cadence": (
        "state/reference/generate-claudemeta-manifest.py is absent in this repo and is not a "
        "coordinator/bin bareword, so no admitted launcher starts it"
    ),
}

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

JUDGMENT_BRIEFS: Mapping[str, str] = {
    DIRTY_TREE_POINT: (
        "Attribute a path to another session only when the claim index shows that session "
        "holding it live (liveness=dead is an orphan, not an owner). Everything else is "
        "adopt_commit."
    ),
    CLOSEOUT_POINT: (
        "Answer record with payload {goal_id: 'done'|'lapsed'|'dropped'} covering every open goal "
        "row the evidence names, or skip."
    ),
}


def _flag(flags: Mapping[str, Any] | None, name: str, default: Any = None) -> Any:
    for key in (f"--{name}", name, name.replace("-", "_")):
        if flags and key in flags and flags[key] not in (None, False, ""):
            return flags[key]
    return default


def _today(flags: Mapping[str, Any] | None) -> str:
    return str(_flag(flags, "today", date.today().isoformat()))


def _repo_root(flags: Mapping[str, Any] | None) -> str:
    return str(_flag(flags, "repo-root", "."))


def _day(flags: Mapping[str, Any] | None) -> str:
    return str(_flag(flags, "for-date", _today(flags)))


def _only(flags: Mapping[str, Any] | None) -> bool:
    return bool(_flag(flags, "only", False))


def _scope_summary(flags: Mapping[str, Any] | None) -> str | None:
    value = _flag(flags, "scope-summary") or _flag(flags, "summary")
    return str(value) if value else None


@functools.cache
def _cost_rows() -> Mapping[str, Mapping[str, Any]]:
    return json.loads(STEP_COSTS_PATH.read_text(encoding="utf-8"))["steps"]


def cost_classes() -> dict[str, str]:
    """`{step id: cost_class}` from `step_costs.json`; an id with no row is not in the mapping."""
    return {sid: row["cost_class"] for sid, row in _cost_rows().items()}


COSTS: Mapping[str, str] = cost_classes()


def _step(
    id_: str,
    target: str,
    args: Sequence[str],
    *,
    kind: str = "cli",
    stage: str = STAGE_LAUNCH,
    detail: str = "",
    cost_key: str | None = None,
    cost_class: str | None = None,
    gates_next: bool = False,
    best_effort: bool = False,
    on_exit: Mapping[int, str] | None = None,
) -> StepSpec:
    record: dict[str, Any] = dict(
        id=id_, cli=target, args=list(args), depends_on=None,
        already_satisfied=False, detail=detail, best_effort=best_effort,
    )
    return step_from_directive(
        record,
        stage=stage,
        heading=_HEADINGS.get(id_, _OBSERVATIONS),
        cost_class=cost_class or COSTS.get(cost_key or id_, "unmeasured"),
        kind=kind,
        gates_next=gates_next,
        on_exit=on_exit,
    )


def _not_composed(id_: str) -> StepSpec:
    reason = _GAP_REASONS[id_]
    return step_from_directive(
        dict(id=id_, cli=NOT_COMPOSED_TARGET, args=[], detail=f"not-composed: {reason}"),
        stage=STAGE_LAUNCH,
        heading=_OBSERVATIONS,
        cost_class="over-bar",
        kind="inproc",
    )


def _arguments(flags: Mapping[str, Any] | None) -> str:
    parts: list[str] = []
    if _flag(flags, "for-date"):
        parts += ["--for-date", str(_flag(flags, "for-date"))]
    if _only(flags):
        parts.append("--only")
    summary = _flag(flags, "summary")
    if summary:
        parts.append(str(summary))
    return " ".join(parts)


def front_door(flags: Mapping[str, Any] | None = None) -> tuple[StepSpec, ...]:
    """The grant, the cross-machine guard, then the envelope step.

    The guard gates: a non-zero exit fails the run with the mismatch as its reason. The grant is
    best-effort and is the step the runner checks before a `suite` step.
    """
    brief_args = ["brief"]
    if _flag(flags, "for-date"):
        brief_args += ["--for-date", str(_flag(flags, "for-date"))]
    if _only(flags):
        brief_args.append("--only")
    scope = _scope_summary(flags)
    if scope:
        brief_args.append(f"--scope-summary={scope}")
    for flag in (CATCH_UP_FLAG, CONCLUDE_FLAG):
        if _flag(flags, flag.removeprefix("--")):
            brief_args.append(flag)
    return (
        _step(
            GRANT_STEP_ID, "tier-u-grant-cli",
            ["grant", "ceremony", "workday-complete Tier-U consumers", "--ceremony", NAME],
            stage=STAGE_FRONT_DOOR, best_effort=True,
            detail="Step 0.9-1: the ceremony-scoped Tier-U grant.",
        ),
        _step(
            "check_cross_machine", "workday-complete-args-and-validate",
            ["check-cross-machine", _arguments(flags)],
            stage=STAGE_FRONT_DOOR, gates_next=True,
            detail="Step 0.9-1: a cross-machine --for-date fails loud.",
        ),
        _step(
            ENVELOPE_STEP_ID, "workday-complete-assemble", brief_args,
            stage=STAGE_ENVELOPE, gates_next=True,
            detail="Step 2: the brief envelope.",
        ),
    )


def residue(flags: Mapping[str, Any] | None = None) -> tuple[StepSpec, ...]:
    """The DoE body's steps the brief does not emit, in body order; `--only` drops the sweeps."""
    repo_root = _repo_root(flags)
    steps: list[StepSpec] = [
        _step(
            "register_stall_report", "requirement_register.stall_report",
            [json.dumps({"today": _today(flags)})], kind="op",
            detail="The register stall check.",
        ),
        _step(
            "structural_index_refresh", "structural-index-refresh", ["--flag-only"], best_effort=True,
            detail="Step 4b: the structural index staleness flag; never spawns a refresh.",
        ),
    ]
    if not _only(flags):
        steps += [
            _step("day_coverage_sweep", "day-coverage-sweep", [_day(flags)],
                  detail="Step 9b: commit coverage for the day."),
            _step("baton_drift_sweep", "baton-drift-sweep", [],
                  detail="Step 9c: baton drift."),
        ]
    steps += [
        _step(
            "check_auto_memory_drained", "check-auto-memory-drained", ["--root", repo_root],
            best_effort=True,
            detail="Step 9c: exit 1 lists residual paths for the memory-drain group.",
        ),
        _step(
            "fleet_scratch_hygiene", "fleet.scratch_hygiene",
            [json.dumps({"repo_root": repo_root, "cadence": "workday_complete", "apply": False})],
            kind="op", best_effort=True, detail="Step 8a: scratch purge, dry run.",
        ),
        _step(
            "git_maintenance_daily", "git.maintenance",
            [json.dumps({"tier": "daily", "repo": repo_root})],
            kind="op", best_effort=True, detail="Step 8b: maintenance checkpoint, daily tier.",
        ),
    ]
    steps += [_not_composed(i) for i in _GAP_REASONS]
    return tuple(steps)


def stage_of(directive_id: str) -> str:
    """The stage of a brief directive; raises ValueError on an id the brief did not emit before."""
    if directive_id == "d_goal_close_day":
        return STAGE_FINISH_BUILT
    if directive_id in _FINISH_DIRECTIVES:
        return STAGE_FINISH
    if directive_id in _LAUNCH_DIRECTIVES:
        return STAGE_LAUNCH
    raise ValueError(f"workday-complete: unknown directive id {directive_id!r}")


def heading_of(directive_id: str) -> str:
    return _HEADINGS.get(directive_id, _OBSERVATIONS)


def _declared(point: Mapping[str, Any]) -> set[str]:
    return {d.get("value") for d in (point.get("dispositions") or [])}


def _rule_decision(point: Mapping[str, Any], rule: str, value: str, rationale: str) -> Decision:
    return Decision(
        point_id=str(point.get("id", "")),
        question=str(point.get("question", "")),
        decision=value,
        decided_by="rule",
        rule=rule,
        rationale=rationale,
        evidence=str(point.get("evidence", "")),
    )


def only_mode(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    """Under `--only` the clustering dispatch is skipped; otherwise the point is not claimed."""
    if point.get("id") != CLUSTERING_POINT or not _only(context.get("flags")):
        return None
    if "skip_only_mode" not in _declared(point):
        return None
    return _rule_decision(
        point, RULE_ONLY_MODE, "skip_only_mode",
        "A targeted wrap skips completion-log clustering, as the DoE body's Step 8 states.",
    )


def backfill_cap(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    """Decide `backfill_all`: `d_step3_5_backfill_phase_b` takes no bound, so none can be applied."""
    if point.get("id") != BACKFILL_CAP_POINT or "backfill_all" not in _declared(point):
        return None
    return _rule_decision(
        point, RULE_BACKFILL_CAP, "backfill_all",
        "backfill-dispatch-rows has no count flag and processes every row of the scan TSV on "
        "stdin; the scan's lookback window bounds the fan-out and nothing else does, so "
        "bounded_subset cannot be carried out.",
    )


def refuse_decision(step: StepSpec, result: Any, *, repo_root: Any, run_dir: Any) -> str:
    """Step rule: a refusal step's failure reason is the reason it was built with."""
    return step.args[0]


def refuse(repo_root: Any) -> tuple[int, str]:
    """Inproc target of a refusal step: always fails; the step rule supplies the reason."""
    return 1, "refused"


RULES: dict[str, Callable[..., Any]] = {
    RULE_ONLY_MODE: only_mode,
    RULE_BACKFILL_CAP: backfill_cap,
    decide.RECOMMENDATION: decide.recommendation,
    RULE_REFUSE: refuse_decision,
}

RULE_TABLE = decide.RuleTable(
    point_rules=(
        (RULE_ONLY_MODE, only_mode),
        (RULE_BACKFILL_CAP, backfill_cap),
        (decide.RECOMMENDATION, decide.recommendation),
    ),
    exhausted_rule="no-live-disposition",
)


def spec_points(
    envelope: Mapping[str, Any], flags: Mapping[str, Any] | None, ledger_rows: Sequence[Any]
) -> list[dict[str, Any]]:
    """Workday-complete makes no judgment point of its own."""
    return []


def _decision_for(resolution: Any, point_id: str) -> str | None:
    for d in getattr(resolution, "decisions", None) or ():
        if d.point_id == point_id:
            return d.decision
    return None


def _row(ledger_rows: Sequence[Any], step_id: str) -> Any:
    for r in ledger_rows:
        row = as_row(r)
        if row.id == step_id:
            return row
    return None


def _directive(envelope: Mapping[str, Any], directive_id: str) -> Mapping[str, Any]:
    for d in envelope.get("directives") or ():
        if d.get("id") == directive_id:
            return d
    return {}


def _envelope_scope(envelope: Mapping[str, Any]) -> tuple[str, bool]:
    """`(target day, only mode)` as the brief baked them into its directives."""
    args = list(_directive(envelope, "d_step3_5_backfill_phase_b").get("args") or ())
    day = args[args.index("--for-date") + 1] if "--for-date" in args else date.today().isoformat()
    only = "--only-mode" in (_directive(envelope, "d_step9_changelog").get("args") or ())
    return day, only


def _catch_up(envelope: Mapping[str, Any]) -> bool:
    """True when the brief resolved `catch_up`: the in-progress day is left open, so not audited."""
    return (envelope.get(PERIOD_MODE_KEY) or {}).get("mode") == "catch_up"


def _gap_rows(scan_stdout: str) -> list[tuple[str, str, str, str]]:
    rows = []
    for line in scan_stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 4 and _DAY_RE.match(fields[0]):
            rows.append((fields[0], fields[1], fields[2], fields[3]))
    return sorted(rows)


def _chains(completions_stdout: str, day: str) -> list[dict[str, Any]] | None:
    """Chains of `day` with two or more entries and no narrative; None when the output is not JSON."""
    try:
        entries = json.loads(completions_stdout)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    by_chain: dict[str, list[Mapping[str, Any]]] = {}
    for e in entries:
        fm = (e or {}).get("frontmatter") or {}
        if str(fm.get("created", "")) == day and fm.get("chain"):
            by_chain.setdefault(str(fm["chain"]), []).append({"path": e.get("path"), **fm})
    return [
        {"chain": chain, "paths": [m["path"] for m in members]}
        for chain, members in sorted(by_chain.items())
        if len(members) >= 2 and not any(m.get("narrative") for m in members)
    ]


def _group(group_id: str, phase: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": group_id, "phase": phase, "agent": "coordinator:executor",
        "ruling_agent": decide.DEFAULT_AGENT, "inputs": {}, "entries": entries,
    }


def agent_groups(
    envelope: Mapping[str, Any], ledger_rows: Sequence[Any], resolution: Any
) -> list[dict[str, Any]]:
    """The digest groups of the run, built from ledger rows, the envelope and the resolution only."""
    day, only = _envelope_scope(envelope)
    scope = None
    changelog_args = list(_directive(envelope, "d_step9_changelog").get("args") or ())
    if "--" in changelog_args:
        scope = changelog_args[changelog_args.index("--") + 1]
    groups: list[dict[str, Any]] = []

    if _decision_for(resolution, ANALYST_POINT) == "dispatch":
        groups.append(_group("daily-summary", 1, [{
            "key": f"day:{day}",
            "brief": (
                f"Write archive/daily-summaries/{day}-<machine>.md for {day}. Report "
                "skipped-no-new-work when the day has no new commits and no agent-driven change."
            ),
            "argv": None,
            "inputs": {"day": day, "only_mode": only, "scope_summary": scope},
        }]))

    scan = _row(ledger_rows, "d_step3_5_backfill_scan")
    if scan is not None and scan.status == "ran":
        gaps = _gap_rows(scan.stdout)
        if gaps:
            groups.append(_group("backfill-summary", 2, [
                {"key": f"day:{d}", "brief": f"Write the daily summary for {d}.", "argv": None,
                 "inputs": {"day": d, "commit_count": count, "base": base, "tip": tip}}
                for d, count, base, tip in gaps
            ]))

    if _decision_for(resolution, CLUSTERING_POINT) == "dispatch_if_multi_entry_chains" and not only:
        completions = _row(ledger_rows, "d_step6_archive_audit_completions")
        chains = _chains(completions.stdout, day) if completions and completions.status == "ran" else None
        brief = (
            "Write a `narrative:` synthesis for the chain: at most 300 words, SHAs preserved, "
            "no editorial bucketing."
        )
        if chains is None:
            entries = [{
                "key": f"chains:{day}",
                "brief": f"Run query-completions --where created={day} --format json, group by "
                         f"chain, and for each chain of two or more entries lacking a narrative: {brief}",
                "argv": None, "inputs": {"day": day},
            }]
        else:
            entries = [
                {"key": f"chain:{c['chain']}", "brief": brief, "argv": None, "inputs": c} for c in chains
            ]
        if entries:
            groups.append(_group("clustering", 1, entries))

    if _decision_for(resolution, HEALTH_LEDGER_POINT) == "add_unaudited_rows":
        groups.append(_group("health-ledger", 1, [{
            "key": "health-ledger",
            "brief": (
                "Add a `?` row to state/health-ledger.md for each system touched today with "
                "no row; never touch audit clocks or existing grades. Remove "
                "tasks/daily-review-scratch."
            ),
            "argv": None, "inputs": {"day": day},
        }]))

    if not only and not _catch_up(envelope):
        groups.append(_group("archive-audit", 2, [{
            "key": f"archive-audit:{day}",
            "brief": (
                "Add or fix completed-archive entries from the day's commits and the receipts; "
                "put an uncovered receipt into a meta-entry. Answer with the outcome line "
                "'Archive audit: N verified, M added, K corrected; R receipts, U without a "
                "covering entry.'"
            ),
            "argv": None,
            "inputs": {
                "day": day,
                "receipts": (envelope.get("gates") or {}).get("completion_receipts"),
                "completions_step": "d_step6_archive_audit_completions",
            },
        }]))

    drain = _row(ledger_rows, "check_auto_memory_drained")
    if drain is not None and drain.exit_code == 1:
        groups.append(_group("memory-drain", 3, [{
            "key": "memory-drain",
            "brief": (
                "Promote each residual path to a durable home in your own voice, or drop it; "
                "delete it either way."
            ),
            "argv": None, "inputs": {"residual": drain.stdout.splitlines()},
        }]))
    return groups


def _refusal(id_: str, reason: str) -> StepSpec:
    return _step(
        id_, f"{__name__}:refuse", [reason], kind="inproc", stage=STAGE_FINISH,
        gates_next=True, on_exit={1: RULE_REFUSE}, detail=reason,
    )


_CLOSEOUT_DECISIONS = ("done", "lapsed", "dropped")


def _valid_closeout(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and bool(payload)
        and all(isinstance(k, str) and v in _CLOSEOUT_DECISIONS for k, v in payload.items())
    )


def _capabilities(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        found = value.get("capabilities")
        if isinstance(found, list):
            return [str(c) for c in found]
        return [c for v in value.values() for c in _capabilities(v)]
    if isinstance(value, list):
        return [c for v in value for c in _capabilities(v)]
    return []


def _run_value(run: Any, key: str, default: str) -> str:
    value = run.get(key) if isinstance(run, Mapping) else getattr(run, key, None)
    return str(value) if value else default


def finish_steps(
    decisions: Sequence[Decision], ledger_rows: Sequence[Any], *, run: Any
) -> list[StepSpec]:
    """The steps the decisions and the launch rows open, refusals first.

    `adopt_commit` opens a gating commit step for the ambiguous paths; `attribute_to_session`
    has no step that carries it, so it yields a gating refusal. Either runs ahead of the
    envelope's ready directives, so a failure halts `d_step3_consolidate`.
    """
    steps: list[StepSpec] = []
    by_point = {d.point_id: d for d in decisions}

    dirty = by_point.get(DIRTY_TREE_POINT)
    if dirty is not None:
        if dirty.decision == "adopt_commit":
            steps.append(_step(
                "adopt_dirty_tree", ADOPT_COMMIT_TARGET, [], kind="inproc", stage=STAGE_FINISH,
                gates_next=True, detail="Step 2.5: commit the ambiguous paths (adopt_commit).",
            ))
        else:
            steps.append(_refusal("refuse_dirty_tree", f"no-step-carries:{dirty.decision}"))

    closeout = by_point.get(CLOSEOUT_POINT)
    if closeout is not None and closeout.decision == "record":
        if _valid_closeout(closeout.payload):
            steps.append(_step(
                "d_goal_close_day", "goal-close-day",
                ["--decisions", json.dumps(dict(closeout.payload), sort_keys=True)],
                stage=STAGE_FINISH, detail="Day-goal closeout from the agent's payload map.",
            ))
        else:
            steps.append(_refusal("refuse_goal_closeout", f"payload-invalid:{CLOSEOUT_POINT}"))

    drain = _row(ledger_rows, "check_auto_memory_drained")
    if drain is not None and drain.exit_code == 1:
        steps.append(_step(
            "check_auto_memory_drained_recheck", "check-auto-memory-drained",
            ["--root", _run_value(run, "repo_root", ".")],
            stage=STAGE_FINISH, cost_key="check_auto_memory_drained",
            detail="Step 9c: the re-run after the memory-drain group.",
        ))

    scratch = _row(ledger_rows, "fleet_scratch_hygiene")
    if scratch is not None and scratch.status == "ran":
        try:
            capable = "hold_pending_marker" in _capabilities(json.loads(scratch.stdout))
        except ValueError:
            capable = False
        if capable:
            steps.append(_step(
                "fleet_scratch_hygiene_apply", "fleet.scratch_hygiene",
                [json.dumps({"repo_root": _run_value(run, "repo_root", "."),
                             "cadence": "workday_complete", "apply": True})],
                kind="op", stage=STAGE_FINISH, best_effort=True,
                detail="Step 8a: scratch purge, apply.",
            ))

    baton = _row(ledger_rows, "baton_drift_sweep")
    if baton is not None and baton.status == "ran":
        found = re.search(r"^stranded=(\d+)", baton.stdout, re.MULTILINE)
        if found and int(found.group(1)) > 0:
            steps.append(_refusal("baton_drift_stranded", f"stranded={found.group(1)}"))
    return steps


def _stdout(ledger_rows: Sequence[Any], step_id: str) -> str:
    row = _row(ledger_rows, step_id)
    return row.stdout if row is not None and row.status == "ran" else ""


def receipt_facts(
    ledger_rows: Sequence[Any],
    flags: Mapping[str, Any] | None = None,
    agent_results: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The facts `receipt_sections` conditions read; a key is falsy when its line is not owed."""
    def failed(step_id: str) -> bool:
        row = _row(ledger_rows, step_id)
        return row is not None and row.status == "failed"

    def count(step_id: str, key: str) -> int:
        found = re.search(rf"^{key}=(\d+)", _stdout(ledger_rows, step_id), re.MULTILINE)
        return int(found.group(1)) if found else 0

    audit = ((agent_results or {}).get("groups") or {}).get("archive-audit") or []
    drain = _row(ledger_rows, "check_auto_memory_drained")
    swept = not _only(flags)
    return {
        "day_summary": ("; ".join(p for p in (
            f"targeted wrap of {_flag(flags, 'for-date')}" if _only(flags) else "",
            _scope_summary(flags) or "",
        ) if p)),
        "goal_closeout": bool(_stdout(ledger_rows, "d_goal_close_day").strip()),
        "validate_failed": failed("d_step1_validate"),
        "archive_audit": "\n".join(
            str(r.get("outcome", "")) for r in audit if isinstance(r, Mapping) and r.get("outcome")
        ),
        "coverage_gaps": swept and count("day_coverage_sweep", "orphaned") > 0,
        "baton_stranded": swept and count("baton_drift_sweep", "stranded") > 0,
        "memory_residue": drain.stdout.strip() if drain is not None and drain.exit_code == 1 else "",
        "weekly_stale": bool(re.search(r"\b(STALE|MILD)\b", _stdout(ledger_rows, "d_step10_weekly_staleness"))),
        "hook_line": bool(_stdout(ledger_rows, "d_step10_5_post_command_hook").strip()),
    }


# (heading, condition, source): a section appears only when its condition holds.
RECEIPT_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("Branch state", "always", "step:d_step3_consolidate"),
    ("Day summary", "day_summary", "fact:day_summary"),
    ("Day-goal closeout", "goal_closeout", "step:d_goal_close_day"),
    ("Validate", "validate_failed", "step:d_step1_validate"),
    ("Archive audit", "archive_audit", "fact:archive_audit"),
    ("Commit coverage", "coverage_gaps", "step:day_coverage_sweep"),
    ("Baton drift", "baton_stranded", "step:baton_drift_sweep"),
    ("Auto-memory residue", "memory_residue", "fact:memory_residue"),
    ("Weekly staleness", "weekly_stale", "step:d_step10_weekly_staleness"),
    ("Post-ceremony hook", "hook_line", "step:d_step10_5_post_command_hook"),
)


SPEC = CeremonySpec(
    name=NAME,
    dispatch_key=DISPATCH_KEY,
    run_dir_name=NAME + RUN_DIR_SUFFIX,
    template=TEMPLATE,
    marker_path=None,
    launch_stages=(STAGE_FRONT_DOOR, STAGE_ENVELOPE, STAGE_LAUNCH),
    finish_stages=(STAGE_FINISH,),
    front_door=front_door,
    envelope_step=ENVELOPE_STEP_ID,
    stage_of=stage_of,
    residue=residue,
    rules=RULES,
    spec_points=spec_points,
    agent_groups=agent_groups,
    finish_steps=finish_steps,
    receipt_sections=RECEIPT_SECTIONS,
    render=None,
    costs=COSTS,
    chain=None,
    launch_flags=frozenset(
        {"--for-date", "--only", "--scope-summary", "--summary", CATCH_UP_FLAG, CONCLUDE_FLAG}
    ),
)
