"""dispatch.ask_gate handler: one sizing in, an arm to run or a halt (room, touchpoint, refusal) out.

Reads the sizing; at M+ with no halt it mints the baton record in-process (a write). An unfired
plan on disk with rows still to run rides the verdict as `resume_plan` and `resume_rows`.

Every verdict carries the sizing's `design_route` payload when one is recorded. The gate never
halts on it: a canvas route with no recorded approval arms (plan authored first) and
`ask_stage.stage` refuses it with a `design-review:` reason, so the halt falls after plan and
before stage.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.warp_fire import fire_command
from coordinator_core.ops.dispatch_emit import design_route, phase_ledger
from coordinator_core.ops.dispatch_emit.ask_exclusions import excluded_paths

RESUME_EXCLUDE_FIELD = "exclude"
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALT_REFUSAL,
    HALT_RESEARCH,
    HALT_ROOM,
    HALT_SPLIT,
    HALT_TOUCHPOINT,
    LEG_REFUSE,
    UNCLAIMED_ROWS_HALT_KIND,
    RESEARCH_HANDBACK_ROUTE,
    RESEARCH_MIXED_REFUSAL,
    research_is_bounded_single_write,
    LEDGER_DERIVED_ROW_PREFIX,
    RESUME_LEDGER_ARM_FIELD,
    RESUME_LEDGER_CARRIED_MET_FIELD,
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_DERIVED_FIELD,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_LEG_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    RESUME_LEDGER_REVISED_FIELD,
    RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
    RESUME_LEDGER_SINCE_SHA_FIELD,
    RESUME_LEDGER_UNCLAIMED_FIELD,
    RESUME_LEDGER_UNSERVED_FIELD,
    RESUME_LEDGER_UNLANDED_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
    RESUMED_RUN_ID_KEY,
    RUN_OUTCOME_FILE,
    GateVerdict,
    read_run_outcome,
    record_dir,
    write_run_baton,
    rulable_ids,
)
from coordinator_core.ops.sizing_acceptance import (
    ENGINE_SIZE_RULE,
    apm_picks_xl_exit,
    lacks_human_acceptance,
    sizing_acceptance_skipped,
)
from coordinator_core.ops.dispatch_emit.ask_stage import (
    PLAN_EXECUTING_STAMP_KEY,
    AskStageError,
    stage,
    stamp_plan_executing,
)
from coordinator_core.ops.dispatch_emit import ask_stage_receipt, reply_sidecar
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_S,
    ALL_LANDED_NO_RUN,
    ARM_M_PLUS,
    ARM_ROADMAP,
    ARM_XS,
    STATEMENT_ABSENT_PREFIX,
    COMMIT_HALT_KIND,
    RESUME_LEDGER_HALT_BLOCKING_FIELD,
    SizingFireRefused,
    collect_fire_refusals,
    effective_route,
    load_sizing,
    load_sizing_effective,
    resolve_arm,
    resumable_plan,
    set_fleet_mode_provider,
)
from coordinator_core.session.mode_resolution import resolve_mode


def resolve_fleet_interaction_mode() -> Optional[str]:
    """`resolve_mode("interaction_mode")` as `sizing-assemble` resolves it with no flag.

    Installed as `sizing_fire`'s fleet-mode provider on import: this module is a sanctioned
    fleet-record reader, so `sizing_fire` itself stays out of the reader closure. Every module that
    reads a sizing through `load_sizing_effective` must import this one first.
    """
    return resolve_mode("interaction_mode", "sizing-assemble-cli", env=None)


set_fleet_mode_provider(resolve_fleet_interaction_mode)

_ACCEPT_TOUCHPOINTS = frozenset({"accept_sizing", "accept_exit_criterion"})
_ACCEPTED_NULL_PREFIX = "`exit_criterion.accepted`"
_PARAMS = (
    Field("sizing_path", "nonempty_str", required=True),
    Field("writes", "str_list"),
    Field("declared_writes", "str_list"),
    Field("baton", "nonempty_str"),
    Field("record", "dict"),
    Field("run_id", "nonempty_str"),
    Field("cross_repo_approved", "bool"),
    Field("session_id", "str"),
    Field("route_override_reason", "str"),
    Field("require_live_token", "bool"),
)
_RECORD_KINDS = ("exit_criterion", "xl_exit", design_route.RECORD_KIND)
_XL_PICKS = ("roadmap", "accept_multi_session", "split")
_DOC_NEW = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"


def handback_line(sizing: dict) -> str:
    """The one line a shape-routed sizing prints: topic from `name`, else a short slice of `intent`."""
    topic = str(sizing.get("name") or "").strip() or " ".join(str(sizing.get("intent") or "").split())[:60].strip()
    return (
        f"Job unclear: {topic}. Needs a PM conversation (coordinator:shape room); "
        "re-run --ask once the JTBD is stated."
    )


def _halt(kind: str, reason: str, **extra: object) -> GateVerdict:
    return GateVerdict(arm=None, halt={"kind": kind, "reason": reason, **extra})


#: Longest the gate waits on any file lock it takes itself; the gate's whole budget is 500ms.
GATE_LOCK_WAIT_SECS = 0.2


def _held_lock_refusal(root: Path, target: Path) -> Optional[str]:
    """The refusal naming `target` and its lock holder when another process holds `target`'s RMW lock, else None.

    One non-blocking try on the lock sidecar `locked_rmw` keys for `target`: never waits, takes no
    lock past the probe. The holder is the sidecar's metadata when the holder wrote any (a `held_lock`
    holder does; a `locked_rmw` holder does not, and reads "holder unknown").
    """
    from coordinator_core import locked_write as lw

    try:
        lock_path = lw._lock_dir(root) / f"{lw._lock_key(target)}.lock"
    except (RuntimeError, OSError):
        return None  # no git common dir: `locked_rmw` refuses the same root itself
    flags = os.O_CREAT | (os.O_WRONLY if lw._FCNTL_AVAILABLE else os.O_RDWR)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        return None
    try:
        if lw._plat_try_lock(fd):
            lw._plat_unlock(fd)
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return (
        f"lock held on {target.as_posix()} (held by: {lw._describe_holder(lock_path)}); "
        f"the gate does not wait on a held lock. Retry once the holder finishes."
    )


@functools.cache
def _load_doc_new():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_for_ask_gate", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _design_scope(sizing: dict, writes: Sequence[str]) -> list[str]:
    """The paths the gate can see: the scouted writes plus each acceptance row's writes."""
    scope = [w for w in writes if isinstance(w, str) and w.strip()]
    for row in sizing.get("acceptance_rows") or []:
        if isinstance(row, dict):
            scope += [w for w in row.get("writes") or [] if isinstance(w, str) and w.strip()]
    return list(dict.fromkeys(scope))


def gate(
    repo_root: Path,
    sizing_rel: str,
    *,
    writes: Sequence[str] = (),
    baton: Optional[str] = None,
    mint_design_rows: bool = False,
    landed_sink: Optional[list] = None,
    run_id: Optional[str] = None,
    kept_sink: Optional[list] = None,
    phase_sink: Optional[list] = None,
    held_sink: Optional[list] = None,
    reasons_sink: Optional[list] = None,
) -> GateVerdict:
    """Arm for the sizing at `sizing_rel`, or the single halt that stops it, naming every cause.

    `kept_sink`, when given, receives `(kept row ids, kept_run)` for a row resume that keeps rows.
    `held_sink`, when given, receives the resume's `ResumePlan.held` on the row and the ledger resume alike.
    `reasons_sink` likewise receives `ResumePlan.not_run_reasons`.

    `baton` (repo-relative) goes to the mint at M+, which owns the baton checks; XS carries it when it
    is this sizing's own (never in `GateVerdict.baton`) and refuses any other; S and roadmap mint no
    baton, so they refuse one. A non-halted verdict carries the design-registry brief for the
    sizing's scope; a malformed registry with a non-empty scope halts `registry-unparseable`.
    `mint_design_rows` (a run-bound call) also appends one design row per hit to the register.
    `landed_sink`, when given, receives the row resume's `ResumePlan.landed_open`.
    `phase_sink`, when given, receives one `phase_ledger.resume_phases` dict for a resume verdict.
    `run_id` (a run-bound call) carries the run's and its `resumed_from` predecessor's live (unretracted)
    revise notes into `resume_ledger` and the run's requirements view, and refuses nothing on notes.
    """
    from coordinator_core import design_registry as dr
    from coordinator_core.ops.dispatch_emit import design_conformance as dc

    root = Path(repo_root)
    route_payload = None
    try:
        sizing = load_sizing(root, sizing_rel)
        scope = _design_scope(sizing, writes)
        route_payload, _ = design_route.read(sizing, sizing_rel)
    except SizingFireRefused:
        scope = []
    hits, error = dr.lookup(root, scope) if scope else ([], None)
    registry = dr.registry_path(root)
    try:
        shown = registry.relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = None
    if error:
        return replace(_halt(HALT_REFUSAL, f"{error}: repair {shown or registry}"), design_route=route_payload)
    verdict = _arm_verdict(
        root, sizing_rel, writes=writes, baton=baton, landed_sink=landed_sink, run_id=run_id, kept_sink=kept_sink,
        phase_sink=phase_sink, held_sink=held_sink, reasons_sink=reasons_sink,
    )
    if route_payload is not None and verdict.design_route is None:
        verdict = replace(verdict, design_route=route_payload)
    if verdict.halt is not None or not scope:
        return verdict
    registry_rel = shown if registry.is_file() else None
    brief = dc.brief_text(hits or [], registry_rel)
    if mint_design_rows and hits:
        dc.mint_rows(root, sizing_rel, hits)
    return replace(verdict, design_brief=brief) if brief else verdict


