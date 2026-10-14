"""Workday-start's CeremonySpec: the ceremony as data for the shared driver.

Everything here is what the driver cannot know: the manifest's stage-ordered steps and their
cadence gates, the rule table, the three agent groups, the post-briefing tail, and the two
per-step checks (the inbox assembler's output, the backfill plan).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.contract import apply_base
from coordinator_core.ceremony_common.run.contract import DEADLINE_S, StepSpec
from coordinator_core.ceremony_common.run.driver import PRE_DIRECTIVE_STAGE, RunHooks
from coordinator_core.ceremony_common.run.runner import HELD_PREFIX, ChildOutcome, run_bin_cold
from coordinator_core.ceremony_common.run.spec import CeremonySpec
from coordinator_core.git import reconcile_manifest
from coordinator_core.workday_start import backfill, briefing, manifest, rules
from coordinator_core.workday_start.contract import FINISH_STAGES, LAUNCH_STAGES

CEREMONY = "workday-start"
DISPATCH_KEY = "workday_start"
TEMPLATE_RELPATH = "workflows/workday-start.mjs"

STEP0_STEP = "step0-branch-setup"
ORIENT_STEP = "orient-assemble-brief-day"
INBOX_STEP = "memo-inbox-blitz-assemble"
FRESHNESS_STEP = "fresh-commit-delta"
BACKFILL_SCAN_STEP = "weekly-backfill-scan"
ALIGNMENT_STEPS = ("priority-whats-next", "triage-query-completions")

PHASE_ONE_LAST_STAGE = "freshness"
POST_BRIEFING_STAGES = FINISH_STAGES[FINISH_STAGES.index("briefing") + 1 :]
DIRECTIVE_STAGE = "decisions"

CONCLUDE_STEP = "reconcile-conclude"
CONCLUDE_TARGET = "coordinator_core.workday_start.run_spec:conclude_open_merge"
RESOLVER_BRIEF = (
    "Step 0 left a merge of origin/main open on this checkout. Resolve every file listed in "
    "`inputs.paths`: edit it with Edit or Write until no conflict marker (<<<<<<<, =======, >>>>>>>) "
    "remains, keeping the intent of both sides. Use file edits only. Run no git command of any kind "
    "(no merge, add, commit, checkout, restore, reset or stash); the guards deny one, and the run "
    "concludes the merge itself at finish. Report each file as resolved, or name the file you could not resolve."
)


@dataclass(frozen=True)
class WorkdayStartSpec(CeremonySpec):
    """`CeremonySpec` plus the driver's optional `hooks`."""

    hooks: RunHooks = field(default_factory=RunHooks)


def marker_path(repo_root: Path) -> Path:
    from coordinator_core.ops.check_weekly_staleness import _resolve_state_root

    root = _resolve_state_root()
    return Path(root if root else Path(repo_root) / "state") / ".workday-start-marker"


def _json_or_none(text: str) -> "dict[str, Any] | None":
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _latest(rows: Sequence[Mapping[str, Any]], step_id: str) -> "Mapping[str, Any] | None":
    return next((r for r in reversed(rows) if r["id"] == step_id), None)


def _raw(row: Mapping[str, Any]) -> str:
    """Unclipped stdout; `raw_stdout` exists only in the launching process, so at finish this is the clipped ledger text."""
    return str(row.get("raw_stdout", row.get("stdout") or ""))


def _inbox(rows: Sequence[Mapping[str, Any]]) -> "dict[str, Any]":
    row = _latest(rows, INBOX_STEP)
    return (_json_or_none(_raw(row)) if row and row["status"] == "ran" else None) or {}


def _reconcile_rule(step: Any, result: Any, *, repo_root: Path, run_dir: Path) -> "bool | str":
    """The reconcile rule with the conflicted paths appended to an unresolved reason."""
    out = rules.step0_reconcile_outcome(step, result, repo_root=repo_root, run_dir=run_dir)
    if isinstance(out, str):
        paths = rules._paths_after_marker(result.stderr or "")
        if paths:
            out = f"{out} paths:{','.join(paths)}"
    return out


