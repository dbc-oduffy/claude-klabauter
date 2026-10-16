"""Pure contract for the `--ask` gate: sizing load, arm selection, fire refusals, stage-1 plan path, resumable plan.

`ask_gate` and `ask_compose` build against these names. Nothing here
writes, spawns, or reroutes: a sizing whose route disagrees with its arm is refused by field.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import yaml

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, ResumePlan, ledger_halt_path, read_ledger_halt
from coordinator_core.ops.sizing_acceptance import lacks_human_acceptance, sizing_acceptance_skipped
from coordinator_core.session import record_homes

ARM_XS, ARM_S, ARM_M_PLUS, ARM_ROADMAP = "xs", "s", "m_plus", "roadmap"
ARM_ROUTE = {ARM_XS: "dispatch", ARM_S: "spec-dispatch", ARM_M_PLUS: "plan", ARM_ROADMAP: "roadmap"}
_PM_DECISION = "pm-decision"
_ROADMAP_EXIT = "roadmap"

_TSHIRT_ARM = {
    "XS": ARM_XS,
    "S": ARM_S,
    "M": ARM_M_PLUS,
    "L": ARM_M_PLUS,
    "XL": ARM_M_PLUS,
    "XXL": ARM_M_PLUS,
}
_FIREABLE_STATUS = ("sized", "routed")
# Literal `_scaffold_sizing` in coordinator-doc-new writes for intent and premise.evidence.
_SCAFFOLD_PLACEHOLDER = "PLACEHOLDER"
STATEMENT_ABSENT_PREFIX = "`exit_criterion.statement` is absent"


class SizingFireRefused(ValueError):
    """A sizing that cannot fire; `fields` carries one message per failing input."""

    def __init__(self, fields: Sequence[str]):
        self.fields = list(fields)
        super().__init__("; ".join(self.fields))


class SizingHandBack(Exception):
    """A shape-routed sizing: a clean hand-back to the PM, not a refusal; `line` is the one printed line."""

    def __init__(self, line: str):
        self.line = line
        super().__init__(line)


def load_sizing(repo_root: Path, sizing_rel: str) -> dict:
    """Read a sizing YAML contained under `<repo_root>/state/sizings/`."""
    root = Path(repo_root)
    p = Path(sizing_rel)
    if not p.is_absolute():
        p = root / p
    p = contained_path(p, [Path(record_homes.home_dir(str(root), "sizings"))])
    if p is None:
        raise SizingFireRefused([f"sizing escapes state/sizings/: {sizing_rel!r}"])
    if not p.is_file():
        raise SizingFireRefused([f"sizing-object not found on disk: {sizing_rel}"])
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SizingFireRefused([f"sizing-object is not valid YAML: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise SizingFireRefused(["sizing-object is not a YAML mapping"])
    return doc


def effective_route(sizing: Mapping) -> object:
    """`route`, except a `pm-decision` whose recorded `xl_exit` resolves it: the plan-resolving
    exit (plan_gate's constant) to `plan`, `roadmap` to `roadmap`. An unresolved one stays."""
    route = sizing.get("route")
    if route != _PM_DECISION:
        return route
    from coordinator_core.roadmap.plan_gate import _XL_EXIT_RESOLVING_TO_PLAN

    xl_exit = sizing.get("xl_exit")
    if xl_exit == _XL_EXIT_RESOLVING_TO_PLAN:
        return "plan"
    return _ROADMAP_EXIT if xl_exit == _ROADMAP_EXIT else route


def resolve_arm(sizing: Mapping) -> str:
    """The arm keyed by `estimate.tshirt`, ARM_ROADMAP for an XL+ roadmap route; an absent or
    unknown size is a refusal."""
    est = sizing.get("estimate")
    tshirt = est.get("tshirt") if isinstance(est, Mapping) else None
    arm = _TSHIRT_ARM.get(tshirt)
    if arm is None:
        raise SizingFireRefused(
            [f"`estimate.tshirt` is {tshirt!r} — expected one of {sorted(_TSHIRT_ARM)}"]
        )
    if arm == ARM_M_PLUS and tshirt in ("XL", "XXL") and effective_route(sizing) == _ROADMAP_EXIT:
        return ARM_ROADMAP
    return arm


def s_plan_path(sizing_rel: str) -> str:
    """Stage-1 plan location, `docs/plans/<sizing-stem>.md`."""
    return f"docs/plans/{PurePosixPath(str(sizing_rel).replace(chr(92), '/')).stem}.md"


def resumable_plan(
    sizing: Mapping, sizing_rel: str, repo_root: Path, arm: str | None = None
) -> ResumePlan | None:
    """The existing plan for this sizing and its rows still to run, else None.

    The plan is the sizing's `plan:` edge, else at arm S `s_plan_path`. Incomplete rows are those
    neither closed by disposition (`coded`, `delivered_elsewhere`, ...) nor landed by a checkpoint
    commit of this plan or a `ledger-check` halt commit whose `Halt-Plan:` names it; row reports
    on disk are stale history and do not refuse. A plan with rows, every one landed, raises SizingFireRefused ("already ran") unless a ledger-halt pointer
    names this plan and sizing and no row is coded: then `ResumePlan.ledger` carries it. A plan with
    no readable spine or no rows is not resumable (None). One file read, plus one more on the
    already-ran path; one `git log` only when a row is open.
    """
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import (
        NON_DISPATCHABLE_DISPOSITIONS,
        load_rows_memo,
        with_canonical_disposition,
    )

    edge = sizing.get("plan")
    plan_rel = edge if isinstance(edge, str) and edge.strip() else (s_plan_path(sizing_rel) if arm == ARM_S else None)
    if plan_rel is None:
        return None
    root = Path(repo_root)
    plan = contained_path(root / plan_rel, [root / "docs" / "plans"])
    if plan is None or not plan.is_file():
        return None
    result = load_rows_memo(plan.read_text(encoding="utf-8"))
    if result.status is not LocateStatus.LOCATED:
        return None
    rows = [with_canonical_disposition(r) for r in result.rows]
    ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
    if not ids:
        return None
    open_ids = [
        r["id"]
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and r.get("disposition") not in NON_DISPATCHABLE_DISPOSITIONS
    ]
    if open_ids:
        from coordinator_core.ops.dispatch_emit.emit import resume_landed_rows

        landed = resume_landed_rows(root, plan_rel)
        open_ids = [i for i in open_ids if i not in landed]
    if not open_ids:
        halt = _ledger_halt_for(root, plan_rel, sizing_rel, rows)
        if halt is not None:
            return ResumePlan(plan_rel, (), ledger=halt)
        raise SizingFireRefused(
            [f"{plan_rel} already ran: every row is landed; residue is a new sizing naming it"]
        )
    return ResumePlan(plan_rel, tuple(open_ids))


def _ledger_halt_for(root: Path, plan_rel: str, sizing_rel: str, rows: Sequence) -> LedgerHalt | None:
    """The ledger-halt pointer when it names this plan and sizing and no row is `coded` (a coded
    row means the terminal commit landed), else None."""
    if any(isinstance(r, dict) and r.get("disposition") == "coded" for r in rows):
        return None
    halt = read_ledger_halt(ledger_halt_path(root, plan_rel))
    if halt is None or halt.plan_rel != plan_rel or halt.sizing_rel != sizing_rel:
        return None
    return halt


def _acceptance_skipped(sizing: Mapping) -> bool:
    """The engine size rule, on the recorded route (never `effective_route`) and size."""
    est = sizing.get("estimate")
    return sizing_acceptance_skipped(sizing.get("route"), est.get("tshirt") if isinstance(est, Mapping) else None)


def collect_fire_refusals(
    sizing: Mapping,
    *,
    sizing_rel: str,
    arm: str,
    writes: Sequence[str],
    repo_root: Path | None = None,
    resume_sink: list | None = None,
) -> list[str]:
    """Every failing fire input, one message each, never stopping at the first; [] when fireable.

    `repo_root` enables the arm-s check that the stage-1 plan path is still free.
    `resume_sink`, when given, receives the arm-s `resumable_plan` answer (a `ResumePlan`, or
    None; nothing when it refused) so the caller does not repeat that read's `git log`.
    """
    out: list[str] = []
    ec = sizing.get("exit_criterion")
    ec = ec if isinstance(ec, Mapping) else {}
    if not ec.get("statement"):
        out.append(f"{STATEMENT_ABSENT_PREFIX} — nothing to hand off as the exit criterion")
    if lacks_human_acceptance(ec.get("accepted")) and not _acceptance_skipped(sizing):
        out.append(
            "`exit_criterion.accepted` is null — accept it first: "
            f"sizing-accept-exit-criterion --sizing {sizing_rel} --pm-quote \"<PM's words>\" "
            "(or --apm-ruling \"<ruling>\" --ruling-ref <path> in pm/ceo mode)"
        )
    if not sizing.get("interaction_mode"):
        out.append("`interaction_mode` is absent")
    intent = sizing.get("intent")
    if isinstance(intent, str) and intent.startswith(_SCAFFOLD_PLACEHOLDER):
        out.append("`intent` is still the scaffold placeholder — write the PM's ask, verbatim")
    premise = sizing.get("premise")
    premise = premise if isinstance(premise, Mapping) else {}
    evidence = premise.get("evidence")
    if isinstance(evidence, str) and evidence.startswith(_SCAFFOLD_PLACEHOLDER):
        out.append("`premise.evidence` is still the scaffold placeholder — cite what you actually looked at")
    if premise.get("provenance") == "unrecorded":
        out.append("`premise.provenance` is 'unrecorded' — record how the premise was verified")
    status = sizing.get("status")
    if status not in _FIREABLE_STATUS:
        out.append(f"`status` is {status!r}, not one of {list(_FIREABLE_STATUS)}")
    expected = ARM_ROUTE[arm]
    route = effective_route(sizing)
    if route != expected:
        out.append(f"`route` is {route!r}, but arm {arm!r} fires only route {expected!r}")
    if arm == ARM_XS and not list(writes):
        out.append(
            "XS needs `writes` (the ask's file footprint): a sizing carries no footprint; "
            "name it as args.writes beside args.sizing"
        )
    if arm == ARM_S and repo_root is not None:
        plan_rel = s_plan_path(sizing_rel)
        try:
            resumable = resumable_plan(sizing, sizing_rel, Path(repo_root), arm)
            if resume_sink is not None:
                resume_sink.append(resumable)
        except SizingFireRefused as exc:
            out.extend(exc.fields)
            resumable = plan_rel
        if resumable is None and (Path(repo_root) / plan_rel).exists():
            out.append(
                f"{plan_rel} already exists — stage 1 would overwrite it; "
                f"run `emit-dispatch-workflow --plan {plan_rel}` instead"
            )
    return out