_SIZING_STEM_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?:[0-9a-f]{6}-\d+-)?")


def _accepted_plan_hit(repo_root: Path, sizing: dict, sizing_rel: str) -> Optional[str]:
    """Repo-relative path of an exact (slug or ask-hash) plan hit in an accepted status, else None.

    Fallback for a sizing minted before intake bound the `plan:` edge; a sizing already naming a plan is skipped.
    """
    from coordinator_core.ops.dispatch_emit.extant_work import (
        ACCEPTED_PLAN_STATUSES,
        compute_ask_hash,
        lookup_extant,
    )

    if sizing.get("plan"):
        return None
    intent = str(sizing.get("intent") or "")
    slug = _SIZING_STEM_PREFIX_RE.sub("", Path(sizing_rel).stem)
    found = lookup_extant(str(repo_root), slug, compute_ask_hash(intent) if intent else "", intent)
    for hit in found.exact:
        if hit.kind == "plan" and hit.plan_status in ACCEPTED_PLAN_STATUSES:
            return hit.path
    return None


def _baton_belongs_to_sizing(repo_root: Path, sizing: dict, sizing_rel: str, baton: str) -> bool:
    """True when the sizing's `baton:` names `baton`, or the baton's `sizing_object:` names the sizing.

    Both sides compare repo-relative paths with separators folded to `/`; a missing baton file is False.
    """
    # Deferred to keep the frontmatter package off ask_gate's import path until a baton is checked.
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    def fold(p: object) -> str:
        return str(p or "").strip().replace("\\", "/")

    given = fold(baton)
    if given and fold(sizing.get("baton")) == given:
        return True
    if not given or given.startswith("/") or ":" in given or ".." in given.split("/"):
        return False
    target = repo_root / given
    if not target.is_file():
        return False
    split = split_frontmatter(target.read_text(encoding="utf-8"))
    owner = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    return fold(owner) == fold(sizing_rel)


def _newest_run_binding(repo_root: Path, sizing_rel: str, skip: Optional[str]) -> Optional[str]:
    """The newest `warp-<stem>-*` / `ask-*` run id binding `sizing_rel`, other than `skip` and any
    `orphan_pending` fire; None when there is none. One scandir per record root; `ask-*` dirs are
    read only when stamped on or after the sizing filename's date."""
    from pathlib import PurePosixPath

    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import orphan_adopt as oa
    from coordinator_core.ops.dispatch_emit.ask_contract import _read_json_object

    norm = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
    stem = PurePosixPath(norm).stem
    dated = re.match(r"(\d{4})-(\d{2})-(\d{2})", stem)
    floor = "".join(dated.groups()) if dated else None
    prefix = f"warp-{stem}-"
    best: Optional[tuple[str, str, Path]] = None
    for rel in oa._ROOTS:
        try:
            with os.scandir(repo_root / rel) as it:
                for entry in it:
                    name = entry.name
                    if name == skip or not entry.is_dir() or not safe_id(name):
                        continue
                    if name.startswith(prefix):
                        stamp = name[len(prefix):]
                    elif name.startswith("ask-") and floor is not None:
                        stamp = name[4:]
                        if stamp[:8] < floor:
                            continue
                    else:
                        continue
                    if len(stamp) != 22 or (best is not None and stamp <= best[0]):
                        continue
                    if not name.startswith(prefix) and (
                        (_read_json_object(Path(entry.path) / "run.json") or {}).get("sizing_rel") != norm
                    ):
                        continue
                    marker = oa._read_marker(Path(entry.path))
                    if marker and marker.get("halted") == oa.ORPHAN_PENDING:
                        continue
                    best = (stamp, name, Path(entry.path))
        except OSError:
            continue
    return best[1] if best else None


def _orphan_pending_halt(repo_root: Path, sizing_rel: str, run_id: Optional[str]) -> Optional[GateVerdict]:
    """The `orphan_pending` halt naming the adopt call when the newest run binding the sizing is an
    orphan (dead emitter, nothing halted, nothing newer); else None."""
    from coordinator_core.ops.dispatch_emit import halt_refire
    from coordinator_core.ops.dispatch_emit import orphan_adopt as oa
    from coordinator_core.session.core import resolve_session_id

    newest = _newest_run_binding(repo_root, sizing_rel, run_id)
    if newest is None:
        return None
    verdict = oa.orphan_verdict(repo_root, newest, resolve_session_id() or "")
    if not verdict["orphaned"]:
        return None
    reason = (
        f"run {newest} was orphaned by dead session {verdict['recorded_session']} at stage {verdict['stage']}: "
        "adopt it to resume from its first unreceipted stage instead of re-running the sizing"
    )
    return _halt(
        oa.ORPHAN_PENDING, reason, orphan_run=newest,
        next_action=halt_refire.exit_next_action(
            reason=reason, route=halt_refire.ROUTE_ADOPT, call=oa.adopt_call(newest, repo_root)
        ),
    )