def _memo_inbox_rule(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Any:
    return rules.memo_inbox(point, context={"inbox": _inbox(context.get("ledger_rows") or [])})


RULES: dict[str, Callable[..., Any]] = {
    "step0-stale-branch": rules.step0_stale_branch,
    "step0-reconcile-outcome": _reconcile_rule,
    "memo-inbox": _memo_inbox_rule,
    rules.RECOMMENDATION: rules.recommendation,
}


def _stages(stages: Sequence[str]) -> Callable[..., list[Any]]:
    """A segment source: the manifest's steps for `stages`, owed and cadence-skipped."""

    def build(*, repo_root: Path, today: _dt.date, ledger_rows: Sequence[Mapping[str, Any]]) -> list[Any]:
        fresh = _latest(ledger_rows, FRESHNESS_STEP)
        freshness = {"stdout": _raw(fresh)} if fresh else None
        kwargs = dict(repo_root=repo_root, today=today, freshness=freshness)
        return [*manifest.steps_for(stages, **kwargs), *manifest.cadence_skipped(stages, **kwargs)]

    return build


def _after_step(step: Any, outcome: ChildOutcome, *, envelope: "Mapping[str, Any] | None") -> ChildOutcome:
    if outcome.exit_code != 0:
        return outcome
    if step.id == INBOX_STEP:
        inbox = _json_or_none(outcome.stdout)
        if inbox is None:
            return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "assembler-output-not-json")
        points = (envelope or {}).get("judgment_points") or []
        if inbox.get("state") == "skipped" and any(str(p.get("id", "")).startswith("j-memo-") for p in points):
            return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "skipped-with-open-memos")
        dispatches = inbox.get("dispatches") or []
        memos = sum(len(d.get("memos") or []) for d in dispatches)
        return dataclasses.replace(
            outcome, stdout=f"inbox {inbox.get('state', 'unknown')}: {len(dispatches)} dispatch(es), {memos} memo(s)"
        )
    if step.id == BACKFILL_SCAN_STEP:
        _, reason = backfill.plan_with_reason(outcome.stdout, "")
        if reason:
            return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, reason)
    return outcome


def _merge_held(rows: Sequence[Mapping[str, Any]]) -> "Mapping[str, Any] | None":
    """The Step 0 row when it ended holding an open reconcile merge, else None."""
    row = _latest(rows, STEP0_STEP)
    held = row is not None and row["status"] == "ran" and row["reason"] == f"{HELD_PREFIX}{rules.HOLD_REASON}"
    return row if held else None


def agent_groups(
    envelope: Mapping[str, Any], ledger_rows: Sequence[Mapping[str, Any]], resolution: Any
) -> list[dict[str, Any]]:
    """The inbox, backfill and alignment groups; the driver appends the judgment group."""
    groups: list[dict[str, Any]] = []
    held = _merge_held(ledger_rows)
    if held is not None:
        groups.append({
            "id": "reconcile", "phase": 0, "agent": "coordinator:executor", "ruling_agent": None, "inputs": {},
            "entries": [{
                "key": "reconcile", "brief": RESOLVER_BRIEF, "argv": None,
                "inputs": {"paths": rules._paths_after_marker(held.get("stderr") or "")},
            }],
        })
    inbox = _inbox(ledger_rows)
    dispatches = inbox.get("dispatches") or []
    if dispatches:
        entries, seen = [], set()
        for i, d in enumerate(dispatches, start=1):
            key = str(d.get("id") or d.get("bucket") or f"inbox-{i}")
            key = key if key not in seen else f"{key}-{i}"
            seen.add(key)
            entries.append(
                {"key": key, "brief": d.get("brief"), "argv": None,
                 "inputs": {k: v for k, v in d.items() if k != "brief"}}
            )
        groups.append({
            "id": "inbox", "phase": 1, "agent": "coordinator:executor", "ruling_agent": "coordinator:apm",
            "inputs": {"supersession_candidates": inbox.get("supersession_candidates") or []},
            "entries": entries,
        })
    scan = _latest(ledger_rows, BACKFILL_SCAN_STEP)
    if scan is not None and scan["status"] == "ran":
        planned, reason = backfill.plan_with_reason(_raw(scan), "")
        if planned and not reason:
            groups.append({
                "id": "backfill", "phase": 2, "agent": None, "ruling_agent": None, "inputs": {},
                "entries": planned,
            })
    ran = [
        s for s in ALIGNMENT_STEPS
        if (row := _latest(ledger_rows, s)) is not None and row["status"] == "ran" and _raw(row).strip()
    ]
    if ran:
        groups.append({
            "id": "alignment", "phase": 3, "agent": None, "ruling_agent": None, "inputs": {},
            "entries": [{
                "key": "alignment", "brief": None, "argv": None,
                "inputs": {"ledger": str(ledger_rows[0]["ledger_path"]), "steps": ran},
            }],
        })
    return groups