def _arm_verdict(
    repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = (), baton: Optional[str] = None,
    landed_sink: Optional[list] = None, run_id: Optional[str] = None, kept_sink: Optional[list] = None,
    phase_sink: Optional[list] = None, held_sink: Optional[list] = None, reasons_sink: Optional[list] = None,
) -> GateVerdict:
    """The arm-or-halt verdict, before the design-registry leg."""
    from coordinator_core import sizing_assemble as sa

    repo_root = Path(repo_root)
    try:
        sizing = load_sizing_effective(repo_root, sizing_rel)
        arm = resolve_arm(sizing)
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))

    pending_orphan = _orphan_pending_halt(repo_root, sizing_rel, run_id)
    if pending_orphan is not None:
        return pending_orphan

    if arm == ARM_XS and not writes:
        # The printed re-land line carries no `writes`: the halted run's manifest recorded them.
        from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

        writes = recorded_xs_writes(repo_root, sizing_rel)

    if baton and arm == ARM_XS:
        if not _baton_belongs_to_sizing(repo_root, sizing, sizing_rel, baton):
            return _halt(
                HALT_REFUSAL,
                f"baton {baton} is not this sizing's: neither its sizing_object nor the sizing's baton names the other",
            )
    elif baton and arm != ARM_M_PLUS:
        return _halt(HALT_REFUSAL, f"baton given but arm {arm} mints no baton")

    if sizing.get("xl_exit") == "split":
        return _halt(
            HALT_SPLIT,
            "xl_exit recorded as split: the bundle resolves to sibling runs, not one",
        )
    route = effective_route(sizing)
    if route == "shape":  # DR-450 exemption: only the PM can state the JTBD, so no workflow fronts shape
        return _halt(
            HALT_ROOM,
            f"route 'shape' is room-owned: {sa._ROOM_ENTRY['shape']}",
            route=route,
            handback=handback_line(sizing),
        )
    research = sizing.get("research")
    if isinstance(research, dict) and research and not research_is_bounded_single_write(sizing, writes):
        if writes:
            return _halt(HALT_REFUSAL, RESEARCH_MIXED_REFUSAL)
        return _halt(
            HALT_RESEARCH,
            f"research sizing {sizing_rel}: the run's `fire` entry is the one call",
            route=RESEARCH_HANDBACK_ROUTE,
            fire=[fire_command(sizing_rel)],
        )
    if arm != ARM_ROADMAP and route in sa._ROOM_ENTRY:
        extra = {}
        if route == "pm-decision" and apm_picks_xl_exit(sizing.get("interaction_mode")):
            # The script's xl-exit stage records the APM's pick and re-gates; pm mode halts here.
            extra["apm_xl_exit"] = True
        return _halt(
            HALT_ROOM,
            f"route {route!r} is room-owned: {sa._ROOM_ENTRY[route]}",
            route=route,
            **extra,
        )

    resume_sink: list = []
    refusals = collect_fire_refusals(
        sizing, sizing_rel=sizing_rel, arm=arm, writes=writes, repo_root=repo_root,
        resume_sink=resume_sink,
    )
    route_payload, route_bad = design_route.read(sizing, sizing_rel)
    if route_bad:
        refusals.append(route_bad)
    accepted_null = lacks_human_acceptance((sizing.get("exit_criterion") or {}).get("accepted"))
    mode = sizing.get("interaction_mode")
    tshirt = sizing["estimate"]["tshirt"]
    needs_acceptance = False
    if mode in sa.TOUCHPOINTS_BY_MODE:
        needs_acceptance = any(
            t["id"] in _ACCEPT_TOUCHPOINTS
            for t in sa.touchpoints(mode, tshirt, sizing.get("route"))
        )
    if any(r.startswith(STATEMENT_ABSENT_PREFIX) for r in refusals):
        # A sizing written exit-criterion-pending: the accept stage authors the statement in-run.
        others = [r for r in refusals if not r.startswith((STATEMENT_ABSENT_PREFIX, _ACCEPTED_NULL_PREFIX))]
        if others:
            return _halt(HALT_REFUSAL, "; ".join(others))
        return _halt(
            HALT_TOUCHPOINT,
            "no exit criterion on record",
            touchpoint=f"coordinator-invoke sizing.accept_exit_criterion with a statement for {sizing_rel}",
        )
    if accepted_null and needs_acceptance:
        line = next((r for r in refusals if r.startswith(_ACCEPTED_NULL_PREFIX)), "")
        return _halt(
            HALT_TOUCHPOINT,
            f"{mode} mode asks for the exit criterion at {tshirt}",
            touchpoint=line.partition("accept it first: ")[2]
            or f"coordinator-invoke sizing.accept_exit_criterion for {sizing_rel}",
        )
    acceptance = None
    if accepted_null:
        refusals = [r for r in refusals if not r.startswith(_ACCEPTED_NULL_PREFIX)]
    if accepted_null and sizing_acceptance_skipped(sizing.get("route"), tshirt):
        # Nobody was asked: the receipt says so, since `accepted` stays null.
        acceptance = {
            "by": ENGINE_SIZE_RULE,
            "route": sizing.get("route"),
            "tshirt": tshirt,
            "mode": mode,
            "ruling": "2026-10-08",
        }
    resume = None
    if resume_sink:
        resume = resume_sink[0]
    elif arm != ARM_ROADMAP:
        try:
            resume = resumable_plan(sizing, sizing_rel, repo_root, arm)
        except SizingFireRefused as exc:
            refusals.extend(f for f in exc.fields if f not in refusals)
    if refusals:
        unread = _unread_notes_on_ran_refusal(repo_root, refusals, run_id)
        if unread:
            return _halt(
                HALT_REFUSAL,
                f"{'; '.join(refusals)}; run {run_id} carries unread revise note(s) {unread}: "
                "reopen work with `workflow.revise` with `verbatim`",
            )
        return _halt(HALT_REFUSAL, "; ".join(refusals))
    try:
        resume_ledger = _resume_ledger(sizing, resume.ledger, repo_root, run_id=run_id, sizing_rel=sizing_rel) if resume else None
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))
    resume_plan = resume.plan_rel if resume and resume_ledger is None else None
    resume_rows = list(resume.incomplete) if resume and resume_ledger is None else None
    if landed_sink is not None and resume_rows:
        landed_sink.extend(resume.landed_open)
    if kept_sink is not None and resume_rows and resume.kept:
        kept_sink.append((list(resume.kept), resume.kept_run))
    if held_sink is not None and resume:
        held_sink.extend(resume.held)
    if reasons_sink is not None and resume:
        reasons_sink.extend(resume.not_run_reasons)
    pending_rulings = None
    ready_memo: dict = {}

    def plan_ready(plan_rel: str) -> bool:
        # One readiness run per gate call: it spawns the plan's anchor-premise checks.
        if plan_rel not in ready_memo:
            ready_memo[plan_rel] = _plan_gate_resume_ready(repo_root, sizing_rel, plan_rel)
        return ready_memo[plan_rel]

    if resume_plan is not None:
        from coordinator_core.ops.dispatch_emit.requirements_ledger import pending_ruling_ids

        pending_rulings = list(pending_ruling_ids((sizing.get("requirement_register") or {}).get("rows") or []))
    if resume and phase_sink is not None:
        pending = pending_rulings
        if pending is None:
            from coordinator_core.ops.dispatch_emit.requirements_ledger import pending_ruling_ids

            pending = list(pending_ruling_ids((sizing.get("requirement_register") or {}).get("rows") or []))
        phase_sink.append(_resume_phases(repo_root, sizing_rel, resume, resume_ledger, pending, plan_ready))

    if arm != ARM_M_PLUS:
        # roadmap mints no baton: roadmap-blitz stubs are the batons
        return GateVerdict(
            arm=arm, halt=None, acceptance=acceptance, resume_plan=resume_plan, resume_rows=resume_rows,
            resume_ledger=resume_ledger, design_route=route_payload, pending_rulings=pending_rulings,
        )

    if resume is None:
        accepted_plan = _accepted_plan_hit(repo_root, sizing, sizing_rel)
        if accepted_plan:
            return _halt(
                HALT_REFUSAL,
                f"accepted plan {accepted_plan} already exists for this ask: set `plan: {accepted_plan}` on "
                f"{sizing_rel} and refire to resume it",
            )

    doc_new = _load_doc_new()
    try:
        baton = doc_new.mint_baton_from_sizing(sizing_rel, str(repo_root), baton=baton)
    except doc_new.SizingMintRefused as exc:
        return _halt(HALT_REFUSAL, str(exc))
    # The effective route, so a PM-recorded accept_multi_session plans in the
    # single-mode wave instead of re-adjudicating the sizing's raw `pm-decision`.
    minted = {"id": baton["id"], "path": baton["path"], "route": route}
    deliverable_id = str(sizing.get("deliverable_id") or "").strip()
    if deliverable_id:
        minted["deliverableId"] = deliverable_id
    spine, spine_refusal = (None, None) if resume else _spine_plan(repo_root, sizing, sizing_rel)
    if spine_refusal:
        return _halt(HALT_REFUSAL, spine_refusal)
    resume_ready = bool(resume_plan) and pending_rulings == [] and plan_ready(resume_plan)
    return GateVerdict(
        resume_ready=resume_ready,
        arm=arm,
        halt=None,
        baton=minted,
        acceptance=acceptance,
        resume_plan=resume_plan,
        resume_rows=resume_rows,
        spine_plan=spine,
        resume_ledger=resume_ledger,
        design_route=route_payload,
        pending_rulings=pending_rulings,
    )


def _unread_notes_on_ran_refusal(repo_root: Path, refusals: Sequence[str], run_id: Optional[str]) -> list[str]:
    """Live carried revise notes (each capped at 120 chars) of `run_id`'s chain when `refusals` hold the
    all-landed "already ran" refusal, else []. A note never reopens landed rows; only a verbatim row does."""
    # Phrase is built in sizing_fire's all-landed refusal; keep the two in step.
    if not run_id or not any(" already ran: every row is landed" in r for r in refusals):
        return []
    from coordinator_core.ops.dispatch_emit import run_notes
    from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused, carried_notes

    try:
        live = run_notes.live(carried_notes(Path(repo_root), run_id))
    except LedgerRefused:
        return []
    return [repr(n["note"][:120]) for n in live]


def _resume_phases(repo_root: Path, sizing_rel: str, resume, resume_ledger: Optional[dict], pending: Sequence[str], plan_ready) -> dict:
    """`phase_ledger.resume_phases` for a resume of a halted run: landed phases the halt left valid.

    plan / plan_review are credited only when `_plan_gate_resume_ready` holds with no ruling pending;
    an unreadable halt kind ("unknown") invalidates every phase."""
    from coordinator_core.ops.dispatch_emit.halted_run import _read, halted_ask_run_for

    root = Path(repo_root)
    ledger = resume.ledger
    pred = None
    if ledger is not None and ledger.run_id != ALL_LANDED_NO_RUN:
        pred = ledger.run_id
    pred = pred or resume.kept_run or halted_ask_run_for(root, sizing_rel)
    kind = ledger.halted if ledger is not None else None
    if kind is None and pred:
        try:
            marker = _read(record_dir(root, pred))
        except ValueError:
            marker = None
        kind = marker.get("halted") if marker else None
    landed = phase_ledger.derive(root, pred) if pred else {}
    # An execute-phase halt passed its plan gate: readiness of a refused plan is not its question.
    past_plan_gate = phase_ledger.HALT_PHASE.get(kind or "unknown") == "execute"
    if landed.get("plan") and (pending or (not past_plan_gate and not plan_ready(resume.plan_rel))):
        landed.pop("plan", None)
        landed.pop("plan_review", None)
    revised = []
    if resume_ledger and (resume_ledger.get(RESUME_LEDGER_NOTES_FIELD) or resume_ledger.get(RESUME_LEDGER_REVISED_FIELD)):
        revised.append("requirement")
    return phase_ledger.resume_phases(landed, kind or "unknown", revised)


def _plan_gate_resume_ready(repo_root: Path, sizing_rel: str, plan_rel: str) -> bool:
    """True when the newest halted ask run of this sizing was a plan-gate refusal of these very
    plan bytes and `plan.readiness_verdict` over its carried reviews now reads `ready`.

    One `kept.json` read, one plan hash, at most one in-process readiness call; no git spawn, but
    readiness runs the plan's anchor-premise check commands, which do spawn.
    """
    import hashlib

    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for
    from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused, run_dir

    halted = halted_ask_run_for(repo_root, sizing_rel)
    if halted is None:
        return False
    try:
        gate_record = kept_work.read(run_dir(repo_root, halted))["plan_gate"]
        if not gate_record or gate_record.get("plan") != plan_rel:
            return False
        digest = hashlib.sha256((Path(repo_root) / plan_rel).read_bytes()).hexdigest()
        if gate_record.get("plan_sha256") != digest:
            return False
        from coordinator_core.ops.plan_readiness_verdict import _handler as readiness

        params = {"plan": plan_rel}
        for key in ("reviews", "coverage"):
            if gate_record.get(key) is not None:
                params[key] = gate_record[key]
        return readiness(params, repo_root=repo_root).get("verdict") == "ready"
    except (LedgerRefused, OSError, ValueError):
        return False


#: The composed ledger-check script's demotion step for a met row whose met_by no note names; the
#: composed JS in ask_compose.py repeats it verbatim.
PRE_HALT_CREDIT_DEMOTION_STEP = "met_by: the commit sha that carries the credited work"


def _without_gate_verdicts(row_id: str, record):
    """`record` with the commit gate's own verdicts removed from its `remaining` steps: the
    unchanged-since-halt refusal (bare or `<id>: `-prefixed) and the pre-halt-credit demotion are
    verdicts the verify leg re-judges, not owed work, so a row carrying only them owes no re-plan."""
    if not isinstance(record, dict) or not isinstance(record.get("remaining"), list):
        return record
    verdicts = {UNCHANGED_SINCE_HALT_REASON, f"{row_id}: {UNCHANGED_SINCE_HALT_REASON}", PRE_HALT_CREDIT_DEMOTION_STEP}
    return {**record, "remaining": [s for s in record["remaining"] if not (isinstance(s, str) and s.strip() in verdicts)]}


def _discharged_em_items(blocking: Sequence[str], discharged: Mapping, plan_path: Path) -> set[str]:
    """Blocking items whose serving plan rows are all em-performed and em-discharged: the EM's
    recorded discharge is the evidence, so such an item owes no re-plan. An unreadable plan, or an
    item with no serving row, is never discharged."""
    if not discharged or not blocking:
        return set()
    from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import PlanRowsUnreadable, serving_rows

    out: set[str] = set()
    for item in blocking:
        try:
            rows = serving_rows(plan_path, [item])
        except PlanRowsUnreadable:
            return set()
        if rows and all(row_is_em_performed(r) and str(r.get("id")) in discharged for r in rows):
            out.add(item)
    return out


def _credited_landed(blocking: Sequence[str], halt, repo_root: Path) -> list[str]:
    """Blocking items whose serving plan rows all landed, one at least after the halt commit.

    Reads only `halt.landed_since_halt` and `halt.unlanded`; an item with an unlanded or no
    serving row, or an unreadable plan, is never credited."""
    since = set(halt.landed_since_halt or ())
    if not since or not blocking:
        return []
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import PlanRowsUnreadable, serving_rows

    unlanded = set(halt.unlanded)
    credited: list[str] = []
    for item in blocking:
        try:
            ids = {str(r.get("id")) for r in serving_rows(Path(repo_root) / halt.plan_rel, [item])}
        except PlanRowsUnreadable:
            return []
        if ids and not ids & unlanded and ids & since:
            credited.append(item)
    return credited


def _unserved_revise_rows(revised: Sequence[str], remaining: Mapping, unclaimed: Sequence[str], halt, repo_root: Path) -> list[str]:
    """Revise-minted blocking ids with no halt steps, no serving plan row and no unclaimed claim: the
    ledger-replan leg serves them. An unreadable plan adds nothing. Reads the plan once per candidate
    item, """
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import PlanRowsUnreadable, serving_rows

    out: list[str] = []
    for item in revised:
        record = remaining.get(item)
        if (isinstance(record, dict) and record.get("remaining")) or item in unclaimed:
            continue
        try:
            if serving_rows(Path(repo_root) / halt.plan_rel, [item]):
                continue
        except PlanRowsUnreadable:
            return []
        out.append(item)
    return out