def conclude_open_merge(repo_root: Path) -> "tuple[int, str]":
    """Run `reconcile-merge-conclude` as one measured cold child; a refusal's lines lead the text."""
    out = run_bin_cold(
        reconcile_manifest.CONCLUDE_CLI, (), repo_root=Path(repo_root), deadline_s=DEADLINE_S["engine"]
    )
    return out.exit_code, (out.stderr or "") + (out.stdout or "")


def finish_steps(decisions: Sequence[Any], ledger_rows: Sequence[Mapping[str, Any]], *, run: Mapping[str, Any]) -> list[Any]:
    """The post-briefing tail: marker, caches and the ceremony hook, with their cadence skips.

    A run whose Step 0 left a merge open first gets the conclude step, staged before the finish directives.
    """
    kwargs = dict(repo_root=Path(run["repo_root"]), today=_dt.date.fromisoformat(run["day"]))
    conclude: list[Any] = []
    if _merge_held(ledger_rows) is not None:
        conclude.append(StepSpec(
            id=CONCLUDE_STEP, stage=PRE_DIRECTIVE_STAGE, kind="inproc", target=CONCLUDE_TARGET, args=(),
            depends_on=None, already_satisfied=False, detail="Conclude the open reconcile merge.",
            heading="Observations", cost_class="engine",
        ))
    return [
        *conclude,
        *manifest.steps_for(POST_BRIEFING_STAGES, **kwargs),
        *manifest.cadence_skipped(POST_BRIEFING_STAGES, **kwargs),
    ]


def _memo_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def check_results(digest: Mapping[str, Any], results: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Inbox memos with no agent result, and `needs-ruling` results that carry no decision."""
    named: list[str] = []
    for group in digest["agent_work"]["groups"]:
        if group["id"] == "inbox":
            for entry in group["entries"]:
                named.extend(entry["inputs"].get("memos") or [])
    failures: list[tuple[str, str]] = []
    returned: set[str] = set()
    for res in (results.get("groups") or {}).get("inbox", []):
        returned.update(res.get("memos") or [])
        if res.get("outcome") == "needs-ruling" and not res.get("decision"):
            failures.append((f"inbox:{res['key']}", "needs-ruling-no-decision"))
    seen: set[str] = set()
    for memo in named:
        name = _memo_name(memo)
        if memo not in returned and name not in seen:
            seen.add(name)
            failures.append((f"inbox:{name}", "no-result"))
    return failures


def _directive_wanted(directive: Mapping[str, Any]) -> bool:
    """Only a directive gated on a decision runs; the manifest already covers the rest."""
    return bool(apply_base.normalize_depends_on(directive.get("depends_on")))


def _cost_class(key: str) -> str:
    return manifest._cost_rows().get(key, {}).get("cost_class", "unmeasured")


SPEC = WorkdayStartSpec(
    name=CEREMONY,
    dispatch_key=DISPATCH_KEY,
    run_dir_name=CEREMONY,
    template=TEMPLATE_RELPATH,
    marker_path=marker_path,
    launch_stages=LAUNCH_STAGES,
    finish_stages=FINISH_STAGES,
    front_door=_stages(LAUNCH_STAGES[: LAUNCH_STAGES.index(PHASE_ONE_LAST_STAGE) + 1]),
    envelope_step=ORIENT_STEP,
    stage_of=lambda _directive_id: DIRECTIVE_STAGE,
    residue=(_stages(LAUNCH_STAGES[LAUNCH_STAGES.index(PHASE_ONE_LAST_STAGE) + 1 :]),),
    rules=RULES,
    spec_points=lambda envelope, flags, ledger_rows: [],
    agent_groups=agent_groups,
    finish_steps=finish_steps,
    receipt_sections=(),
    render=briefing.render,
    costs=_cost_class,
    chain=None,
    launch_flags=frozenset(),
    hooks=RunHooks(
        after_step=_after_step,
        check_results=check_results,
        directive_wanted=_directive_wanted,
        acting_dispositions=rules.ACTING_DISPOSITIONS,
        held_stages=rules.HELD_STAGES,
    ),
)