def _resume_ledger(
    sizing: dict, halt, repo_root: Path, run_id: Optional[str] = None, sizing_rel: Optional[str] = None
) -> Optional[dict]:
    """The verdict's `resume_ledger` for a ledger-halt resume: run, plan, blocking ids, the
    sizing's rulable register ids and the plan's em-discharged rows as `{row_id: text}`; None when
    the resume carries no halt. `blocking` is the halt's blocking ids followed by every other rulable
    row (neither met nor waived), so rows recorded or left unjudged after the halt are judged too."""
    if halt is None:
        return None
    from coordinator_core.ops.dispatch_emit.requirements_ledger import (
        NOTES_FILE,
        ORIGIN_REVISION,
        _origin,
        _unclaimed_rows,
        carried_notes,
        ledger_ids,
        ruled_out_ids,
        run_dir,
    )
    from coordinator_core.ops.dispatch_emit import run_notes
    from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

    rows = (sizing.get("requirement_register") or {}).get("rows") or []
    item_ids, withheld_ids = ledger_ids([r for r in rows if isinstance(r, dict)])
    out = set(ruled_out_ids([r for r in rows if isinstance(r, dict)]))
    rulable = [r for r in rulable_ids(rows) if r not in out]
    discharged = {
        str(row_id): entry["text"]
        for row_id, entry in read_em_discharges(Path(repo_root) / halt.plan_rel).items()
        if isinstance(entry, dict) and isinstance(entry.get("text"), str)
    }
    carried = [c for c in halt.carried_met if c not in out]
    blocking = [
        *(b for b in halt.blocking if b not in out),
        *(r for r in rulable if r not in halt.blocking and r not in carried),
    ]
    credited = _credited_landed(blocking, halt, repo_root)
    blocking = [b for b in blocking if b not in credited]
    all_landed = halt.halted == ALL_LANDED_HALT_KIND
    notes = (
        [] if halt.halted in (ALL_LANDED_HALT_KIND, UNCLAIMED_ROWS_HALT_KIND) and halt.run_id == ALL_LANDED_NO_RUN
        else [e["note"] for e in run_notes.live(run_notes.entries(run_dir(Path(repo_root), halt.run_id) / NOTES_FILE, halt.run_id))]
    )
    if run_id:
        notes = list(dict.fromkeys([*notes, *(n["note"] for n in run_notes.live(carried_notes(Path(repo_root), run_id)))]))
    revision_ids = {
        str(r.get("id")) for r in rows
        if isinstance(r, dict) and _origin(r.get("anchor"))[0] == ORIGIN_REVISION
    }
    em_done = _discharged_em_items(blocking, discharged, Path(repo_root) / halt.plan_rel)
    verdict = {
        "run_id": halt.run_id,
        "plan": halt.plan_rel,
        "blocking": blocking,
        RESUME_LEDGER_CARRIED_MET_FIELD: carried,
        RESUME_LEDGER_HALT_BLOCKING_FIELD: [b for b in halt.blocking if b not in out],
        RESUME_LEDGER_EM_DISCHARGED_FIELD: discharged,
        "rulable_ids": rulable,
        "ledger_item_ids": list(item_ids),
        "withheld_ids": list(withheld_ids),
        RESUME_LEDGER_HALT_SHA_FIELD: halt.halt_sha,
        RESUME_LEDGER_SINCE_SHA_FIELD: halt.since_sha,
        RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: list(halt.since_halt_shas),
        RESUME_LEDGER_REMAINING_FIELD: {
            k: (
                {**v, "remaining": []}
                if k in em_done and isinstance(v, dict)
                else _without_gate_verdicts(k, v)
            )
            for k, v in halt.remaining.items() if k in blocking
        },
        RESUME_LEDGER_UNLANDED_FIELD: list(halt.unlanded),
        RESUME_LEDGER_NOTES_FIELD: notes,
        RESUME_EXCLUDE_FIELD: list(excluded_paths(sizing)),
    }
    if credited:
        verdict[RESUME_LEDGER_CREDITED_FIELD] = credited
    revised = [b for b in blocking if b in revision_ids]
    if revised:
        verdict[RESUME_LEDGER_REVISED_FIELD] = revised
    # A revise after the halt mints register rows the halted plan never claimed; the re-plan serves them.
    unclaimed = [r for r in _unclaimed_rows(Path(repo_root), sizing_rel)[1] if r not in out]
    if unclaimed:
        verdict[RESUME_LEDGER_UNCLAIMED_FIELD] = unclaimed
    unserved = _unserved_revise_rows(revised, verdict[RESUME_LEDGER_REMAINING_FIELD], unclaimed, halt, repo_root)
    if unserved:
        verdict[RESUME_LEDGER_UNSERVED_FIELD] = unserved
    if all_landed:
        verdict[RESUME_LEDGER_KIND_FIELD] = ALL_LANDED_HALT_KIND
    elif halt.halted == UNCLAIMED_ROWS_HALT_KIND:
        verdict[RESUME_LEDGER_KIND_FIELD] = UNCLAIMED_ROWS_HALT_KIND
    if halt.halted == COMMIT_HALT_KIND:
        outcome = read_run_outcome(Path(repo_root), halt.run_id)
        if outcome is None:
            raise SizingFireRefused(
                [f"commit halt of run {halt.run_id} has no readable {RUN_OUTCOME_FILE}; its review verdict is unrecorded"]
            )
        verdict["inline_review"] = {**outcome["inline_review"], RESUMED_RUN_ID_KEY: halt.run_id}
    return verdict


def _spine_plan(repo_root: Path, sizing: dict, sizing_rel: str) -> tuple[Optional[str], Optional[str]]:
    """`(repo-relative spine path, None)` minted from a concrete sizing, `(None, None)` when the
    sizing is not concrete, or `(None, refusal)` when the spine cannot be minted.

    A file already at the path is never overwritten: when its rows' writes or checks differ from
    the sizing's, the refusal names the differing rows.
    """
    from coordinator_core.ops.dispatch_emit.inventory_mint import InventoryMintError
    from coordinator_core.ops.dispatch_emit.sizing_m_mint import (
        concrete_acceptance,
        mint_m_spine,
        spine_mismatch,
    )

    rows = concrete_acceptance(sizing)
    if rows is None:
        return None, None
    try:
        text, rel = mint_m_spine(sizing, sizing_rel=sizing_rel, rows=rows, repo_root=repo_root)
    except (InventoryMintError, ValueError) as exc:
        return None, f"spine-direct mint refused: {exc}"
    target = repo_root / rel
    if target.exists():
        bad = spine_mismatch(target.read_text(encoding="utf-8"), text)
        if bad:
            return None, (
                f"{rel.as_posix()} already exists and its rows differ from the sizing's acceptance_rows "
                f"({', '.join(bad)}); delete it to re-mint, or edit the rows to match"
            )
        return rel.as_posix(), None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return rel.as_posix(), None


def _record_refusal(record: dict) -> Optional[str]:
    """Why `record` is malformed, else None; checks shape only, the writers own the semantics."""
    kind = record.get("kind")
    if kind not in _RECORD_KINDS:
        return f"dispatch.ask_gate params.record.kind must be one of {list(_RECORD_KINDS)}"
    if kind == design_route.RECORD_KIND:
        if "apm_ruling" in record:
            return "dispatch.ask_gate params.record.apm_ruling is refused for design_approval; the PM approves a canvas"
        for name in ("board_url", "pm_ruling", "ruling_ref"):
            if not isinstance(record.get(name), str) or not record[name].strip():
                return f"dispatch.ask_gate params.record.{name} must be a non-empty string"
        return None
    for name in ("apm_ruling", "ruling_ref"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            return f"dispatch.ask_gate params.record.{name} must be a non-empty string"
    statement = record.get("statement")
    if statement is not None and (not isinstance(statement, str) or not statement.strip()):
        return "dispatch.ask_gate params.record.statement must be a non-empty string"
    if kind == "xl_exit" and record.get("xl_exit") not in _XL_PICKS:
        return f"dispatch.ask_gate params.record.xl_exit must be one of {list(_XL_PICKS)}"
    return None


def _write_record(repo_root: Path, sizing_rel: str, record: dict) -> dict:
    """Write the ruling through its kind's single writer; `{"ok": bool, "error"?: str}`."""
    # Writer modules import lazily: they sit outside dispatch_emit and are needed only under `record`.
    if record["kind"] == design_route.RECORD_KIND:
        from coordinator_core.ops.dispatch_emit.design_approval import record as write_approval

        reply = write_approval(repo_root, sizing_rel, board_url=record["board_url"])
        if reply.get("exit_code") == 0:
            return {"ok": True, "pm_ruling": record["pm_ruling"], "ruling_ref": record["ruling_ref"]}
        return {"ok": False, "error": str(reply.get("error") or "record refused")}
    base = {"sizing": sizing_rel, "apm_ruling": record["apm_ruling"], "ruling_ref": record["ruling_ref"]}
    if record["kind"] == "exit_criterion":
        from coordinator_core.ops.sizing_accept_exit_criterion import _handler as writer

        if record.get("statement"):
            base["statement"] = record["statement"]
    else:
        from coordinator_core.ops.sizing_record_xl_exit import _handler as writer

        base["xl_exit"] = record["xl_exit"]
    reply = writer(base, repo_root)
    if reply.get("exit_code") == 0:
        return {"ok": True}
    return {"ok": False, "error": str(reply.get("error") or reply.get("message") or "record refused")}


@register_op("dispatch.ask_gate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_gate": params `sizing_path` (str, required), `writes` (list[str]), `baton` (str),
    `record` (object: kind exit_criterion|xl_exit, apm_ruling, ruling_ref, statement?, xl_exit?; or kind
    design_approval, board_url, pm_ruling, ruling_ref: the canvas board link and the PM's verbatim approval,
    echoed in `recorded` and not stored),
    `run_id` (str: the warp run asking), `cross_repo_approved` (bool) and `session_id` (str): the stage params
    for a ledger-resume verdict or an S verdict with `resume_plan`, which with `run_id` gains `manifest_handle` (the
    `{path, sha256, bytes}` handle over the `manifest.json` that staging `resume_ledger.plan`, or `resume_plan`
    limited to `resume_rows`, wrote) or, on a stage refusal, `manifest` = `{error}`; no other verdict carries
    either key.

    `require_live_token` (bool): with `run_id`, replies a refusal halt naming the fire call when the run's
    token is not live, before the run binds or spends anything; the script sets it when it holds a token.

    `route_override_reason` (str): why the sizing routes against the run's `route-preference.json`; with a
    `run_id` whose record holds a preference it is written to `em_analysis`, and a divergence with no reason
    on the call or the sizing replies a refusal halt. Every other bound reply, halted or armed, carries
    `route_verdict` (`route_preference.route_verdict`: pushback with the reason on a divergence, independent
    with the sizing's `premise.evidence` on a match) when the run's preference names a route.

    With `run_id`, the run is bound to the sizing first (`_bind_run`), whatever the verdict, and a bound
    run's reply carries `sizing_path`: the sizing the run is bound to, repo-relative and forward-slash
    (absent without `run_id` and on a refused bind). A non-halted verdict that minted a baton records its
    path in the run's `run-baton.json` (a refused write leaves the run unrecorded, the verdict standing). With
    `record`, writes that ruling through its single writer, then gates the sizing as it stands and
    adds `recorded` {ok, error?, pm_ruling?, ruling_ref?} to the reply; a refused write still returns the verdict. Replies
    `GateVerdict.to_json()`; a malformed request replies `{"error": str}` and writes nothing.
    """
    if repo_root is None:
        return {"error": "dispatch.ask_gate requires repo_root"}
    refusal = validate_params("dispatch.ask_gate", params, _PARAMS)
    if refusal is not None:
        return refusal
    record = params.get("record")
    if record is not None:
        refusal = _record_refusal(record)
        if refusal is not None:
            return {"error": refusal}
    sizing_rel = params["sizing_path"]
    writes = params.get("writes") or []
    root = main_worktree_root(Path(repo_root))
    declared = [w for w in params.get("declared_writes") or [] if isinstance(w, str) and w.strip()]
    if declared:
        writes = list(dict.fromkeys([*writes, *declared]))
        if params.get("run_id"):
            import json as _json
            from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused, run_dir as _run_dir

            try:
                _dir = _run_dir(root, params["run_id"])
                _dir.mkdir(parents=True, exist_ok=True)
                (_dir / "declared_writes.json").write_text(_json.dumps(declared), encoding="utf-8")
            except (LedgerRefused, OSError):
                pass
    if params.get("run_id") or record is not None:
        from coordinator_core.ops.dispatch_emit import requirements_ledger

        try:
            held = _held_lock_refusal(root, requirements_ledger._sizing_path(root, sizing_rel))
        except requirements_ledger.LedgerRefused:
            held = None
        if held is not None:
            return _halt(HALT_REFUSAL, held).to_json()
    route_verdicts: list = []
    if params.get("run_id"):
        # A refire's refusal returns before the verdict writes its own snapshot; the prior attempt's must not outlive it.
        _clear_resume_snapshot(root, params["run_id"])
    if params.get("run_id") and params.get("require_live_token"):
        from coordinator_core.ops.dispatch_emit import run_token

        if not run_token.is_live(root, params["run_id"]):
            _mark_token_spent(root, params["run_id"])
            return _halt(
                HALT_REFUSAL,
                f"run token for {params['run_id']} is spent or gone (a run that halted or ended no longer "
                "holds one), so this refire could land nothing. This run is retired; the call starts a "
                f"new run id with a new token: {fire_command(sizing_rel)}",
            ).to_json()
    if params.get("run_id"):
        refused = _bind_run(
            root, params["run_id"], sizing_rel, route_override_reason=params.get("route_override_reason"),
            verdict_sink=route_verdicts,
        )
        if refused is not None:
            return _halt(HALT_REFUSAL, refused).to_json()
    recorded = _write_record(root, sizing_rel, record) if record is not None else None
    landed: list = []
    kept: list = []
    phases: list = []
    held: list = []
    reasons: list = []
    verdict = gate(
        root, sizing_rel, writes=writes, baton=params.get("baton"),
        mint_design_rows=bool(params.get("run_id")), landed_sink=landed, run_id=params.get("run_id"),
        kept_sink=kept, phase_sink=phases, held_sink=held, reasons_sink=reasons,
    )
    if landed and verdict.halt is None and verdict.resume_plan and verdict.resume_rows:
        _stamp_landed_rows(root, verdict.resume_plan, landed)
    if params.get("run_id"):
        _record_resume_snapshot(
            root, params["run_id"], verdict, *(kept[0] if kept and verdict.halt is None else ((), None)),
            held=held, reasons=reasons,
        )
        # XS carries the caller's baton (validated in _arm_verdict) instead of minting one.
        claimed = verdict.baton["path"] if verdict.baton else (
            params.get("baton") if verdict.arm == ARM_XS else None
        )
        if verdict.halt is None and claimed:
            try:
                write_run_baton(root, params["run_id"], claimed)
            except (OSError, ValueError):
                pass
    out =(replace(verdict, recorded=recorded) if recorded is not None else verdict).to_json()
    if phases and verdict.halt is None:
        out[phase_ledger.RESUME_PHASES_FIELD] = phases[0]
    if route_verdicts:
        from coordinator_core.ops.dispatch_emit import route_preference as rp

        out[rp.ROUTE_VERDICT_KEY] = route_verdicts[0]
    if params.get("run_id"):
        from coordinator_core.ops.dispatch_emit import run_token

        out["sizing_path"] = run_token.normalize_sizing_rel(root, sizing_rel)
    if params.get("run_id") and verdict.halt is None:
        if verdict.resume_ledger is not None:
            unlanded = list(verdict.resume_ledger.get(RESUME_LEDGER_UNLANDED_FIELD) or [])
            plan_rel, ledger_resume, only_rows = verdict.resume_ledger["plan"], True, unlanded
            out["resume_ledger"][RESUME_LEDGER_ARM_FIELD] = verdict.arm
        elif (
            (verdict.arm == ARM_S or (verdict.arm == ARM_M_PLUS and verdict.resume_ready))
            and verdict.resume_plan
            and verdict.resume_rows is not None
        ):
            # An empty resume_rows stages no rows (all landed); None would re-stage every row.
            plan_rel, ledger_resume, only_rows = verdict.resume_plan, False, list(verdict.resume_rows)
        else:
            if verdict.resume_plan and verdict.resume_rows:
                stale = _release_stale_receipt(
                    root, params["run_id"], verdict.resume_rows, verdict.resume_plan
                )
                if stale:
                    out["restage_rows"] = stale
            return out
        try:
            if ledger_resume and verdict.arm in (ARM_XS, ARM_S):
                derived = _derive_resume_rows(root, plan_rel, out["resume_ledger"])
                out["resume_ledger"][RESUME_LEDGER_DERIVED_FIELD] = derived
                only_rows = [*only_rows, *(d for d in derived if d not in only_rows)]
            manifest = stage(
                root,
                run_id=params["run_id"],
                plan_rel=plan_rel,
                writes=writes,
                cross_repo_approved=bool(params.get("cross_repo_approved")),
                session_id=params.get("session_id") or None,
                commit_sizing_rel=sizing_rel,
                ledger_resume=ledger_resume,
                only_rows=only_rows,
            )
            if ledger_resume:
                from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import (
                    PlanRowsUnreadable,
                    plan_rows_serving,
                    remaining_leg_plan,
                )
                from coordinator_core.git.tracked_case import tracked_spelling

                resume_out = out["resume_ledger"]
                try:
                    plan_writes = plan_rows_serving(root / plan_rel, resume_out.get("blocking") or [])
                except PlanRowsUnreadable as exc:
                    raise AskStageError(str(exc)) from exc
                resume_out[RESUME_LEDGER_LEG_FIELD] = remaining_leg_plan(
                    resume_out.get(RESUME_LEDGER_REMAINING_FIELD) or {},
                    resume_out.get(RESUME_LEDGER_NOTES_FIELD) or [],
                    manifest.rows,
                    resume_out.get("blocking") or [],
                    list(manifest.doc_writes),
                    out.get("sizing_path") or sizing_rel,
                    root,
                    plan_writes=plan_writes,
                    arm=verdict.arm,
                    spelling=functools.partial(tracked_spelling, root),
                    exclude=resume_out.get(RESUME_EXCLUDE_FIELD) or (),
                )
                from coordinator_core.ops.dispatch_emit.refire_noop import identical_rehalt_reason

                leg = resume_out[RESUME_LEDGER_LEG_FIELD]
                # A dispatch leg with no writes carries no work unless a note asks for the re-plan
                # leg, and a note releases the predicate on its own.
                rehalt = identical_rehalt_reason(
                    resume_out, manifest.rows, manifest.owed, manifest.gated,
                    derived if verdict.arm in (ARM_XS, ARM_S) else [],
                    leg if leg.get("writes") else None,
                )
                if rehalt is not None:
                    out["halt"] = {"kind": HALT_REFUSAL, "reason": rehalt}
                    return out
            stamp = stamp_plan_executing(root, plan_rel)
            out["manifest_handle"] = reply_sidecar.handle_for(root, record_dir(root, params["run_id"]) / "manifest.json")
            ask_stage_receipt.write_gate(record_dir(root, params["run_id"]), out["manifest_handle"])
            if stamp["outcome"] != "unchanged":
                out[PLAN_EXECUTING_STAMP_KEY] = stamp
            if ledger_resume:
                import json

                from coordinator_core.atomic_replace import atomic_write_bytes
                from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE
                from coordinator_core.session.declared_writes import declare_write

                footprint = record_dir(root, params["run_id"]) / LEG_FOOTPRINT_FILE
                declare_write(str(footprint))
                atomic_write_bytes(
                    footprint,
                    (json.dumps(resume_out[RESUME_LEDGER_LEG_FIELD], indent=2) + "\n").encode("utf-8"),
                )
        except (AskStageError, SizingFireRefused, reply_sidecar.ReplySidecarError) as exc:
            out["manifest"] = {"error": str(exc)}
            if ledger_resume:
                out["resume_ledger"][RESUME_LEDGER_LEG_FIELD] = {
                    "action": LEG_REFUSE, "writes": [], "unwritable": [], "reason": str(exc),
                }
    return out


def _stale_write_rows(root: Path, plan_rel: str, manifest: dict, open_rows: Sequence[str]) -> list[str]:
    """Open staged rows whose recorded writes omit a write the plan row lists now.

    A kept (`kept_from`) or stranded-adopting row replays its own receipt and is skipped. A recorded
    superset (test widening) is not stale. An unreadable plan: `[]`."""
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.plan_tasks_render import load_rows

    try:
        loaded = load_rows((root / plan_rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if loaded.status is not LocateStatus.LOCATED:
        return []

    def _norm(paths) -> set[str]:
        return {str(p).replace("\\", "/") for p in paths or [] if isinstance(p, str)}

    current = {str(r.get("id")): _norm(r.get("writes")) for r in loaded.rows if isinstance(r, dict)}
    wanted = set(open_rows)
    stale = []
    for rec in manifest.get("rows") or []:
        if not isinstance(rec, dict) or rec.get("kept_from") or rec.get("adopts_stranded"):
            continue
        rid = str(rec.get("id"))
        if rid in wanted and not current.get(rid, set()) <= _norm(rec.get("writes")):
            stale.append(rid)
    return stale


def _release_stale_receipt(
    root: Path, run_id: str, open_rows: Sequence[str], plan_rel: Optional[str] = None
) -> list[str]:
    """Open rows the run's recorded manifest neither stages nor accounts for (owed, gated, retired),
    plus staged rows whose recorded writes omit a current plan-row write (`plan_rel` given).

    When there are any, the stage receipt is rewritten as a gate receipt so the next `dispatch.ask_stage`
    on the run re-stages instead of replaying a stale manifest. Absent manifest or
    receipt, or an unreadable manifest: `[]`, nothing touched."""
    try:
        record = record_dir(root, run_id)
        manifest = json.loads((record / ask_stage_receipt.MANIFEST_NAME).read_bytes().decode("ascii"))
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict) or not (record / ask_stage_receipt.RECEIPT_NAME).is_file():
        return []
    accounted = {str(r.get("id")) for key in ("rows", "owed", "gated") for r in manifest.get(key) or [] if isinstance(r, dict)}
    accounted |= {str(i) for i in manifest.get("retired_resume_rows") or []}
    missing = sorted(set(open_rows) - accounted)
    if plan_rel:
        missing = sorted({*missing, *_stale_write_rows(root, plan_rel, manifest, open_rows)})
    if missing:
        ask_stage_receipt.write_gate(record, reply_sidecar.handle_for(root, record / ask_stage_receipt.MANIFEST_NAME))
    return missing


def _derive_resume_rows(root: Path, plan_rel: str, resume_out: dict) -> list[str]:
    """Ids of the rows serving the resume's open ledger items, appended to the plan's spine.

    One `derive_ledger_rows` row per open item; an item already holding an open derived row from an
    earlier gate call reuses it. An underivable item, like a revise-minted item no plan row serves, is
    appended to `resume_out[unserved_rows]` and served by the ledger-replan leg; a derived row that
    cannot be written raises `AskStageError`. A plan outside `docs/plans` (an XS record spine) is written through the same locked
    rewrite `plan_tasks_mutate._add_task` applies.
    """
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.git.tracked_case import tracked_spelling
    from coordinator_core.ops.dispatch_emit import ledger_remaining_paths as lrp
    from coordinator_core.ops.plan_tasks_render import load_rows

    plan = root / plan_rel
    blocking = list(resume_out.get("blocking") or [])
    try:
        loaded = load_rows(plan.read_text(encoding="utf-8"))
        if loaded.status is not LocateStatus.LOCATED:
            raise lrp.PlanRowsUnreadable(f"plan {plan_rel} spine is {loaded.status.name}")
        spine = [r for r in loaded.rows if isinstance(r, dict)]
        serving = {b: lrp.plan_rows_serving(plan, [b]) for b in blocking}
    except (OSError, ValueError, lrp.PlanRowsUnreadable) as exc:
        raise AskStageError(str(exc)) from exc
    reuse: dict[str, str] = {}
    for row in spine:
        rid, item = str(row.get("id") or ""), (row.get("register_rows") or [None])[0]
        if rid.startswith(LEDGER_DERIVED_ROW_PREFIX) and item in blocking and row.get("disposition") == "open":
            reuse.setdefault(str(item), rid)
    unserved = resume_out.get(RESUME_LEDGER_UNSERVED_FIELD) or []
    todo = [b for b in blocking if b not in reuse and b not in unserved]
    rows, underivable = lrp.derive_ledger_rows(
        resume_out.get(RESUME_LEDGER_REMAINING_FIELD) or {},
        resume_out.get(RESUME_LEDGER_NOTES_FIELD) or [],
        todo,
        serving,
        [str(r.get("id")) for r in spine],
        root,
        spelling=functools.partial(tracked_spelling, root),
        exclude=resume_out.get(RESUME_EXCLUDE_FIELD) or (),
    )
    if underivable:
        resume_out[RESUME_LEDGER_UNSERVED_FIELD] = list(dict.fromkeys([*unserved, *underivable]))
    for row in rows:
        row = {**row, "surface": row["writes"][0], "queue_scope": "project"}
        error = _append_spine_row(root, plan_rel, row)
        if error:
            raise AskStageError(f"derived row {row['id']} not written: {error}")
    return [*reuse.values(), *(r["id"] for r in rows)]


def _append_spine_row(root: Path, plan_rel: str, row: dict) -> Optional[str]:
    """Append `row` to the plan's spine; the refusal text, else None."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops import plan_tasks_mutate as ptm

    held = _held_lock_refusal(root, root / plan_rel)
    if held is not None:
        return held
    if (root / plan_rel).resolve().is_relative_to((root / "docs" / "plans").resolve()):
        reply = ptm._add_task(plan_rel, row, root, root)
        return None if reply.get("exit_code") == 0 else str(reply.get("error"))

    def mutate(old: str) -> str:
        found = ptm.locate_fenced_block(old)
        if found.status is not ptm.LocateStatus.LOCATED:
            raise MutateAbort(f"task spine is {found.status.name}")
        rows = ptm._parse_rows_or_abort(found.body, "add-task")
        if any(isinstance(r, dict) and r.get("id") == row["id"] for r in rows):
            raise MutateAbort(f"duplicate task id {row['id']!r}")
        start, end = found.span
        return old[:start] + ptm._dump_rows([*rows, row]) + old[end:]

    try:
        locked_rmw(root / plan_rel, mutate, repo_root=root, timeout=GATE_LOCK_WAIT_SECS)
    except (MutateAbort, LockTimeout, OSError) as exc:
        return str(exc.args[0]) if exc.args else str(exc)
    return None


def _stamp_landed_rows(root: Path, plan_rel: str, landed: Sequence) -> None:
    """Stamp each `(row id, landing sha)` row `coded` with that sha in the plan, so the premise
    check plan-blitz runs over the resumed plan does not re-verify rows a commit already landed.
    A stamp that would break the spine's source check, or an unreadable plan, is not written. No
    git spawn."""
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
    from coordinator_core.ops.dispatch_emit.plan_row_coding import flip_rows_coded
    from coordinator_core.session.claimed_write import replace_text

    plan = root / plan_rel
    try:
        text = plan.read_text(encoding="utf-8")
    except OSError:
        return
    updated = text
    for sha in dict.fromkeys(s for _, s in landed):
        updated, _ = flip_rows_coded(updated, {i for i, s in landed if s == sha}, sha)
    if updated == text:
        return
    if check_plan_tasks_source(updated) is not None and check_plan_tasks_source(text) is None:
        return
    replace_text(plan, updated)


#: Sentinel in a run's record dir: its gate found the run token spent, so the run did no work.
TOKEN_SPENT_FILE = "token-spent.json"


def _mark_token_spent(root: Path, run_id: str) -> None:
    """Leave `TOKEN_SPENT_FILE` in the run's record dir for `retire_token_spent_halt`; best-effort."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, safe_id

    if not safe_id(run_id):
        return
    try:
        target = Path(root) / RECORD_DIR_ROOT / run_id
        target.mkdir(parents=True, exist_ok=True)
        (target / TOKEN_SPENT_FILE).write_text("{}\n", encoding="utf-8")
    except OSError:
        pass


def retire_token_spent_halt(root: Path, sizing_rel: str) -> Optional[str]:
    """Mark `resumed` the halted run of `sizing_rel` whose gate refused for a spent run token, so
    the next emit takes a fresh run id and script. Returns the retired run id, else None.

    Without this a refire re-emits the same id, the gate refuses the same way, and the refusal's own
    remedy loops.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for, halted_run_for, mark_resumed

    for run_id in (halted_run_for(root, sizing_rel), halted_ask_run_for(root, sizing_rel)):
        if not run_id:
            continue
        rd = record_dir(root, run_id)
        if (rd / TOKEN_SPENT_FILE).is_file() and mark_resumed(rd):
            return run_id
    return None


def _clear_resume_snapshot(root: Path, run_id: str) -> None:
    from coordinator_core.ops.dispatch_emit import resume_snapshot

    resume_snapshot.clear(root, run_id)


def _record_resume_snapshot(
    root: Path, run_id: str, verdict: GateVerdict, kept: Sequence[str] = (), kept_run: Optional[str] = None,
    held: Sequence = (), reasons: Sequence = (),
) -> None:
    """Write the run's resume snapshot (the plan's row ids minus `resume_rows`, plus `held`) for a row
    or a ledger resume, else clear it, so a snapshot from an earlier gate call never outlives its
    verdict. Every spine entry counts, owed and gated rows included. One plan read, no git spawn."""
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit import resume_snapshot
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

    plan_rel = verdict.resume_plan or ((verdict.resume_ledger or {}).get("plan") if verdict.resume_ledger else None)
    row_resume = bool(verdict.resume_plan and verdict.resume_rows)
    if verdict.halt is None and plan_rel and (row_resume or verdict.resume_ledger):
        plan = root / plan_rel
        try:
            result = load_rows_memo(plan.read_text(encoding="utf-8"))
        except OSError:
            result = None
        if result is not None and result.status is LocateStatus.LOCATED:
            ids = {r["id"] for r in result.rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
            # A ledger resume stages exactly its unlanded rows; `ask_stage` reads this snapshot for any `only_rows`
            # call, so an empty resume set would drop every row from the stage.
            rows = verdict.resume_rows or tuple((verdict.resume_ledger or {}).get(RESUME_LEDGER_UNLANDED_FIELD) or ())
            not_to_run = ids - set(rows)
            resume_snapshot.write(
                root, run_id, plan_rel, not_to_run, rows, kept=kept, kept_run=kept_run, held=held,
                reasons={i: r for i, r in reasons if i in not_to_run},
            )
            return
    resume_snapshot.clear(root, run_id)


def _bind_run(
    root: Path, run_id: str, sizing_rel: str, route_override_reason: Optional[str] = None,
    verdict_sink: Optional[list] = None,
) -> Optional[str]:
    """Bind the run to the sizing it is gating: the sizing's requirement_register takes the PM's
    words, the raw ask (a relayed ask under its own origin) and any queued revision
    (`requirements_ledger.bind`), and the run's token record names the sizing, so
    `run_token.run_owns_sizing` claims it and nothing else. The refusal text when the
    requirements cannot be recorded, else None."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_TEXT_FILE, RELAYED_ASK_FILE

    def _text(path: Path) -> Optional[str]:
        return path.read_text(encoding="utf-8") if path.is_file() else None

    try:
        run_dir = requirements_ledger.run_dir(root, run_id)
        requirements_ledger.bind(
            root, run_id, sizing_rel,
            ask_text=_text(run_dir / ASK_TEXT_FILE), relayed_text=_text(run_dir / RELAYED_ASK_FILE),
        )
    except (requirements_ledger.LedgerRefused, OSError) as exc:
        return f"requirements: {exc}"
    refused = _bind_route_preference(root, run_id, sizing_rel, route_override_reason, verdict_sink)
    if refused is not None:
        return refused
    run_token.bind_sizing(root, run_id, sizing_rel)
    return None


def _bind_route_preference(
    root: Path, run_id: str, sizing_rel: str, route_override_reason: Optional[str],
    verdict_sink: Optional[list] = None,
) -> Optional[str]:
    """Record the run's EM route preference on the sizing's `em_analysis`; the refusal text when the
    sizing routes against it with no stated reason, else None. Writes nothing when the sizing already
    holds the values, and nothing for a run with no `route-preference.json`. `verdict_sink`, when
    given, receives `route_preference.route_verdict` for the sizing as read, written or not; it
    stays empty on a refusal or a failed write."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger, route_preference as rp

    record = requirements_ledger.run_dir(root, run_id) / rp.ROUTE_PREFERENCE_FILE
    if not record.is_file():
        return None
    try:
        pref = rp.from_json(record.read_text(encoding="utf-8"))
    except OSError:
        return None
    if pref.is_empty():
        return None
    reason = route_override_reason.strip() if isinstance(route_override_reason, str) else ""

    import yaml

    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    def mutate(old: str) -> str:
        doc = yaml.safe_load(old) or {}
        em = doc.get("em_analysis")
        em = dict(em) if isinstance(em, dict) else {}
        route = effective_route(doc)
        held = em.get(rp.ROUTE_OVERRIDE_REASON_KEY)
        held = held.strip() if isinstance(held, str) else ""
        want = {**em, **rp.to_em_analysis(pref)}
        if isinstance(route, str) and route and rp.diverges(pref, route):
            if not (reason or held):
                raise MutateAbort(
                    f"route_override_reason: the sizing routes {route} against route_preference "
                    f"{pref.route_preference}; state why in route_override_reason."
                )
            if reason:
                want[rp.ROUTE_OVERRIDE_REASON_KEY] = reason
        if verdict_sink is not None:
            premise = doc.get("premise")
            evidence = premise.get("evidence") if isinstance(premise, dict) else None
            found = rp.route_verdict(
                pref, route, reason=reason or held or None,
                basis=evidence if isinstance(evidence, str) else None,
            )
            verdict_sink[:] = [found] if found is not None else []
        if want == em:
            return old
        doc["em_analysis"] = want
        crlf = "\r\n" in old
        text = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True, width=100000)
        text = requirements_ledger._validated(text)
        return text.replace("\n", "\r\n") if crlf else text

    try:
        locked_rmw(
            requirements_ledger._sizing_path(root, sizing_rel), mutate, repo_root=Path(root),
            timeout=GATE_LOCK_WAIT_SECS,
        )
    except MutateAbort as exc:
        if verdict_sink is not None:
            verdict_sink.clear()
        return str(exc.args[0]) if exc.args else "route preference write refused"
    except (LockTimeout, requirements_ledger.LedgerRefused, OSError) as exc:
        if verdict_sink is not None:
            verdict_sink.clear()
        return f"route_preference: {exc}"
    return None
