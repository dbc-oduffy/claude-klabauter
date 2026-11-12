"""dispatch.ask_stage: a plan or an XS sizing -> brief files, manifest.json and commit request.

Working files land under `RUN_DIR_ROOT/<run_id>/`: `briefs/<row>.md` (the plan route's row prompt) and
`commit-request.txt` (the `render_marker` line, empty when no row declares a path). Run records land
under `ask_contract.record_dir`: the minted spine, `pm-intent.md` and `manifest.json` (a `StageManifest` plus `head_sha`, ASCII JSON written by `reply_sidecar.write_reply`; the op replies the `reply_sidecar.handle_for` handle over it, never the manifest) and `stage-dirt.json` (`stage_dirt_snapshot`: the tree's dirt at stage, read by review prep). In-process file writes only; no git, no subprocess, so the
op holds the 500ms bar. Trap: unlike `compose_script`, the per-row pathspec is not
gitignore-filtered, because that filter spawns git.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Optional, Sequence

from coordinator_core.bash_guards import leg_attribution
from coordinator_core.bash_guards.leg_attribution import leg
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.testing.suite_run_record import read_head_sha
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import (
    ask_stage_receipt,
    design_route,
    reply_sidecar,
    stage_family,
    stage_family_binding,
)
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_TEXT_FILE,
    HALT_PARKED,
    PARK_SOURCE_REPLY_KEY,
    PARKED_REPLY_KEY,
    PM_INTENT_FILE,
    REGISTER_ROWS_UNCLAIMED_REFUSAL,
    RELAYED_ASK_FILE,
    RELAYED_ASK_HEADING,
    ROWS_DROPPED_WITHOUT_REASON,
    RUN_DIR_ROOT,
    SPINE_SUFFIX,
    STOP_AFTER_FILE,
    STOP_AFTER_PLAN,
    GatedRow,
    ManifestRow,
    OwedRow,
    StageManifest,
    delivery_note_rel,
    record_dir,
    stamp_plan_phase,
    stamp_run_stage,
    stop_after_plan_live,
    stop_after_source,
    rulable_ids,
)
from coordinator_core.ops.dispatch_emit.agent_route import RouteAdmissionError, RouteTableError, route_rows
from coordinator_core.ops.dispatch_emit.premise_gate import EPISTEMIC_PREMISE, PREMISE_RECORD_BRIEF_LINE
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
    gated_rows,
    slot_withheld_rows,
    own_gate_owners,
    provisioned_sibling_roots,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.credit_gate import (
    DOCS_CHUNK_ID,
    KEPT_REVIEW_CHUNK_ID,
    SIZING_CHUNK_ID,
)
from coordinator_core.ops.dispatch_emit import write_baseline as _write_baseline
from coordinator_core.ops.dispatch_emit.halted_run import halted_run_base
from coordinator_core.ops.dispatch_emit.fanout_cost import FanoutCost, Unit, propose_groups
from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import (
    REVIEWER_AGENT_TYPE,
    review_inputs,
    review_specs,
)
from coordinator_core.ops.dispatch_emit.emit import (
    resolve_agent_model,
    _EXECUTOR_AGENT_TYPE,
    _dispatch_report_path,
    _plan_deliverable_id,
    _dedupe_preserve_order,
    _AGENT_MODELS,
    _model_opt,
    _plan_id,
    _row_agent_type,
    _row_prompt,
    _widen_with_test_candidates,
    check_unschedulable_rows,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.pathspec import (
    NoTestTargetError,
    NoWritesDeclaredError,
    commit_pathspec_or_none,
    terminal_test_scope,
)
from coordinator_core.ops.dispatch_emit.inventory_mint import strip_literal_pathspec
from coordinator_core.ops import scratch_delete
from coordinator_core.ops.requirement_register import pin_drift_reason, plan_pin_drift
from coordinator_core.ops.review_findings_ledger import LedgerError, targets_add
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit import halt_tree_evict, kept_work, requirements_ledger, resume_snapshot, row_returns, sibling_split
from coordinator_core.ops.dispatch_emit.land_run import _DELIVERED_REPORT_RE, _UNDELIVERED_REPORT_RE
from coordinator_core.ops.dispatch_emit.self_dr_discharge import discharge_clauses
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS, OUTSIDE_CREDIT_REASON, SizingFireRefused, load_sizing, resolve_arm
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine
from coordinator_core.ops.dispatch_emit.spine_read import (
    EM_OWNED_REASONS,
    InvalidChangeKindError,
    NON_DISPATCHABLE_DISPOSITIONS,
    SpineReadError,
    read_spine,
    with_canonical_disposition,
)
from coordinator_core.ops.dispatch_emit import review_floor, run_notes
from coordinator_core.ops.dispatch_emit.requirements_ledger import NOTES_FILE
from coordinator_core.ops.dispatch_emit.wave_map import (
    _RUNTIME_ORDER_GATE_KINDS,
    build_waves,
    cap_waves,
    dag_from_waves,
)
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops._brief_premise import check_brief_claims, render_contradicted
from coordinator_core.ops.sizing_intake_contract import read_em_brief
from coordinator_core.telemetry import op_row_fields

# Writes per-run staging files (manifest, commit-request) under a run dir, not a stamped artifact.
GENERATES = []

_PARAMS = (
    Field("run_id", "str", required=True),
    Field("plan_path", "str"),
    Field("sizing_path", "str"),
    Field("commit_sizing_path", "str"),
    Field("writes", "str_list"),
    Field("gated", "list"),
    Field("cross_repo_approved", "bool"),
    Field("session_id", "str"),
    Field("only_rows", "str_list"),
    Field("claim_register_rows", "bool"),
)

__all__ = ["AskStageError", "stage", "scaffold_markers", "pm_intent_block", "relayed_ask_block", "manifest_payload"]

PM_INTENT_HEADING = "## PM intent (verbatim)"


_HARNESS_RELAY_MARKERS = (
    "[Workflow harness",
    "was relayed from the driving session",
    "<system-reminder>",
)


def _is_harness_relay(text: str) -> bool:
    """True when `text` is a harness-composed resume relay, not the PM's words."""
    head = text.lstrip()
    return any(head.startswith(m) for m in _HARNESS_RELAY_MARKERS) or _HARNESS_RELAY_MARKERS[1] in head[:400]


def pm_intent_block(sizing: Optional[Mapping], ask_text: Optional[str]) -> str:
    """The brief's head: the PM's own words (sizing `pm_verbatims`, else a PM-verbatim `intent`,
    plus the ask as submitted), the exit criterion, and the precedence rule. Never the EM's
    paraphrase: an `em-elaborated` intent is left out. Empty when no PM words are on record."""
    sizing = sizing or {}
    quotes = requirements_ledger.pm_words(sizing)
    if ask_text and ask_text.strip() and not _is_harness_relay(ask_text) and ask_text.strip() not in quotes:
        quotes.append(ask_text.strip())
    if not quotes:
        return ""
    lines = [PM_INTENT_HEADING, ""]
    for q in quotes:
        lines.extend("> " + ln if ln else ">" for ln in q.splitlines())
        lines.append("")
    criterion = sizing.get("exit_criterion")
    statement = criterion.get("statement") if isinstance(criterion, Mapping) else None
    if isinstance(statement, str) and statement.strip():
        lines.extend([f"Exit criterion: {statement.strip()}", ""])
    lines.append(
        "These words are the outcome you are held to; design the change that makes each of them "
        "literally true. Where the brief below, a plan, a memo or in-repo doctrine reads narrower "
        "than these words or against them, these words win: keep the ask and name the conflict in "
        "your report."
    )
    return "\n".join(lines) + "\n"


def relayed_ask_block(relayed_text: Optional[str]) -> str:
    """The brief's relayed-ask section, below the PM-intent block and never inside it: an EM's
    relay of the ask, stated as not the PM's words. Empty when nothing was relayed."""
    if not relayed_text or not relayed_text.strip():
        return ""
    lines = [RELAYED_ASK_HEADING, ""]
    for ln in relayed_text.strip().splitlines():
        lines.append("> " + ln if ln else ">")
    lines.extend([
        "",
        "An agent relayed this ask; it is not the PM's own wording. Serve it, and where the PM's "
        "verbatim words above (or in the sizing) read differently, the PM's words outrank it.",
    ])
    return "\n".join(lines) + "\n"


REVISIONS_HEADING = "## Revisions sent while the run was in flight"


def revisions_block(root: Path, run_id: str) -> str:
    """The brief's section of every register row a `workflow.revise` added to the run, in arrival
    order: the ledger judges each, so an executor not told of one can only fail it. Empty when none."""
    try:
        ledger = requirements_ledger.read_ledger(root, requirements_ledger.run_dir(root, run_id))
    except requirements_ledger.LedgerRefused:
        return ""
    items = [i for i in ledger.items if i.origin == requirements_ledger.ORIGIN_REVISION] if ledger else []
    if not items:
        return ""
    lines = [REVISIONS_HEADING, ""]
    for item in items:
        tag = ", ".join(p for p in (item.at, f"from {item.sender}" if item.sender else "") if p)
        lines.append(f"{item.id} ({tag}):" if tag else f"{item.id}:")
        lines.extend("> " + ln if ln else ">" for ln in item.text.strip().splitlines())
        lines.append("")
    lines.append(
        "These are requirements of this run, relayed by an EM or peer, not the PM's words. Deliver "
        "each one your row serves; a later revision governs over an earlier item it changes."
    )
    return "\n".join(lines) + "\n"


def em_brief_block(sizing: Optional[Mapping], root: Optional[Path] = None) -> str:
    """The brief's EM-diagnosis section (the sizing's `em_analysis.em_brief`), marked as not the
    PM's words and placed below the PM-intent and relayed-ask blocks. Empty when none is recorded.
    With ``root``, the brief's tree-contradicted claims follow as CONTRADICTED lines."""
    brief = read_em_brief(sizing) if isinstance(sizing, Mapping) else None
    if not brief:
        return ""
    block = f"## EM diagnosis (not the PM's words)\n\n{brief}\n"
    if root is not None:
        flagged = render_contradicted(check_brief_claims(brief, root))
        if flagged:
            block += f"\n{flagged}\n"
    return block


def _run_sizing_rel(sizing_rel: Optional[str], plan_text: str) -> Optional[str]:
    if sizing_rel:
        return sizing_rel
    split = split_frontmatter(plan_text)
    return read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None


#: Plan-row dispositions that are this run's requirement (the plan's `do` grouping).
_MINTED_DISPOSITIONS = ("open", "coded", "delivered_elsewhere")


def _run_sizing(root: Path, rel: Optional[str]) -> Optional[dict]:
    if not rel:
        return None
    try:
        return load_sizing(root, rel)
    except (SizingFireRefused, OSError, ValueError):
        return None


DELIVERY_NAMED_NOTE = re.compile(r"DELIVERY[^/]*\.md", re.IGNORECASE)
_DOC_TARGET_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#.*)$")


def doc_writes(run_id: str, sizing: Optional[Mapping]) -> tuple[str, ...]:
    """The docs stage's declared write set: the run's delivery note, then every path the sizing
    names in `doc_consumers` and `ask_conflicts` (a `:<line>` or `#<anchor>` suffix stripped),
    plus any `DELIVERY*.md` the sizing's `writes` names (the delivery note the ask asked for, at
    its named path). A sizing or plan path is never one -- those stay process records."""
    sizing = sizing or {}
    raw = [p for p in sizing.get("doc_consumers") or [] if isinstance(p, str)]
    raw += [
        c.get("path") for c in sizing.get("ask_conflicts") or []
        if isinstance(c, Mapping) and isinstance(c.get("path"), str)
    ]
    raw += [
        w for w in sizing.get("writes") or []
        if isinstance(w, str) and DELIVERY_NAMED_NOTE.match(PurePosixPath(w.strip()).name)
    ]
    from coordinator_core.session.record_homes import home_dir

    process = (PurePosixPath(home_dir(".", "sizings")).as_posix() + "/", "docs/plans/")
    out = [delivery_note_rel(run_id)]
    for entry in raw:
        rel = PurePosixPath(_DOC_TARGET_SUFFIX.sub("", entry.strip()).replace("\\", "/")).as_posix()
        if (
            rel and rel != "." and not PurePosixPath(rel).is_absolute() and ".." not in PurePosixPath(rel).parts
            and not rel.startswith(process) and rel not in out
        ):
            out.append(rel)
    return tuple(out)


def _reviewable(declared: Sequence[str]) -> tuple[str, ...]:
    """The declared paths a reviewer can read a product diff for. Bookkeeping and scratch writes
    (audits, state, plans) freeze no reviewable diff, so a run declaring only those has an empty
    list and the composed script skips the review wave instead of halting at review prep."""
    from coordinator_core.ops.review_stamp import PRODUCT_FILE_EXCLUDED_PREFIXES

    skip = (*PRODUCT_FILE_EXCLUDED_PREFIXES, "scratch/")
    return tuple(p for p in declared if not p.replace("\\", "/").removeprefix("./").startswith(skip))


def _widen_with_existing_tests(paths: Sequence[str], root: Path) -> list[str]:
    """`paths` verbatim, plus each stem-derived test candidate that is a file in the tree.
    A candidate absent from the tree is never emitted: only a declared path may name a file to be created."""
    declared = list(paths)
    keep = set(declared)
    return [
        p for p in _widen_with_test_candidates(declared, root)
        if p in keep or (root / strip_literal_pathspec(p)).is_file()
    ]


def _test_scope(waves, root: Path) -> tuple[tuple[str, ...], Optional[dict]]:
    """(test targets, None), or ((), the typed no-target record) when the locator maps no
    testable write to a runnable target -- e.g. a .ts-only spine in a repo whose tests it cannot
    see. Never a crash and never a silent pass: the run reads its tests as degraded."""
    try:
        return tuple(terminal_test_scope(waves, repo_root=root)), None
    except NoTestTargetError as exc:
        return (), {
            "reason": str(exc),
            "unmapped_paths": list(exc.unmapped_paths),
            "testable_omissions": list(exc.testable_omissions),
        }
    except NoWritesDeclaredError as exc:
        return (), {"reason": str(exc), "unmapped_paths": [], "testable_omissions": []}


def _wave_test_scopes(waves, root: Path) -> dict[str, tuple[str, ...]]:
    """Cumulative declared test scope per non-last wave, keyed by the wave number rows carry.
    A wave whose cumulative scope is empty has no key: nothing to wait on before its checkpoint."""
    out: dict[str, tuple[str, ...]] = {}
    for i in range(len(waves) - 1):
        scope = _test_scope(waves[: i + 1], root)[0]
        if scope:
            out[str(i)] = scope
    return out

# Mirrors DoE's coordinator/lib/plan_scaffold_markers.py; claude-klabauter never imports DoE's tree.
_SCAFFOLD_MARKERS = (
    ("PLACEHOLDER", re.compile(r"\bPLACEHOLDER\b")),
    ("<REPLACE:", re.compile(r"<REPLACE:")),
    ("path/to/file", re.compile(r"path/to/file")),
)


def scaffold_markers(text: str) -> dict[str, int]:
    """Marker -> first 1-based line carrying it; `#` YAML comment lines are template docs, skipped."""
    found: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            continue
        for name, rx in _SCAFFOLD_MARKERS:
            if name not in found and rx.search(line):
                found[name] = n
    return found


from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


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


def _premise_row_ids(raw_by_id: dict) -> frozenset:
    """Rows another row names in an epistemic-premise `depends_on` edge. Their briefs carry the record
    line: a premise row that finishes without a verdict leaves its dependents unjudged (run 6467a3)."""
    ids = set()
    for raw in raw_by_id.values():
        edges = raw.get("depends_on") if isinstance(raw, dict) else None
        for edge in edges if isinstance(edges, list) else []:
            if isinstance(edge, dict) and edge.get("gate_kind") == EPISTEMIC_PREMISE and edge.get("chunk"):
                ids.add(edge["chunk"])
    return frozenset(ids)


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


def _with_owner_roots(gated: Sequence[GatedRow]) -> list[GatedRow]:
    """Fill each gated row's ``owner_root`` from the machine-local registry, one lookup per
    distinct ``owner_repo``; ``""`` when unregistered. No process is spawned."""
    gated = list(gated)
    owners = {g.owner_repo for g in gated if g.owner_repo}
    if not owners:
        return gated
    from coordinator_core.machine_resolver import registry_get
    from coordinator_core.ops.fleet._memo_resolver import receiver_em_to_repo_key

    roots = {o: registry_get(receiver_em_to_repo_key(o)) or "" for o in owners}
    return [replace(g, owner_root=roots.get(g.owner_repo, "")) for g in gated]


def _engine_rows(
    root: Path,
    plan_path: Path,
    raw_by_id: Mapping[str, Mapping],
    gated: Sequence[GatedRow],
    run_sizing: Optional[Mapping],
    sibling_roots: Optional[Mapping[str, Path]] = None,
) -> tuple[dict, ...]:
    """Rows delivered in the engine plane, over the plan's ungated raw rows, the run sizing's
    register rows and the plan's evidence sidecar. One text scan and at most one resolver call."""
    from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

    withheld = {g.id for g in gated}
    plan_rows = [r for rid, r in raw_by_id.items() if rid not in withheld]
    register = ((run_sizing or {}).get("requirement_register") or {}).get("rows") or []
    sidecar = evidence_sidecar_path(plan_path)
    try:
        evidence = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""
    except OSError:
        evidence = ""
    rows = engine_owner_rows(plan_rows, [r for r in register if isinstance(r, Mapping)], evidence, root)
    return tuple(rows) + _sibling_owner_rows(plan_rows, sibling_roots or {})


def _sibling_owner_rows(plan_rows: Sequence[Mapping], sibling_roots: Mapping[str, Path]) -> tuple[dict, ...]:
    """One owner-repo row (no ``engine`` flag) per staged plan row gated on a provisioned sibling,
    judged at that sibling's root."""
    out: list[dict] = []
    for raw in plan_rows:
        gates = raw.get("external_gate") if isinstance(raw, Mapping) else None
        for g in gates if isinstance(gates, list) else ():
            owner = g.get("owner_repo") if isinstance(g, Mapping) else None
            if owner in sibling_roots and g.get("cleared") is not True:
                out.append({"id": raw["id"], "owner_repo": owner, "owner_root": sibling_roots[owner].as_posix()})
                break
    return tuple(out)


def _only_rows(
    rows: list, exclusions: list, keep: frozenset, snap: Optional[resume_snapshot.Snapshot] = None
) -> tuple[list, list, tuple[str, ...], list]:
    """The resume subset of a spine read, the resume rows a re-plan retired, and the dispatchable
    rows dropped as landed: `keep` rows only,
    edges onto the dropped (landed) rows stripped, and only `keep` rows among the owed/gated
    exclusions. With `snap` (the gate's snapshot), every known row outside `snap.not_to_run` is
    kept, rows a re-plan added included. A `keep` id the spine lacks but `snap.resume_rows` names
    is retired: reported, never staged, never refused. Any other missing id refuses."""
    known = {r.id for r in rows} | {e.get("id") for e in exclusions}
    missing = keep - known
    retired: tuple[str, ...] = ()
    if snap is not None:
        retired = tuple(sorted(missing & snap.resume_rows))
        missing = missing - snap.resume_rows
    if missing:
        raise AskStageError(f"resume row id(s) not in the plan's spine: {sorted(missing)}")
    if snap is not None:
        keep = frozenset(known - snap.not_to_run)
    landed = known - keep
    kept = []
    dropped = []
    for row in rows:
        if row.id not in keep:
            dropped.append(row)
            continue
        edges = [e for e in row.depends_on if not (isinstance(e, dict) and e.get("chunk") in landed)]
        kept.append(row._replace(depends_on=edges) if len(edges) != len(row.depends_on) else row)
    return kept, [e for e in exclusions if e.get("id") in keep], retired, dropped


def _is_closed_exclusion(e: Mapping) -> bool:
    """A row the plan already counts done: landed by a checkpoint, or closed by a `disposition`."""
    return e.get("reason") in ("checkpoint-landed", "disposition")


_DRAFT_STATUS = re.compile(r"^status:\s*['\"]?draft\b", re.MULTILINE)


def _draft_resume_without_evidence(
    plan_text: str, dropped: Sequence, snap: Optional[resume_snapshot.Snapshot]
) -> bool:
    """True when a resume subset emptied a never-staged (`status: draft`) plan and the gate recorded
    no reason for any row it dropped. A draft plan has no run history, so an unexplained drop is
    not evidence of landing; a dropped row with a recorded reason stays dropped."""
    if snap is None or not dropped:
        return False
    split = split_frontmatter(plan_text)
    if split is None or _DRAFT_STATUS.search(split.fm_text) is None:
        return False
    return not any(snap.reasons.get(r.id) for r in dropped)


def _resume_dropped_lines(
    full_rows: Sequence, full_exclusions: Sequence, rows: Sequence, exclusions: Sequence,
    snap: Optional[resume_snapshot.Snapshot],
) -> list[str]:
    """`<id> (<reason>)` for each spine row or exclusion the resume subset removed, so a zero-row
    refusal names rows the gate read as landed or closed."""
    kept = {r.id for r in rows} | {e.get("id") for e in exclusions}
    by_exclusion = {e.get("id"): e for e in full_exclusions}
    out: list[str] = []
    for rid in dict.fromkeys([*(r.id for r in full_rows), *by_exclusion]):
        if rid in kept:
            continue
        e = by_exclusion.get(rid) or {}
        reason = (snap.reasons.get(rid) if snap else None) or " ".join(
            str(x) for x in (e.get("reason"), e.get("detail")) if x
        ) or "outside only_rows"
        out.append(f"{rid} ({reason})")
    return out


def _drop_reminted_em_rows(plan_path: Path, raw_by_id: Mapping[str, Mapping], keep: frozenset) -> tuple[str, ...]:
    """Ids of a ledger re-plan's em-performed rows whose register rows an em-discharged
    em-performed row already serves; the caller omits them. A row with no register_rows, itself
    discharged, or chained to a discharged row is never returned."""
    from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed
    from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

    discharged = read_em_discharges(plan_path)
    if not discharged:
        return ()
    served: set[str] = set()
    for row_id in discharged:
        raw = raw_by_id.get(str(row_id))
        if raw is not None and row_is_em_performed(raw):
            served.update(str(x) for x in raw.get("register_rows") or ())
    dupes = []
    for row_id in sorted(keep):
        raw = raw_by_id.get(row_id)
        if raw is None or row_id in discharged or not row_is_em_performed(raw):
            continue
        regs = {str(x) for x in raw.get("register_rows") or ()}
        if regs and regs <= served and not _reaches(raw_by_id, row_id, discharged):
            dupes.append(row_id)
    return tuple(dupes)


def _reaches(raw_by_id: Mapping[str, Mapping], start: str, targets) -> bool:
    """True when `start`'s transitive depends_on chunk edges reach any id in `targets`."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        raw = raw_by_id.get(stack.pop())
        for edge in (raw or {}).get("depends_on") or ():
            chunk = edge.get("chunk") if isinstance(edge, Mapping) else edge
            if not isinstance(chunk, str) or chunk in seen:
                continue
            if chunk in targets:
                return True
            seen.add(chunk)
            stack.append(chunk)
    return False


_UNKNOWN_RECEIVER_MARK = "UNKNOWN RECEIVER"


def _unknown_receiver_detail(to: str) -> str:
    return (
        f"receiver {to!r} is unknown to the registry, memo.draft refuses it as {_UNKNOWN_RECEIVER_MARK}; "
        "post it as a PR/issue comment and close the row with delivered_via other and an http(s) "
        "disposition_ref"
    )


def _demote_unregistered_memo_rows(
    rows: list,
    owed: list,
    raw_by_id: Mapping[str, Mapping],
    classify_receiver: Optional[Callable[[str, bool], object]] = None,
) -> tuple[list, list]:
    """Rows that stay dispatchable, and `owed` plus every memo-send row whose receiver the registry
    refuses and every row transitively behind one. One classification per memo row."""
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_receiver_unregistered

    root_to: dict[str, str] = {}
    for row in rows:
        fields = getattr(row, "memo_send", None)
        to = fields.get("to") if isinstance(fields, Mapping) else None
        if to and memo_receiver_unregistered(to, classify_receiver):
            root_to[row.id] = to
    if not root_to:
        return rows, owed

    def _preds(row) -> list[str]:
        return [e.get("chunk") if isinstance(e, Mapping) else e for e in row.depends_on]

    root_of: dict[str, str] = dict(zip(root_to, root_to))
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.id in root_of:
                continue
            for pred in _preds(row):
                if pred in root_of:
                    root_of[row.id] = root_of[pred]
                    changed = True
                    break
    new_owed = list(owed)
    for row in rows:
        root = root_of.get(row.id)
        if root is None:
            continue
        if row.id == root:
            new_owed.append(OwedRow(id=row.id, reason="memo-send", detail=_unknown_receiver_detail(root_to[root])))
        else:
            new_owed.append(
                OwedRow(
                    id=row.id,
                    reason="withheld_by_owed_dependency",
                    detail=f"behind memo-send row {root}, whose receiver {root_to[root]!r} the registry refuses",
                )
            )
    return [r for r in rows if r.id not in root_of], new_owed


def _declared_delete_brief_clause(run_id: str, row_id: str, writes: Sequence[str]) -> str:
    """The declared-delete marker clause for one executor brief; empty while the channel is dormant
    or when every declared write is under scratch/. Every executor brief route appends this."""
    from coordinator_core.bash_guards import _declared_delete_license
    from coordinator_core.bash_guards import block_subagent_destructive_action as guard

    if not guard._DECLARED_DELETE_CHANNEL_LIVE:
        return ""
    if all(str(w).replace("\\", "/").startswith("scratch/") for w in writes):
        return ""
    return _declared_delete_license.brief_clause(run_id, row_id)


def _memo_draft_steps(
    root: Path,
    run_dir: Path,
    owed: Sequence[OwedRow],
    raw_by_id: Mapping[str, Mapping],
    intent: str,
    staged: Sequence[ManifestRow] = (),
    classify_receiver: Optional[Callable[[str, bool], object]] = None,
) -> tuple[list[OwedRow], list[ManifestRow]]:
    """Does the outbox hold the draft each owed `memo-send` row's send needs? One dispatched draft
    step per row whose draft is absent and whose receiver is named; the row stays owed for the EM's
    send either way, its detail saying why no step was staged. It stages a draft for a row the EM
    still sends: `performer: em`, or a receiver the registry refuses.

    The step inherits the owed row's `depends_on` edges that name a `staged` row, and sits one wave
    after the latest of them: the draft carries content those rows produce."""
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_receiver_unregistered
    from coordinator_core.ops.dispatch_emit.spine_read import memo_draft_fields
    from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR

    out_owed: list[OwedRow] = []
    steps: list[ManifestRow] = []
    for o in owed:
        if o.reason != "memo-send" or _UNKNOWN_RECEIVER_MARK in o.detail:
            out_owed.append(o)
            continue
        raw = raw_by_id.get(o.id) or {}
        fields, why = memo_draft_fields(dict(raw))
        if fields is None:
            out_owed.append(replace(o, detail=f"{o.detail} (no draft step staged: {why})"))
            continue
        if memo_receiver_unregistered(fields["to"], classify_receiver):
            out_owed.append(
                replace(
                    o,
                    detail=f"{o.detail} (no draft step staged: {_unknown_receiver_detail(fields['to'])})",
                )
            )
            continue
        slug = fields["slug"]
        draft_rel = f"{MEMO_OUTBOX_RELDIR}/{slug}.md"
        if (root / draft_rel).is_file() or (root / LEGACY_MEMO_OUTBOX_RELDIR / f"{slug}.md").is_file():
            out_owed.append(o)
            continue
        deps, wave = _staged_dependencies(raw, staged)
        step_id = f"{o.id}-memo-draft"
        brief = run_dir / "briefs" / f"{step_id}.md"
        body = raw.get("brief") or raw.get("body") or ""
        text = (
            f"Stage the outbox draft for memo-send row {o.id}. Run, from the repo root:\n\n"
            f"    cross-repo-memo draft {slug} --to {shlex.quote(fields['to'])} --kind {fields['kind']} "
            f"--title {shlex.quote(fields['title'])}\n\n"
            f"Then Edit the memo body in {draft_rel} to carry the row's content below. Never run "
            "`cross-repo-memo send`: the EM sends. Touch no other file.\n\n"
            f"Row body:\n\n{body}\n"
        )
        delete_clause = _declared_delete_brief_clause(run_dir.name, step_id, (draft_rel,))
        if delete_clause:
            text = text.rstrip() + "\n\n" + delete_clause + "\n"
        _write(brief, (intent + "\n" + text) if intent else text)
        steps.append(
            ManifestRow(
                id=step_id,
                agent_type=_EXECUTOR_AGENT_TYPE,
                model=resolve_agent_model(_EXECUTOR_AGENT_TYPE, None),
                brief_path=_rel(root, brief),
                writes=(draft_rel,),
                wave=wave,
                deps=deps,
            )
        )
        out_owed.append(o)
    return out_owed, steps


def _route_source(row_id: str, routes: Mapping, raw_by_id: Mapping[str, Mapping]) -> str:
    """`route-table` for a class-routed row, `overlay` when the raw plan row names an agent_type, else `default`.

    Reads the raw row: `route_rows` rewrites the parsed row's agent_type, so it cannot show explicitness.
    """
    if row_id in routes:
        return "route-table"
    return "overlay" if (raw_by_id.get(row_id) or {}).get("agent_type") else "default"


def _staged_dependencies(raw: Mapping, staged: Sequence[ManifestRow]) -> tuple[tuple[str, ...], int]:
    """The ids `raw.depends_on` names among `staged`, and the wave one past the latest of them (0 for none)."""
    waves = {r.id: r.wave for r in staged}
    ids: list[str] = []
    for edge in raw.get("depends_on") or ():
        chunk = edge.get("chunk") if isinstance(edge, Mapping) else edge
        if isinstance(chunk, str) and chunk in waves and chunk not in ids:
            ids.append(chunk)
    return tuple(ids), (max(waves[i] for i in ids) + 1 if ids else 0)


def _group_rows(
    rows: Sequence[ManifestRow], sizes: Mapping[str, int], raw_by_id: Mapping[str, Mapping]
) -> tuple[list[ManifestRow], tuple[tuple[str, ...], ...]]:
    """Rows with `group` set where small same-wave rows share one agent, and every agent's member
    ids in proposal order (solo rows included). Reviewer and `needs_slot` rows get a unique key so
    they stay solo; a one-member group leaves `group` empty. Row order is unchanged."""
    units = []
    for r in rows:
        solo = r.agent_type == REVIEWER_AGENT_TYPE or r.family or (raw_by_id.get(r.id) or {}).get("needs_slot") is True
        key = f"solo:{r.id}" if solo else f"{r.wave}|{','.join(sorted(r.deps))}|{r.agent_type}|{r.model}|{r.effort or ''}"
        units.append(Unit(r.id, key, sizes.get(r.id, 0)))
    groups = propose_groups(sorted(units, key=lambda u: u.key))
    group_of = {m: f"g{next(r.wave for r in rows if r.id == g[0])}-{g[0]}" for g in groups if len(g) > 1 for m in g}
    return [replace(r, group=group_of.get(r.id, "")) for r in rows], groups


def _refuse_unapproved_design(sizing: Mapping, sizing_rel: str) -> None:
    """Refuse staging under a canvas design_route the PM has not approved."""
    payload, error = design_route.read(sizing, sizing_rel)
    if error:
        raise AskStageError(error)
    refusal = design_route.stage_refusal(payload)
    if refusal:
        raise AskStageError(refusal)


def _sizing_plan_edge(root: Path, sizing_rel: str) -> Optional[str]:
    """The sizing's `plan:` edge when it names an existing plan file under the repo, else None.

    Trap: an unloadable sizing or a dangling edge returns None, leaving the refusal to the sizing_path arm."""
    sizing = _run_sizing(root, sizing_rel)
    edge = sizing.get("plan") if sizing else None
    if not isinstance(edge, str) or not edge.strip():
        return None
    plan_path = contained_path(root / edge.strip(), [root])
    return edge.strip() if plan_path is not None and plan_path.is_file() else None


def _refuse_misrouted_xs_mint(sizing: Mapping, sizing_rel: str, writes: Sequence[str]) -> None:
    """Raise AskStageError `XS-MINT-MISROUTED` for a sizing the XS mint must not take: one with a
    `plan:` edge or a non-XS `estimate.tshirt`; `XS-MINT-NO-WRITES` for an empty `writes`.

    A sizing with no readable tshirt is left to the writes check (the mint itself needs no estimate)."""
    edge = sizing.get("plan")
    has_edge = isinstance(edge, str) and bool(edge.strip())
    try:
        arm = resolve_arm(sizing)
    except SizingFireRefused:
        arm = ARM_XS
    if has_edge or arm != ARM_XS:
        why = f"plan edge {edge!r}" if has_edge else f"arm {arm!r}"
        raise AskStageError(
            f"XS-MINT-MISROUTED: {sizing_rel} has {why}; stage it by plan_path, not sizing_path"
        )
    if not any(str(w).strip() for w in writes):
        raise AskStageError(f"XS-MINT-NO-WRITES: {sizing_rel} is an XS arm and needs at least one writes path")


_EXCLUSION_CUE = re.compile(
    r"\b(?:do not|don't|dont|must not|never|should not|shall not)\s+(?:touch|modify|edit|change|write|stage|commit|revert)\b"
    r"|\bnot to be (?:touched|modified|edited|changed)\b|\bleave\b[^.\n]*\balone\b|\bout of scope\b",
    re.IGNORECASE,
)
_PATH_TOKEN = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+/?|[\w\-]+\.\w+")


def _excluded_by_ask(sizing: Mapping, writes: Sequence[str]) -> dict[str, str]:
    """Map each of `writes` the sizing's intent or register rows name inside a do-not-touch sentence to that sentence (first match).

    A path matches by equality, or by a directory token it sits under. Trap: lexical only; a path
    described in prose without being spelled out is not caught."""
    texts: list[str] = [str(sizing.get("intent") or "")]
    register = sizing.get("requirement_register")
    for row in register if isinstance(register, list) else ():
        if isinstance(row, Mapping):
            texts.extend(str(v) for v in row.values() if isinstance(v, str))
    named: dict[str, str] = {}
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            if _EXCLUSION_CUE.search(sentence):
                for t in _PATH_TOKEN.findall(sentence):
                    named.setdefault(t.rstrip(".,;:)\"'`").lstrip("(\"'`"), sentence.strip())
    out: dict[str, str] = {}
    for w in writes:
        norm = str(w).replace("\\", "/")
        for n, sentence in named.items():
            if n and (norm == n or (n.endswith("/") and norm.startswith(n))):
                out[w] = sentence
                break
    return out


LIVE_RUN_WRITE_OVERLAP = "live-run-write-overlap"
STAGE_RECEIPT_MISSING = "stage-receipt-missing"
STAGE_CLAIM_STALLED = "stage-claim-stalled"
LIVE_OVERLAP_DEADLINE_S = 0.5


_STAMP_DATE_RE = re.compile(r"^(?P<head>.*-)\d{8}(?P<tail>T\d{6}-[0-9a-f]{6})$")


def _recorded_run_ids(root: Path, hits: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """`hits` re-keyed by each run's exact recorded id (the run-token record's), so a pasted
    `dispatch.release_run_token` finds it. A hit id with no record whose only difference from exactly
    one held record is the stamp's date digits maps to that record; any other id stays as given."""
    from coordinator_core.ops.dispatch_emit import run_token

    try:
        held = [r["run_id"] for r in run_token.held_records(root)]
    except (OSError, ValueError, KeyError, TypeError):
        return dict(hits)
    known = set(held)
    out: dict[str, list[str]] = {}
    for rid, paths in hits.items():
        exact = rid
        match = None if rid in known else _STAMP_DATE_RE.match(rid)
        if match is not None:
            near = {
                h for h in known
                if (m := _STAMP_DATE_RE.match(h)) and m["head"] == match["head"] and m["tail"] == match["tail"]
            }
            if len(near) == 1:
                exact = next(iter(near))
        out.setdefault(exact, []).extend(p for p in paths if p not in out.get(exact, ()))
    return out


def _chain_kin(root: Path, run_id: str, candidates: Iterable[str]) -> set[str]:
    """The `candidates` that are `run_id`'s own chain: runs bound to the same sizing (a chained
    run's `chained_from` ancestor and descendants carry the sizing it hands on) and any run on its
    `resumed_from` chain. Their claims are the same work, never a peer's overlap.
    Trap: a run with no bound sizing has no kin by sizing."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        tokens = {r.get("run_id"): r.get("sizing_rel") for r in run_token.held_records(root)}
    except (OSError, ValueError, KeyError, TypeError):
        tokens = {}

    def sizing_of(rid: str) -> Optional[str]:
        try:
            bound = (rl.read_run(record_dir(root, rid)) or {}).get("sizing_rel") or tokens.get(rid)
        except ValueError:
            bound = tokens.get(rid)
        return str(bound).replace("\\", "/") if bound else None

    mine = sizing_of(run_id)
    chain: set[str] = set()
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(record_dir(root, cur)) or {}).get(rl.RESUMED_FROM_FIELD)
        except ValueError:
            break
        if not (isinstance(prev, str) and prev and prev not in chain and prev != run_id):
            break
        chain.add(prev)
        cur = prev
    return {rid for rid in candidates if rid in chain or (mine is not None and sizing_of(rid) == mine)}


STAGE_WAIT_PARKED = "stage-wait"


class StageWaiting(AskStageError):
    """The stage's writes overlap only live engine runs: `wait` is its `stage_wait` record, its run token
    stays live, and the blocker's end wakes it. A subclass of `AskStageError` so a caller that does not
    know it still reports a stop; `_run_ask_stage` turns it into the non-halting `parked` reply."""

    def __init__(self, message: str, wait: dict) -> None:
        super().__init__(message)
        self.wait = wait


def release_remedy(run_ids: Sequence[str]) -> str:
    """The `dispatch.release_run_token` call per run id, `; `-joined: what the EM runs for a blocker it
    knows is dead (a TaskStop-killed run keeps its token live for the full TTL)."""
    return "; ".join(f'dispatch.release_run_token {{"run_id": "{rid}"}}' for rid in sorted(run_ids))


def _refuse_live_write_overlap(root: Path, run_id: str, writes: Sequence[str]) -> Optional[dict]:
    """Return None when no other live run's staged footprint intersects `writes`. When every
    intersecting run is a live run of this engine (`run_token.is_live`) and this run has no expired
    wait, park instead: keep the token and return the `stage_wait` record naming them. Otherwise raise
    AskStageError `live-run-write-overlap` naming each such run, and release this run's token so the
    refusal ends the run.
    The check and the footprint claim are one locked index mutation (`live_run_index.claim`), so two
    stages cannot both pass. When the claim cannot be made (no lock, stalled store, no common dir) or
    outlives LIVE_OVERLAP_DEADLINE_S, it degrades to the read-only `live_footprints` check, and a read
    outliving the deadline skips the check with a logged advisory: the pre-check never aborts a stage.
    Trap: a claim that outlives the deadline keeps running on its daemon thread and may still write
    markers after this call returns or after `consume` released them; `over()` pruning reaps them."""
    import logging
    import threading

    from coordinator_core.ops.dispatch_emit import live_run_index, live_run_writes, run_token, stage_wait

    def within_deadline(fn):
        box: list = []

        def call() -> None:
            try:
                box.append(fn())
            except BaseException as exc:  # noqa: BLE001 - surfaced by the caller
                box.append(exc)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(LIVE_OVERLAP_DEADLINE_S)
        return box

    claimed = within_deadline(lambda: live_run_index.claim(root, run_id, writes))
    if claimed and isinstance(claimed[0], BaseException):
        raise claimed[0]
    hits = claimed[0] if claimed else None
    if hits is None:
        box = within_deadline(lambda: live_run_writes.live_footprints(root, exclude_run_id=run_id))
        if not box:
            logging.getLogger(__name__).warning(
                "live-run-write-overlap check skipped: footprint read exceeded %ss", LIVE_OVERLAP_DEADLINE_S
            )
            return None
        if isinstance(box[0], BaseException):
            raise box[0]
        hits = {
            rid: paths
            for rid, paths in live_run_writes.overlaps(writes, box[0]).items()
            if not live_run_index.run_is_stale(root, rid)
        }
    if hits:
        hits = _recorded_run_ids(root, hits)
        kin = _chain_kin(root, run_id, hits)
        hits = {rid: paths for rid, paths in hits.items() if rid not in kin}
    if not hits:
        stage_wait.clear(root, run_id)
        return None
    named = "; ".join(f"{rid}: {', '.join(paths)}" for rid, paths in sorted(hits.items()))
    release = release_remedy(list(hits))
    if all(run_token.is_live(root, rid) for rid in hits) and not stage_wait.expired(root, run_id):
        prior = stage_wait.read(root, run_id)
        if prior is not None and prior.get("started"):
            # A started record's marker would make the next wake's O_EXCL claim lose.
            stage_wait.clear(root, run_id)
        owner = run_token._read_rec(root, run_id) or {}
        parked = stage_wait.park(root, run_id, sorted(hits), writes, session_id=owner.get("session_id") or "")
        if parked is not None:
            return parked
    stage_wait.clear(root, run_id)
    run_token.consume(root, run_id)
    raise AskStageError(
        f"{LIVE_RUN_WRITE_OVERLAP}: writes overlap live run {named}. Resubmit when it lands, "
        f"or release a run that has ended: {release}."
    )


KEPT_HUNKS_DRIFTED = "kept-hunks-drifted"


def _kept_review_paths(
    root: Path,
    run_id: str,
    record: Path,
    resume_snap: Optional[resume_snapshot.Snapshot],
    declared: Sequence[str],
) -> tuple[str, ...]:
    """The sorted post-checkpoint paths a halted run recorded (this run's record dir, and the resumed
    run's when it differs) whose bytes still equal the record and no staged row declares.
    Raises AskStageError `kept-hunks-drifted` naming each recorded path whose bytes moved.
    Trap: reads kept.json directly, never `snap.kept` -- a record holding only `post_checkpoint` keeps no row."""
    dirs = [record]
    if resume_snap is not None and resume_snap.kept_run and resume_snap.kept_run != run_id:
        dirs.append(record_dir(root, resume_snap.kept_run))
    owned = {kept_work._norm(p) for p in declared}
    holding: set[str] = set()
    drifted: dict[str, str] = {}
    for rdir in dirs:
        held, moved = kept_work.kept_post_checkpoint(root, rdir)
        holding.update(held)
        recorded = kept_work.read(rdir)[kept_work.POST_CHECKPOINT_KEY]
        drifted.update({p: recorded.get(p, "") for p in moved})
    holding -= owned
    for p in owned:
        drifted.pop(p, None)
    if drifted:
        named = "; ".join(f"{p} (recorded {b})" for p, b in sorted(drifted.items()))
        raise AskStageError(f"{KEPT_HUNKS_DRIFTED}: recorded post-checkpoint hunks changed since the halt: {named}. Restore the recorded bytes (or land them), then re-fire.")
    return tuple(sorted(holding))


CLAIM_LOCK_WAIT_SECS = 0.08  # inside the 500ms budget; expiry replies PLAN-LOCK-BUSY


def _claim_register_rows(root: Path, plan_path: Path) -> None:
    """Claim into `plan_path`'s `register_claims.rows` every row of the register it names that they omit.
    A plan with no readable claims or register is left as it is."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops import requirement_register as rr

    split = split_frontmatter(plan_path.read_text(encoding="utf-8"))
    sizing_rel, _ =rr.plan_claims(split.fm_text) if split is not None else (None, frozenset())
    sizing = contained_path(root / sizing_rel, [root]) if sizing_rel else None
    register = None
    if sizing is not None:
        try:
            register = rr.read_register(sizing.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            register = None
    if register is None:
        return

    raw = plan_path.read_bytes()
    crlf = b"\r\n" in raw
    try:
        unchanged = rr.claim_rows_plan_text(raw.decode("utf-8").replace("\r\n", "\n"), register)[0] == raw.decode("utf-8").replace("\r\n", "\n")
    except (ValueError, UnicodeDecodeError):
        unchanged = False
    if unchanged:
        return

    def mutate(old: str) -> str:
        try:
            new = rr.claim_rows_plan_text(old.replace("\r\n", "\n"), register)[0]
        except ValueError as exc:
            raise MutateAbort(str(exc)) from exc
        return new.replace("\n", "\r\n") if crlf else new

    try:
        locked_rmw(plan_path, mutate, repo_root=root, timeout=CLAIM_LOCK_WAIT_SECS)
    except MutateAbort:
        return
    except LockTimeout as exc:
        raise AskStageError(
            f"PLAN-LOCK-BUSY: _claim_register_rows waited {CLAIM_LOCK_WAIT_SECS}s for the lock on "
            f"{_rel(root, plan_path)}; another writer holds it. Re-fire dispatch.ask_stage."
        ) from exc


def _missing_register_rows(
    fm_text: str, run_sizing_rel: Optional[str], run_sizing: Optional[Mapping]
) -> tuple[Optional[str], list]:
    """`(claimed_sizing, ids)`: the row ids of the register the plan claims against that its
    `register_claims.rows` omit and no ruling retires. `ids` is empty when the plan claims
    against another sizing than the run's, or carries no register."""
    from coordinator_core.ops import requirement_register as rr

    claimed_sizing, claimed = rr.plan_claims(fm_text)
    cited = read_fm_field_unquoted(fm_text, "sizing_object")
    if not claimed_sizing or claimed_sizing != rr._plan_key(cited) or claimed_sizing != rr._plan_key(run_sizing_rel):
        return claimed_sizing, []
    rows = [
        r for r in ((run_sizing or {}).get("requirement_register") or {}).get("rows") or []
        if isinstance(r, Mapping)
    ]
    ruled_out = [
        r.get(rr.ROW_ID) for r in rows
        if r.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED) and not rr._unruled(r)
    ]
    return claimed_sizing, list(rr.unclaimed_ids(rows, claimed, exclude=ruled_out))


def _settle_unclaimed_register_rows(
    root: Path, plan_path: Path, fm_text: str, plan_posix: str,
    run_sizing_rel: Optional[str], run_sizing: Optional[Mapping],
) -> None:
    """Claim into the plan every register row it omits, re-checking after each claim; raise
    AskStageError REGISTER-ROWS-UNCLAIMED only once MAX_IN_RUN_REMEDY_ATTEMPTS claims left a row omitted."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS, remedy_or_halt

    _, missing = _missing_register_rows(fm_text, run_sizing_rel, run_sizing)
    if not missing:
        return

    def claim(_n: int) -> None:
        nonlocal fm_text, missing
        _claim_register_rows(root, plan_path)
        split = split_frontmatter(plan_path.read_text(encoding="utf-8"))
        fm_text = split.fm_text if split is not None else fm_text
        _, missing = _missing_register_rows(fm_text, run_sizing_rel, run_sizing)
        if missing:
            raise RuntimeError(f"register row(s) {', '.join(missing)} still omitted")

    halt = remedy_or_halt(
        REGISTER_ROWS_UNCLAIMED_REFUSAL, claim,
        lambda _f: AskStageError(
            f"{REGISTER_ROWS_UNCLAIMED_REFUSAL}: {plan_posix} omits register row(s) "
            f"{', '.join(missing)} in register_claims.rows after {MAX_IN_RUN_REMEDY_ATTEMPTS} in-run claims. "
            f"{'; '.join(_f)}"
        ),
    )
    if halt is not None:
        raise halt


def stage(
    repo_root: Path,
    *,
    run_id: str,
    plan_rel: Optional[str] = None,
    sizing_rel: Optional[str] = None,
    writes: Sequence[str] = (),
    gated: Sequence[Mapping] = (),
    cross_repo_approved: bool = False,
    session_id: Optional[str] = None,
    commit_sizing_rel: Optional[str] = None,
    only_rows: Optional[Sequence[str]] = None,
    ledger_resume: bool = False,
    claim_register_rows: bool = False,
    classify_receiver: Optional[Callable[[str, bool], object]] = None,
    plan_parse: Optional["_PlanParse"] = None,
) -> StageManifest:
    """Stage the run for `plan_rel`, or for the XS sizing `sizing_rel` (minted to X1 over `writes`).

    `only_rows` (a resume: the halted plan's incomplete row ids) stages just those rows; the rest
    are landed, so an edge onto one is satisfied. A resume row the re-plan retired is reported in the
    manifest's `retired_resume_rows`, not refused. A resume whose rows are all owed or gated
    returns a manifest with no rows and the owed/gated lists.
    `ledger_resume=True` (an all-landed plan resumed at its ledger) admits a spine with zero
    dispatchable rows and returns a whole manifest with rows []; the default refuses it.
    `claim_register_rows=True` (a ledger re-plan) first adds every register row the plan's
    `register_claims.rows` omit to its claims and re-pins them, so the plan the re-plan revised
    claims the rows a later revise minted.
    `plan_parse` is the handler's one parse of an unmodified full plan; it is ignored under
    `claim_register_rows`, which rewrites the plan first.
    """
    root = Path(repo_root)
    if not safe_id(run_id):
        raise AskStageError(f"run_id {run_id!r} is not a single safe path segment")
    if bool(plan_rel) == bool(sizing_rel):
        raise AskStageError("name exactly one of plan_rel and sizing_rel")
    if sizing_rel:
        planned = _sizing_plan_edge(root, sizing_rel)
        if planned:
            plan_rel, commit_sizing_rel, sizing_rel = planned, sizing_rel, None

    run_dir = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if run_dir is None:
        raise AskStageError(f"run dir for {run_id!r} escapes {RUN_DIR_ROOT}")
    try:
        record = record_dir(root, run_id)
    except ValueError as exc:
        raise AskStageError(str(exc)) from exc

    excluded: dict[str, str] = {}
    if sizing_rel:
        sizing = load_sizing(root, sizing_rel)
        run_sizing_rel, run_sizing = sizing_rel, sizing
        _refuse_unapproved_design(sizing, sizing_rel)
        excluded = _excluded_by_ask(sizing, writes)
        writes = [w for w in writes if w not in excluded]
        _refuse_misrouted_xs_mint(sizing, sizing_rel, writes)
        spine_text, spine_path = mint_xs_spine(
            sizing, sizing_rel=sizing_rel, writes=writes, out_dir=record, gated=gated,
            repo_root=root,
        )
        _write(spine_path, spine_text)
        plan_path = spine_path
    else:
        plan_path = contained_path(root / plan_rel, [root])
        if plan_path is None or not plan_path.is_file():
            raise AskStageError(f"plan not found under the repo: {plan_rel!r}")
    if claim_register_rows and plan_rel:
        _claim_register_rows(root, plan_path)
    shared = plan_parse if (plan_rel and not claim_register_rows) else None
    plan_text = shared.plan_text if shared is not None else plan_path.read_text(encoding="utf-8")
    if not sizing_rel:
        run_sizing_rel = _run_sizing_rel(commit_sizing_rel, plan_text)
        run_sizing = _run_sizing(root, run_sizing_rel)
        # An unloadable sizing_object yields None here and skips the design check (fails open).
        if run_sizing is not None:
            _refuse_unapproved_design(run_sizing, run_sizing_rel)
    plan_posix = _rel(root, plan_path)

    if plan_rel:
        unfilled = scaffold_markers(plan_text)
        if unfilled:
            named = ", ".join(f"{k} (line {v})" for k, v in sorted(unfilled.items()))
            raise AskStageError(f"PLAN-SCAFFOLD-UNFILLED: {plan_posix} still carries {named}")
    try:
        with leg("parse"):
            parse = shared if shared is not None else _parse_plan(root, plan_path, plan_text)
    except InvalidChangeKindError as exc:
        raise AskStageError(f"INVALID-CHANGE-KIND: {plan_posix}: {exc}") from exc
    except SpineReadError as exc:
        digest = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()[:12]
        raise AskStageError(
            f"SPINE-REFUSED: {plan_posix}: {type(exc).__name__}: {exc} "
            f"[parsed the plan text stage read: {len(plan_text)} chars, sha256 {digest}]"
        ) from exc
    raw_by_id, rows, sibling_roots = parse.raw_by_id, list(parse.rows), parse.sibling_roots
    exclusions = list(parse.exclusions)
    retired_resume: tuple[str, ...] = ()
    resume_snap = resume_snapshot.read(root, run_id, plan_posix) if only_rows is not None else None
    if not ledger_resume and resume_snap is None and only_rows is not None and not len(only_rows):
        # An empty resume set with no snapshot names no landed row; the parse already excluded landed and closed rows.
        only_rows = None
    dropped_landed: list = []
    resume_dropped: list[str] = []
    if only_rows is not None:
        full_rows, full_exclusions = rows, exclusions
        rows, exclusions, retired_resume, dropped_landed = _only_rows(
            rows, exclusions, frozenset(only_rows), resume_snap
        )
        if not ledger_resume and not rows and _draft_resume_without_evidence(
            plan_text, dropped_landed, resume_snap
        ):
            rows, exclusions, retired_resume, dropped_landed, resume_snap = full_rows, full_exclusions, (), [], None
        else:
            resume_dropped = _resume_dropped_lines(full_rows, full_exclusions, rows, exclusions, resume_snap)
    ran_premises: dict[str, str] = {}
    if only_rows is not None:
        rows, ran_premises = _settle_ran_premises(rows, raw_by_id, root, plan_posix, run_id)
    if ledger_resume and only_rows is not None:
        reminted = _drop_reminted_em_rows(plan_path, raw_by_id, frozenset(only_rows))
        if reminted:
            rows = [r for r in rows if r.id not in reminted]
            retired_resume = (*retired_resume, *reminted)
    check_unschedulable_rows(rows, raw_by_id)
    _split = split_frontmatter(plan_text)
    _drift = plan_pin_drift(_split.fm_text, root) if _split is not None else []
    if _drift:
        raise AskStageError(pin_drift_reason(_drift))
    gated = _with_owner_roots(gated_rows(exclusions, raw_by_id))
    owed = [
        OwedRow(id=e["id"], reason=e["reason"], detail=e.get("detail") or "")
        for e in exclusions
        if e.get("reason") in EM_OWNED_REASONS
    ]
    rows, owed = _demote_unregistered_memo_rows(rows, owed, raw_by_id, classify_receiver)
    try:
        check_cross_repo_writes(rows, root, approved=cross_repo_approved, session_id=session_id)
    except CrossRepoWriteError as exc:
        raise AskStageError(str(exc)) from exc
    try:
        rows, routes, _route_notes = route_rows(rows, root)
    except (RouteAdmissionError, RouteTableError) as exc:
        raise AskStageError(f"route: {exc}") from exc
    # Split, not refuse: runtime_cap is CPU-dependent and a refusal would halt hands-off runs.
    with leg("waves"):
        waves = cap_waves(build_waves(rows), sibling_split.runtime_cap())
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_close_only

    blocking_drops = _criterion_blocking_drops(root, exclusions)
    memo_close = memo_close_only(exclusions, raw_by_id)
    closing_ids = {c["id"] for c in memo_close}
    memo_close_run = bool(memo_close) and not gated and all(o.id in closing_ids for o in owed)
    if not waves and not ledger_resume and not memo_close_run and not (only_rows is not None and (owed or gated)):
        named = f"; gated: {', '.join(g.id for g in gated)}" if gated else ""
        if owed:
            named += f"; owed by the EM: {', '.join(o.id for o in owed)}"
        plan_held = [e for e in exclusions if e.get("reason") == "depends_on_plan"]
        if plan_held:
            named += "; waiting on another plan: " + ", ".join(
                f"{e['id']} ({e.get('detail') or ''})" for e in plan_held
            )
        already = {g.id for g in gated} | {o.id for o in owed} | {e["id"] for e in plan_held}
        left = [e for e in exclusions if e.get("id") not in already]
        done = [e for e in left if _is_closed_exclusion(e)]
        closed = [e for e in left if e not in done]
        if closed:
            named += "; excluded: " + ", ".join(
                f"{e['id']} ({e.get('reason')}: {e.get('detail') or ''})" for e in closed
            )
        if done:
            named += "; already closed: " + ", ".join(
                f"{e['id']} [{e.get('detail') or e.get('reason')}]" for e in done
            )
        seen = already | {e["id"] for e in left}
        resume_only = [line for line in resume_dropped if line.split(" ", 1)[0] not in seen]
        if resume_only:
            named += "; dropped by the resume set: " + ", ".join(resume_only)
        if blocking_drops:
            _record_staging_drops(record, run_id, [{"id": d["id"], "reason": d["reason"]} for d in blocking_drops])
        refusal = AskStageError(
            f"the spine derives zero dispatchable rows{named}",
            owed_em_ids=[o.id for o in owed],
            plan_path=plan_posix,
            gated_rows=[
                {"id": g.id, "owner_repo": g.owner_repo}
                for g in gated
                if g.reason == "external_gate" and g.owner_repo
            ],
            criterion_blocking_drops=blocking_drops,
        )
        from coordinator_core.ops.dispatch_emit.memo_rows import memo_row_slug
        from coordinator_core.ops.dispatch_emit.spine_read import memo_close_route

        # Trap: AskStageError (ask_stage_blocks) has no constructor slot for this; the reply reads it by getattr.
        refusal.owed_memo_rows = [
            {"id": o.id, "route": memo_close_route(o.id, memo_row_slug(raw_by_id.get(o.id) or {}))}
            for o in owed
            if o.reason == "memo-send"
        ]
        raise refusal
    if blocking_drops:
        _record_staging_drops(record, run_id, [{"id": d["id"], "reason": d["reason"]} for d in blocking_drops])
        raise AskStageError(
            f"{CRITERION_UNREACHABLE_AT_STAGING}: {plan_posix} cannot reach its exit criterion; unstaged: "
            + ", ".join(
                f"{d['id']} ({d['reason']}; fire {d['predecessor_sizing'] or d['predecessor_plan'] or 'its predecessor'} first)"
                for d in blocking_drops
            ),
            plan_path=plan_posix,
            criterion_blocking_drops=blocking_drops,
        )

    with leg("context"):
        context = derive_plan_context(
            plan_text, fallback_title=plan_path.stem, repo_root=root.as_posix()
        )
        clauses = _discharge_clauses_for(plan_text, root, list(raw_by_id.values()))

    ask_file = record / ASK_TEXT_FILE
    ask_text = ask_file.read_text(encoding="utf-8") if ask_file.is_file() else None
    pm_block = pm_intent_block(run_sizing, ask_text)
    if pm_block:
        _write(record / PM_INTENT_FILE, pm_block)
    relayed_file = record / RELAYED_ASK_FILE
    relayed_text = relayed_file.read_text(encoding="utf-8") if relayed_file.is_file() else None
    relayed = relayed_ask_block(relayed_text)
    intent = pm_block + ("\n" if pm_block and relayed else "") + relayed
    if run_sizing_rel:
        # The gate bound it already; idempotent, and it folds in a revision queued since.
        try:
            requirements_ledger.bind(
                root, run_id, run_sizing_rel, ask_text=ask_text, relayed_text=relayed_text,
                plan_rows=[
                    (rid, str(raw.get("title") or ""))
                    for rid, raw in raw_by_id.items()
                    if raw.get("disposition", "open") in _MINTED_DISPOSITIONS
                ],
            )
        except requirements_ledger.LedgerRefused as exc:
            raise AskStageError(f"requirements: {exc}") from exc
        run_sizing = _run_sizing(root, run_sizing_rel) or run_sizing
        revisions = revisions_block(root, run_id)
        if revisions:
            intent = intent + ("\n" if intent else "") + revisions
    if plan_rel and not ledger_resume and _split is not None:
        _settle_unclaimed_register_rows(root, plan_path, _split.fm_text, plan_posix, run_sizing_rel, run_sizing)
    em_block = em_brief_block(run_sizing, root)
    if em_block:
        intent = intent + ("\n" if intent else "") + em_block
    with leg("test_scope"):
        test_targets, no_test_target = _test_scope(waves, root)
        wave_test_targets = _wave_test_scopes(waves, root)

    after = {node.row.id: tuple(node.after) for node in dag_from_waves(waves).nodes}
    premise_ids = _premise_row_ids(raw_by_id)
    manifest_rows: list[ManifestRow] = []
    chunks: list[ChunkCommit] = []
    declared: list[str] = []
    brief_sizes: dict[str, int] = {}
    kept_ids: set[str] = set()
    try:
        sizing_family = stage_family.resolve((run_sizing or {}).get("family"))
    except ValueError as exc:
        raise AskStageError(f"sizing {run_sizing_rel or ''}: {exc}") from exc
    binding = None
    try:
        if stage_family_binding.opted_in(run_sizing or {}):
            if (run_sizing or {}).get("family"):
                raise AskStageError(
                    f"sizing {run_sizing_rel or ''} names both family and stage_family_binding; keep one"
                )
            binding = stage_family_binding.load()
    except (ValueError, stage_family_binding.BindingError) as exc:
        if isinstance(exc, AskStageError):
            raise
        raise AskStageError(f"sizing {run_sizing_rel or ''}: {exc}") from exc
    with leg("briefs"):
        for wave_no, wave in enumerate(waves):
            for row in wave:
                agent_type = _row_agent_type(row)
                row_family = "claude"
                role_family = ""
                if sizing_family != "claude" and not row.agent_type and agent_type == _EXECUTOR_AGENT_TYPE:
                    agent_type, row_family = stage_family.RUNNER_AGENT, sizing_family
                elif (
                    binding is not None
                    and row.stage_role
                    and not row.agent_type
                    and agent_type == _EXECUTOR_AGENT_TYPE
                ):
                    bound = binding.roles.get(row.stage_role)
                    if bound is None:
                        raise AskStageError(
                            f"row {row.id}: stage_role {row.stage_role!r} has no family in the stage-family binding"
                        )
                    if bound != stage_family.FALLBACK:
                        agent_type, role_family = stage_family.RUNNER_AGENT, bound
                if row.agent_model and agent_type in _AGENT_MODELS:
                    row = row._replace(agent_model=None)  # a pinned type takes its pin; the override is dropped
                _model_opt(agent_type, row.agent_model)  # raises on bad model grammar of an unpinned override
                model = resolve_agent_model(agent_type, row.agent_model)
                brief = run_dir / "briefs" / f"{row.id}.md"
                report_rel = _dispatch_report_path(plan_posix, row.id)
                kept_from = (
                    resume_snap.kept_run
                    if agent_type != REVIEWER_AGENT_TYPE and _kept_row_holds(root, resume_snap, row.id, report_rel)
                    else ""
                )
                if kept_from:
                    kept_ids.add(row.id)
                    if role_family:
                        agent_type, role_family = _EXECUTOR_AGENT_TYPE, ""
                        model = resolve_agent_model(agent_type, None)
                prior = None if kept_from else _set_aside_prior_report(root, report_rel)
                if kept_from:
                    text = _kept_brief(kept_from)
                elif agent_type == REVIEWER_AGENT_TYPE:
                    text = _reviewer_brief(root, run_dir, row.id, raw_by_id.get(row.id) or {})
                else:
                    text = _row_prompt(row, plan_posix, context)
                    if row_family != "claude":
                        paths = [w for w in row.writes if isinstance(w, str)] if isinstance(row.writes, list) else []
                        text = stage_family.runner_brief(
                            row_family, "write" if paths else "other", _rel(root, brief),
                            f"scratch/family-stage/{row.id}", paths,
                        ) + "\n" + text
                    if intent:
                        text = intent + "\n" + text
                    if row.id in clauses:
                        text = text.rstrip() + "\n\n" + clauses[row.id] + "\n"
                    scratch_clause = scratch_delete.brief_clause(run_id, row.id, row.writes)
                    if scratch_clause:
                        text = text.rstrip() + "\n\n" + scratch_clause + "\n"
                    delete_clause = _declared_delete_brief_clause(run_id, row.id, row.writes)
                    if delete_clause:
                        text = text.rstrip() + "\n\n" + delete_clause + "\n"
                    if prior:
                        text = text.rstrip() + "\n\n" + _prior_report_line(prior) + "\n"
                    if row.id in premise_ids:
                        text = text.rstrip() + "\n\n" + PREMISE_RECORD_BRIEF_LINE + "\n"
                    for dep in after.get(row.id, ()):
                        if dep in kept_ids:
                            line = _persisted_return_line(root, resume_snap.kept_run, dep)
                            if line:
                                text = text.rstrip() + "\n\n" + line
                fallback_rel = fallback_agent = ""
                if role_family and not kept_from:
                    fallback_file = run_dir / "briefs" / f"{row.id}.fallback.md"
                    _write(fallback_file, text)
                    fallback_rel, fallback_agent = _rel(root, fallback_file), _EXECUTOR_AGENT_TYPE
                    text = (
                        stage_family.runner_brief(
                            role_family, row.stage_role, _rel(root, brief), f"scratch/family-stage/{row.id}"
                        )
                        + "\n"
                        + text
                    )
                _write(brief, text)
                brief_sizes[row.id] = len(text.encode("utf-8"))
                paths = _widen_with_existing_tests(commit_pathspec_or_none([row]) or [], root)
                declared.extend(paths)
                manifest_rows.append(
                    ManifestRow(
                        id=row.id,
                        agent_type=agent_type,
                        model=model,
                        effort=row.effort,
                        brief_path=_rel(root, brief),
                        writes=tuple(paths),
                        wave=wave_no,
                        deps=after.get(row.id, ()),
                        route=routes[row.id].to_json() if row.id in routes else None,
                        route_source=_route_source(row.id, routes, raw_by_id),
                        register_rows=tuple(str(r) for r in (raw_by_id.get(row.id) or {}).get("register_rows") or ()),
                        kept_from=kept_from,
                        adopts_stranded=_adopts_stranded(raw_by_id.get(row.id)),
                        family=role_family if fallback_rel else "",
                        stage_role=row.stage_role if fallback_rel else "",
                        fallback_brief_path=fallback_rel,
                        fallback_agent_type=fallback_agent,
                    )
                )
                chunks.append(
                    ChunkCommit(
                        id=row.id,
                        title=row.title,
                        paths=tuple(paths),
                        prefixes=tuple(row.writes_under),
                        report=_dispatch_report_path(plan_posix, row.id),
                    )
                )

    manifest_rows, row_groups = _group_rows(manifest_rows, brief_sizes, raw_by_id)
    owed, draft_rows = _memo_draft_steps(root, run_dir, owed, raw_by_id, intent, manifest_rows, classify_receiver)
    manifest_rows.extend(draft_rows)
    fanout = FanoutCost(
        units=len(manifest_rows),
        agents=len(row_groups) + len(draft_rows),
        basis="exact",
        groups=(*row_groups, *((r.id,) for r in draft_rows)),
    )

    held_ids: tuple[str, ...] = ()
    if ledger_resume:
        held_ids = derive_held_ids(
            raw_by_id,
            _recorded_manifest(record),
            [r.id for r in manifest_rows],
            tuple(resume_snap.not_to_run) if resume_snap else (),
        )
    dropped_rows: tuple[dict[str, str], ...] = ()
    if only_rows is not None:
        dropped_rows = _unstaged_row_reasons(
            root=root,
            ran_premises=ran_premises,
            raw_by_id=raw_by_id,
            parse_exclusions=parse.exclusions,
            staged={r.id for r in manifest_rows},
            reported={
                *(o.id for o in owed),
                *(g.id for g in gated),
                *retired_resume,
                *(str(s.get("id")) for s in slot_withheld_rows(raw_by_id)),
                *(str(c.get("id")) for c in memo_close),
                *held_ids,
                *(h[0] for h in (resume_snap.held if resume_snap else ())),
            },
            resume_snap=resume_snap,
            plan_posix=plan_posix,
        )

    docs = doc_writes(run_id, run_sizing)
    with leg("overlap"):
        from coordinator_core.ops.dispatch_emit import live_run_writes

        waiting = _refuse_live_write_overlap(
            root,
            run_id,
            live_run_writes.file_claims(root, [*declared, *docs, *(w for r in draft_rows for w in r.writes)]),
        )
    if waiting is not None:
        raise StageWaiting(
            f"{STAGE_WAIT_PARKED}: writes overlap live run {', '.join(waiting['blockers'])}; parked until it lands "
            f"or is released; a blocker known dead: {release_remedy(waiting['blockers'])}.",
            waiting,
        )
    # The docs stage writes only these; terminal_commit holds them to the chunk test code rows get.
    chunks.append(ChunkCommit(id=DOCS_CHUNK_ID, title="run docs", paths=docs))
    kept_review = _kept_review_paths(root, run_id, record, resume_snap, declared)
    if kept_review:
        chunks.append(ChunkCommit(id=KEPT_REVIEW_CHUNK_ID, title="kept post-checkpoint hunks", paths=kept_review))
    carried_sizing = sizing_rel or commit_sizing_rel
    if carried_sizing:
        # The run flips the sizing to routed and links its plan; no row declares it.
        chunks.append(
            ChunkCommit(id=SIZING_CHUNK_ID, title="sizing record", paths=(carried_sizing,))
        )

    branch = head_branch(root)
    marker = render_marker(
        CommitRequest(
            chunks=tuple(chunks),
            deliverable_id=_plan_deliverable_id(plan_text),
            repo_root=root.as_posix(),
            plan_path=plan_posix,
            expected_branch=None if branch in (None, "HEAD") else branch,
        )
    )
    marker_path = run_dir / "commit-request.txt"
    _write(marker_path, f"{marker}\n" if marker else "")

    review_paths = [*declared, *kept_review]
    if only_rows is not None and not ledger_resume:
        # Twin of emit.py prior_landed_rows -> prior_surface. Trap: the review base is the halted
        # run's base, so a dropped row landed before that base adds no hunk.
        for row in dropped_landed:
            review_paths.extend(_widen_with_existing_tests(commit_pathspec_or_none([row]) or [], root))

    register_rows = [
        r for r in ((run_sizing or {}).get("requirement_register") or {}).get("rows") or [] if isinstance(r, Mapping)
    ]
    ledger_item_ids, withheld_ids = requirements_ledger.ledger_ids(register_rows)
    review_declared = _reviewable(_dedupe_preserve_order(review_paths))
    review_code = review_floor.code_paths(review_declared)
    if ledger_resume and only_rows is not None:
        # A resume's landed rows stay out of the declared set but still owe the review floor.
        review_code = _dedupe_preserve_order(
            [*review_code, *review_floor.code_paths(commit_pathspec_or_none(dropped_landed) or [])]
        )
    with leg("baseline"):
        write_baseline = _stage_write_baseline(root, manifest_rows, record, run_sizing_rel)
    manifest = StageManifest(
        run_dir=_rel(root, run_dir),
        rows=tuple(manifest_rows),
        review_declared_paths=review_declared,
        review_code_paths=tuple(review_code),
        marker_path=_rel(root, marker_path),
        plan_id=_plan_id(plan_text),
        gated=tuple(gated),
        owed=tuple(owed),
        engine_rows=_engine_rows(root, plan_path, raw_by_id, gated, run_sizing, sibling_roots),
        test_targets=test_targets,
        no_test_target=no_test_target,
        wave_test_targets=wave_test_targets,
        fanout=fanout.meta_dict(),
        doc_writes=docs,
        rulable_ids=without_held_rows(rulable_ids(register_rows), held_ids, raw_by_id),
        ledger_item_ids=ledger_item_ids,
        withheld_ids=withheld_ids,
        retired_resume_rows=retired_resume,
        dropped_writes=tuple({"path": p, "sentence": s} for p, s in excluded.items()),
        write_baseline=write_baseline,
        slot_withheld=tuple(slot_withheld_rows(raw_by_id)),
        memo_close=memo_close if not manifest_rows else (),
        held_ids=held_ids,
        dropped_rows=dropped_rows,
    )
    if dropped_rows:
        _record_staging_drops(record, run_id, dropped_rows)
    with leg("manifest"):
        payload = manifest_payload(root, manifest)
        reply_sidecar.write_reply(root, record / "manifest.json", payload)
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot

    with leg("dirt_snapshot"):
        stage_dirt_snapshot.write_snapshot(root, record)
    from coordinator_core.ops.dispatch_emit import live_run_writes

    with leg("live_index"):
        live_run_writes.index_stage(root, run_id, payload, time.time())
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    try:
        halted_withheld_index.record_declared(
            root, run_id, live_run_writes.manifest_footprint(payload), sizing_rel=run_sizing_rel
        )
    except (OSError, ValueError):
        pass
    stamp_run_stage(root, run_id, "staged", steps_planned=len(manifest_rows))
    if session_id:
        # Confined reviewers' Edits are admitted only for review-targets.txt
        # paths; the plan route registers them at compose time, the ask route
        # only learns them here. The staged plan is admitted too: reviewers are
        # solicited to edit it, yet `_reviewable` drops plan paths.
        admitted = list(manifest.review_declared_paths)
        if plan_rel and plan_posix not in admitted:
            admitted.append(plan_posix)
        try:
            targets_add(root, session_id, admitted)
        except LedgerError:
            pass
    return manifest


CRITERION_UNREACHABLE_AT_STAGING = "CRITERION-UNREACHABLE-AT-STAGING"

_OUTSIDE_CREDIT_RE = re.compile(re.escape(OUTSIDE_CREDIT_REASON).replace(re.escape("{sha}"), r"(\S+)"))
_PREDECESSOR_PLAN_RE = re.compile(r"depends_on_plan (\S+\.md)\b")


def _criterion_blocking_drops(root: Path, exclusions: Sequence[Mapping]) -> list[dict]:
    """`{id, reason, predecessor_plan, predecessor_sizing}` for each row held on an uncoded
    predecessor plan (`depends_on_plan`) or transitively behind one. `exclusions` is the post-
    `only_rows` set, so a caller's narrowing never reaches here. One frontmatter read per distinct plan."""
    roots = {str(e.get("id")): e for e in exclusions if e.get("reason") == "depends_on_plan"}
    if not roots:
        return []
    sizing_by_plan: dict[str, Optional[str]] = {}

    def _plan_and_sizing(root_entry: Mapping) -> tuple[str, Optional[str]]:
        m = _PREDECESSOR_PLAN_RE.search(str(root_entry.get("detail") or ""))
        rel = m.group(1) if m else ""
        if rel and rel not in sizing_by_plan:
            sizing_by_plan[rel] = _plan_sizing_object(root, rel)
        return rel, (sizing_by_plan.get(rel) if rel else None)

    out: list[dict] = []
    for e in exclusions:
        rid = str(e.get("id"))
        if rid in roots:
            root_entry, reason = e, str(e.get("detail") or "depends_on_plan")
        elif e.get("reason") == "withheld_by_held_dependency" and str(e.get("root")) in roots:
            root_entry = roots[str(e["root"])]
            reason = f"withheld behind {e['root']}: {root_entry.get('detail') or 'depends_on_plan'}"
        else:
            continue
        plan, sizing = _plan_and_sizing(root_entry)
        out.append({"id": rid, "reason": reason, "predecessor_plan": plan, "predecessor_sizing": sizing})
    return out


def _plan_sizing_object(root: Path, plan_rel: str) -> Optional[str]:
    """The `sizing_object` frontmatter value of the repo plan at `plan_rel`, or None."""
    path = contained_path(root / plan_rel, [root])
    if path is None or not path.is_file():
        return None
    try:
        split = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if split is None:
        return None
    value = read_fm_field_unquoted(split.fm_text, "sizing_object")
    return value if value and value != "null" else None


def _unstaged_row_reasons(
    *,
    root: Path,
    ran_premises: Mapping[str, str],
    raw_by_id: Mapping[str, Mapping],
    parse_exclusions: Sequence[Mapping],
    staged: set,
    reported: set,
    resume_snap: Optional[resume_snapshot.Snapshot],
    plan_posix: str,
) -> tuple[dict[str, str], ...]:
    """`{id, reason}` for each unfinished spine row a refire neither staged nor reports under a
    manifest category. Refuses ROWS-DROPPED-WITHOUT-REASON, naming the rows, when one has no reason:
    the snapshot's, the exclusion's, or (no snapshot) the caller's exact `only_rows`."""
    excl = {str(e.get("id")): e for e in parse_exclusions}
    dropped: list[dict[str, str]] = []
    unreasoned: list[str] = []
    for rid, raw in raw_by_id.items():
        disposition = (with_canonical_disposition(raw) or {}).get("disposition")
        if disposition in NON_DISPATCHABLE_DISPOSITIONS or rid in staged or rid in reported:
            continue
        reason = (resume_snap.reasons.get(rid) if resume_snap else None) or ""
        if not reason and rid in excl:
            e = excl[rid]
            reason = " ".join(str(x) for x in (e.get("reason"), e.get("detail")) if x)
        if not reason and rid in ran_premises:
            reason = f"premise already ran in a prior leg ({ran_premises[rid]}); not re-run"
        if not reason:
            reason = _held_behind_reason(raw, raw_by_id, resume_snap)
        if not reason and resume_snap is None:
            reason = "outside the caller's only_rows"
        if reason:
            dropped.append({"id": rid, "reason": reason})
        else:
            unreasoned.append(rid)
    if unreasoned:
        raise AskStageError(
            f"{ROWS_DROPPED_WITHOUT_REASON}: refire of {plan_posix} leaves unfinished row(s) "
            f"{sorted(unreasoned)} unstaged with no recorded reason; add each id to only_rows, or "
            "rewrite the resume snapshot with a reason for it",
            plan_path=plan_posix,
        )
    return tuple(dropped)


def _run_started_at(root: Path, run_id: str) -> float:
    """Earliest mtime among the run record's files, 0.0 when the record is absent."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        return min((p.stat().st_mtime for p in record_dir(root, run_id).iterdir() if p.is_file()), default=0.0)
    except (OSError, ValueError):
        return 0.0


def _premise_report_in_prior_leg(root: Path, plan_posix: str, pred: str, run_started: float) -> str:
    """The repo-relative DONE report of premise row `pred` written since this run's record began, else
    ``""``. A writes-nothing premise lands no commit and stays `open`, so the report is the only proof it
    ran. A refire sets the report aside as `<id>.prior.md`, so both spellings count. Two bounded file
    reads, no git spawn."""
    live = _dispatch_report_path(plan_posix, pred)
    for rel in (live, live[: -len(".md")] + ".prior.md"):
        try:
            path = root / rel
            if path.stat().st_mtime < run_started:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        if _DELIVERED_REPORT_RE.search(text) and not _UNDELIVERED_REPORT_RE.search(text):
            return rel
    return ""


def _settle_ran_premises(
    rows: list, raw_by_id: Mapping[str, Mapping], root: Path, plan_posix: str, run_id: str
) -> tuple[list, dict[str, str]]:
    """`(rows, {premise id: report})` for a refire: a writes-nothing premise row that already finished in
    a prior leg is dropped from `rows` (it stays `open` forever, so keeping it re-runs it every leg and
    holds its dependents behind it) and every epistemic-premise edge onto it is stripped."""
    ran: dict[str, str] = {}
    started: Optional[float] = None
    ids = {r.id for r in rows}
    for row in rows:
        for edge in row.depends_on:
            pred = edge.get("chunk") if isinstance(edge, Mapping) else None
            if (
                not isinstance(edge, Mapping)
                or edge.get("gate_kind") != EPISTEMIC_PREMISE
                or pred in ran
                or pred not in ids
                or raw_by_id.get(pred, {}).get("writes") != []
            ):
                continue
            started = _run_started_at(root, run_id) if started is None else started
            report = _premise_report_in_prior_leg(root, plan_posix, str(pred), started)
            if report:
                ran[str(pred)] = report
    if not ran:
        return rows, ran

    def _strip(row):
        edges = [
            e for e in row.depends_on
            if not (isinstance(e, Mapping) and e.get("gate_kind") == EPISTEMIC_PREMISE and e.get("chunk") in ran)
        ]
        return row._replace(depends_on=edges) if len(edges) != len(row.depends_on) else row

    return [_strip(r) for r in rows if r.id not in ran], ran


def _held_behind_reason(
    raw: Mapping,
    raw_by_id: Mapping[str, Mapping],
    resume_snap: Optional[resume_snapshot.Snapshot],
) -> str:
    """`held behind <id> (<gate_kind>)` when the row carries a runtime-order edge onto a predecessor
    that is itself unfinished (dispatchable and not landed per the snapshot), else ``""``."""
    held: list[str] = []
    for edge in raw.get("depends_on") or ():
        if not isinstance(edge, Mapping) or edge.get("gate_kind") not in _RUNTIME_ORDER_GATE_KINDS:
            continue
        pred = str(edge.get("chunk"))
        pred_raw = raw_by_id.get(pred)
        if pred_raw is None or (resume_snap is not None and pred in resume_snap.not_to_run):
            continue
        if (with_canonical_disposition(pred_raw) or {}).get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
            continue
        held.append(f"{pred} ({edge.get('gate_kind')})")
    return "held behind " + ", ".join(held) if held else ""


def _record_staging_drops(record: Path, run_id: str, dropped: Sequence[Mapping[str, str]]) -> None:
    """One `staging_drop` notes line per dropped row, skipping a (row, reason, run) already held."""
    if not dropped:
        return
    from datetime import datetime, timezone

    from coordinator_core.atomic_append import append_line
    from coordinator_core.session.declared_writes import declare_write

    notes = record / NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    held = {(e["id"], e["reason"], e["run"]) for e in run_notes.drop_entries(notes)}
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wrote = False
    for d in dropped:
        if (d["id"], d["reason"][: run_notes.REASON_CAP], run_id) in held:
            continue
        append_line(notes, run_notes.drop_line(d["id"], d["reason"], at, run_id))
        wrote = True
    if wrote:
        declare_write(notes)


def _adopts_stranded(raw: Optional[Mapping]) -> bool:
    """True exactly when the plan-task is em-inferred and its rationale names the token `stranded`."""
    auth = (raw or {}).get("authorization")
    if not isinstance(auth, Mapping) or auth.get("source") != "em-inferred":
        return False
    return re.search(r"\bstranded\b", str(auth.get("rationale") or "")) is not None


def derive_held_ids(
    raw_by_id: Mapping[str, Mapping],
    recorded: Optional[Mapping],
    staged_ids: Sequence[str],
    landed_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Plan chunks a ledger resume holds unrun, in spine order.

    A `recorded` manifest that carries `held_ids` keeps them. One recorded before the key existed
    yields the executor-run (non-`verification`) spine rows it neither staged, owed, gated nor
    slot-withheld, that this stage does not stage and that `landed_ids` does not name. No recorded
    manifest yields ``()``: nothing says what the run ran.

    Trap: `held_ids` is serialised only when non-empty, so a manifest written by an earlier ledger
    resume that held nothing reads as pre-change; `landed_ids` (the gate's resume snapshot) is what
    keeps the rows that run landed from being re-derived as held.
    """
    if not isinstance(recorded, Mapping):
        return ()
    if "held_ids" in recorded:
        return tuple(str(i) for i in recorded.get("held_ids") or ())
    seen = {str(i) for i in (*staged_ids, *landed_ids)}
    for key in ("rows", "owed", "gated", "slot_withheld", "engine_rows"):
        seen.update(str(r.get("id")) for r in recorded.get(key) or () if isinstance(r, Mapping))
    return tuple(
        rid
        for rid, raw in raw_by_id.items()
        if rid not in seen
        and raw.get("disposition", "open") in _MINTED_DISPOSITIONS
        and raw.get("change_kind") != "verification"
    )


def without_held_rows(
    rulable: Sequence[str], held_ids: Sequence[str], raw_by_id: Mapping[str, Mapping]
) -> tuple[str, ...]:
    """`rulable` less the register rows a held chunk serves: they have unrun work, so the ledger
    loop recovers them rather than yielding to an EM ruling."""
    served = {
        str(r) for cid in held_ids for r in (raw_by_id.get(cid) or {}).get("register_rows") or ()
    }
    return tuple(r for r in rulable if r not in served)


def _recorded_manifest(record: Path) -> Optional[dict]:
    """The run's previously staged `manifest.json`, or None when absent or unreadable."""
    try:
        data = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stage_write_baseline(
    root: Path, rows: Sequence[ManifestRow], record: Optional[Path] = None, sizing_rel: Optional[str] = None
) -> dict:
    """Fingerprint every non-adopting row's declared file write (absent -> None); directories and globs are skipped.
    Spawn-free; runs at stage, before any executor.

    Trap: a refire (`record` holds a `halted.json` carrying `run_base_sha`) keeps the baseline the
    halted run's own stage recorded for every path it covers, since the live tree already holds that
    fire's output. A refire never reads the live tree: a path the prior manifest never baselined (or
    a manifest that is absent, unreadable or lacks `write_baseline`) takes its blob at the run base,
    an in-process object read (one `git cat-file --batch --filters` only for conversions it cannot
    reproduce); a failed lookup raises `RefireBaselineError`."""
    prior: Mapping = {}
    source = _baseline_source(root, record)
    base = halted_run_base(source) if source is not None else None
    if base:
        recorded = (_recorded_manifest(source) or {}).get("write_baseline")
        if isinstance(recorded, Mapping):
            prior = recorded
    paths: list[str] = []
    for r in rows:
        if r.adopts_stranded:
            continue
        for w in r.writes:
            if any(ch in w for ch in "*?[") or w.endswith("/") or (root / w).is_dir():
                continue
            paths.append(w)
    wanted = _dedupe_preserve_order(paths)
    missing = [p for p in wanted if p not in prior]
    if base:
        fresh = _run_base_blobs(root, base, missing)
    else:
        fresh = _write_baseline.exclude_halted_withheld(
            root, _write_baseline.capture(root, missing), sizing_rel, record.name if record is not None else ""
        )
    return {p: prior[p] if p in prior else fresh[p] for p in wanted if p in prior or p in fresh}


def _baseline_source(root: Path, record: Optional[Path]) -> Optional[Path]:
    """The record whose baseline a refire inherits: `record` itself when it holds a halted base, else
    the OLDEST `resumed_from` predecessor holding one (None: no refire).

    Trap: the oldest wins, never the nearest. An intermediate pass baselined against the tree the
    first pass already edited, so only the first pass's baseline still sees that edit as written work.
    The walk is cycle-safe, capped at `CARRIED_HOP_CAP` hops, and ends quietly on a bad link."""
    if record is None:
        return None
    if halted_run_base(record):
        return record
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    found: Optional[Path] = None
    visited = {record.name}
    cur = record
    for _ in range(rl.CARRIED_HOP_CAP):
        prev = (rl.read_run(cur) or {}).get(rl.RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and prev not in visited):
            break
        try:
            cur = record_dir(root, prev)
        except ValueError:
            break
        visited.add(prev)
        if halted_run_base(cur):
            found = cur
    return found


class RefireBaselineError(AskStageError):
    """A refire's write baseline could not be read at the halted run's `run_base_sha`."""


def _run_base_blobs(root: Path, base: str, paths: Sequence[str]) -> dict[str, Optional[str]]:
    """`blob_sha` of each path's content at commit `base` after checkout conversion (None: absent there).

    Trap: the domain is `kept_work.fingerprint` (raw working-tree bytes), so the base content is the
    checkout-converted blob; a committed blob sha would never equal an unchanged file under an eol rule.
    Zero spawns on the common path (an in-process object read). A path whose checkout conversion
    cannot be reproduced in process (`filter=`, `text=auto`, a config-set eol, `core.autocrlf` truthy at
    any config level, an included or env-injected config, native CRLF) takes ONE
    `git cat-file --batch --filters` for all such paths together and succeeds."""
    if not paths:
        return {}
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import read_commit, read_tree_spine
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    fail = f"refire write_baseline: cannot read paths at run_base_sha {base}"
    if any(ch.isspace() for p in paths for ch in p):
        raise RefireBaselineError(f"{fail} (a whitespace path cannot be named to cat-file --batch)")
    commit = read_commit(root, base)
    if commit is None:
        raise RefireBaselineError(f"{fail} (not a readable commit)")
    spine = read_tree_spine(root, list(paths), root_tree_sha=commit.tree)
    if spine is None:
        raise RefireBaselineError(f"{fail} (unreadable tree)")
    common = resolve_git_common_dir(root)
    config_forces_git = _config_forces_git(root)
    out: dict[str, Optional[str]] = {}
    needs_git: list[str] = []
    for p in paths:
        entry = spine.get(p.rpartition("/")[0], {}).get(p.rpartition("/")[2])
        if entry is None or entry[0] in (0o40000, 0o160000):
            out[p] = None
            continue
        obj = read_object(common, entry[1])
        if obj is None or obj[0] != "blob":
            raise RefireBaselineError(f"{fail} (blob {entry[1]} of {p} unreadable)")
        data = obj[1]
        if entry[0] != 0o120000:
            converted = None if config_forces_git else _checkout_convert(root, p, data)
            if converted is None:
                needs_git.append(p)
                continue
            data = converted
        out[p] = blob_sha(data)
    if needs_git:
        out.update(_git_filtered_blobs(root, base, needs_git, fail))
    return {p: out[p] for p in paths}


def _git_filtered_blobs(root: Path, base: str, paths: Sequence[str], fail: str) -> dict[str, Optional[str]]:
    """`blob_sha` of each path at `base` as git's checkout converts it: ONE `git cat-file --batch --filters`."""
    import uuid

    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    # Git's `--filters` header reports the unconverted size, so entries are split on a sentinel
    # object (answered `<sentinel> missing`), never on the header size.
    tok = uuid.uuid4().hex
    lines = [base, f"{tok}0"]
    for i, p in enumerate(paths, 1):
        lines += [f"{base}:{p} {p}", f"{tok}{i}"]
    res = run_git(
        ["cat-file", "--batch", "--filters"],
        cwd=str(root),
        input=("\n".join(lines) + "\n").encode("utf-8"),
    )
    data = res.stdout_bytes
    segments: list[bytes] = []
    pos = 0
    for i in range(len(paths) + 1):
        end = data.find(f"{tok}{i} missing\n".encode(), pos)
        if res.returncode != 0 or end < 0:
            raise RefireBaselineError(f"{fail} (git cat-file rc={res.returncode})")
        segments.append(data[pos:end])
        pos = end + len(f"{tok}{i} missing\n")
    out: dict[str, Optional[str]] = {}
    common = _write_baseline.common_dir_for(root)
    for i, seg in enumerate(segments):
        head, _, body = seg.partition(b"\n")
        parts = head.split(b" ")
        if i == 0:
            if len(parts) != 3 or parts[1] != b"commit":
                raise RefireBaselineError(f"{fail} (not a commit)")
            continue
        if len(parts) == 3 and parts[1] == b"blob":
            blob = body[:-1]
            out[paths[i - 1]] = blob_sha(blob)
            _write_baseline.persist_raw(common, blob)
        else:
            out[paths[i - 1]] = None
    return out


_CONFIG_CONVERSION_KEY = re.compile(r"^\s*(?:autocrlf\s*(?:=\s*(?P<crlf>[^\s#;]*))?|eol\s*=|attributesfile\s*=|\[\s*include)", re.I | re.M)
_CONFIG_ENV_KEYS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")


def _config_forces_git(root: Path) -> bool:
    """True when any config level git reads could change checkout conversion, or cannot be read here.

    Over-triggering only costs the one spawn; under-triggering would baseline the wrong content.
    Levels: system (`GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_SYSTEM`), global (`GIT_CONFIG_GLOBAL`, else
    XDG then `~/.gitconfig`), repo, and `config.worktree`; env-injected config and any `[include`
    section force it."""
    import os

    from coordinator_core.git.content_hash import _read_text_scoped, _system_gitconfig_paths
    from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

    env = os.environ
    if any(env.get(k) for k in _CONFIG_ENV_KEYS):
        return True
    if env.get("GIT_CONFIG_NOSYSTEM"):
        files: list[Path] = []
    elif env.get("GIT_CONFIG_SYSTEM"):
        files = [Path(env["GIT_CONFIG_SYSTEM"])]
    else:
        files = list(_system_gitconfig_paths())
    if "GIT_CONFIG_GLOBAL" in env:
        if env["GIT_CONFIG_GLOBAL"]:
            files.append(Path(env["GIT_CONFIG_GLOBAL"]))
    else:
        try:
            home = Path.home()
        except (RuntimeError, KeyError):
            return True
        xdg = env.get("XDG_CONFIG_HOME")
        files.append((Path(xdg) if xdg else home / ".config") / "git" / "config")
        files.append(home / ".gitconfig")
    files += [resolve_git_common_dir(root) / "config", resolve_git_dir(root) / "config.worktree"]
    for cfg in files:
        for m in _CONFIG_CONVERSION_KEY.finditer(_read_text_scoped(cfg) or ""):
            if (m.group("crlf") or "true").lower() not in ("false", "input"):
                return True
    return False


def _checkout_convert(root: Path, rel: str, data: bytes) -> Optional[bytes]:
    """`data` as git's checkout would write `rel`: LF->CRLF under an explicit `eol=crlf`, verbatim when
    nothing converts, None when the conversion is not reproduced in process (the caller takes git)."""
    from coordinator_core.git.checkin_attrs import BINARY, TEXT, UNSET, checkin_disposition
    from coordinator_core.git.content_hash import _attributes_pattern_matches, _clean_filter_may_apply, _read_text_scoped
    from coordinator_core.git.git_dir import resolve_git_dir

    if _clean_filter_may_apply(root, rel):
        return None
    disposition = checkin_disposition(root, rel)
    if disposition == BINARY:
        return data
    if disposition not in (TEXT, UNSET):
        return None
    eol: Optional[str] = None
    parts = rel.split("/")
    # Later wins: shallow to deep `.gitattributes`, then `info/attributes`, which git ranks highest.
    candidates = [
        (root.joinpath(*parts[:depth]) / ".gitattributes", "/".join(parts[depth:])) for depth in range(len(parts))
    ]
    candidates.append((resolve_git_dir(root) / "info" / "attributes", rel))
    for attrs, sub in candidates:
        text = _read_text_scoped(attrs)
        for line in (text or "").splitlines():
            toks = line.split("#", 1)[0].split()
            if len(toks) > 1 and _attributes_pattern_matches(toks[0], sub):
                for tok in toks[1:]:
                    if tok == "text=auto":
                        return None
                    if tok.startswith("eol="):
                        eol = tok[4:]
    if disposition == UNSET:
        return data
    if eol is None:
        import os

        return None if os.linesep == "\r\n" else data
    if eol == "lf":
        return data
    if eol == "crlf":
        return re.sub(rb"(?<!\r)\n", b"\r\n", data)
    return None


def manifest_payload(root: Path, manifest: StageManifest) -> dict:
    """`manifest.to_json()` plus `head_sha`: the execute-start HEAD (stage commits nothing), None when unresolvable.

    Adds `outside_credited` (`[{id, sha}]`) when a dropped row was credited as landed outside the executor,
    so the stage reply names each credited row and sha beside the one notes line."""
    payload = {**manifest.to_json(), "head_sha": read_head_sha(root)}
    credited = [
        {"id": d["id"], "sha": m.group(1)}
        for d in manifest.dropped_rows
        if (m := _OUTSIDE_CREDIT_RE.match(str(d.get("reason") or "")))
    ]
    if credited:
        payload["outside_credited"] = credited
    return payload


def _consume_park_marker(root: Path, run_id: str) -> Optional[dict]:
    """The `stop_after_source` dict (`{run_id, from, inherited}`) when the run, or an ancestor in its lineage,
    holds a `stop_after == "plan"` marker the run has not yet parked on; None otherwise. The run's own marker is unlinked only
    once `halted.json` records the park, so a parked reply lost in relay parks again rather than staging
    past the revise. An ancestor's marker is never unlinked here: a successor with no park recorded
    parks, and one whose `halted.json` records the park has discharged it. A marker written after its
    pass staged (`stop_after_plan_live`) is stale and never counts. An absent, unreadable or
    malformed marker and a lineage without one is None. An ancestor reached through a resume edge that
    already recorded its park is discharged (`stop_after_source`), so a refire never re-parks on it."""
    try:
        dir_ = record_dir(root, run_id)
    except ValueError:
        return None
    marker = dir_ / STOP_AFTER_FILE
    source = stop_after_source(root, run_id)
    if source is None:
        return None
    try:
        halted = json.loads((dir_ / "halted.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        halted = None
    if not (isinstance(halted, dict) and halted.get("halted") == HALT_PARKED):
        return source
    if stop_after_plan_live(dir_):
        try:
            marker.unlink()
        except OSError:
            pass
    return None


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
        plan_text=plan_text,
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


@register_op("dispatch.ask_stage")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_stage": params `run_id` (str), exactly one of `plan_path` / `sizing_path`, `writes`.

    Replies `{"manifest_handle": {path, sha256, bytes}}` over the `manifest.json` that `stage()` wrote
    (`StageManifest.to_json()` plus `head_sha`), never the manifest itself; read it with
    `reply_sidecar.read_reply`. A refused request replies `{"error": str}`. A run whose record dir
    carries a `stop_after=plan` marker replies `{"parked": "plan", "plan_path": str|None}` and stages
    nothing, until its `halted.json` records the park; the next call then consumes the marker and stages.

    Runs under leg attribution: the process_time row carries `legs` and `handler_thread_ms`.
    """
    token = leg_attribution.begin()
    t0 = time.thread_time()
    try:
        return _run_ask_stage(params, repo_root)
    finally:
        try:
            fields = {"handler_thread_ms": round((time.thread_time() - t0) * 1000.0, 1)}
            legs = leg_attribution.summary(top=3, floor_ms=50.0)
            if legs:
                fields["legs"] = legs
            op_row_fields.attach(**fields)
        finally:
            leg_attribution.end(token)


def _run_ask_stage(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return {"error": "dispatch.ask_stage requires repo_root"}
    refusal = validate_params("dispatch.ask_stage", params, _PARAMS)
    if refusal is not None:
        return refusal
    writes = params.get("writes") or []
    run_id = params["run_id"]
    root = main_worktree_root(Path(repo_root))
    stamp_plan_phase(root, run_id)
    park_source = _consume_park_marker(root, run_id)
    if park_source is not None:
        stamp_run_stage(root, run_id, "halted")
        return {PARKED_REPLY_KEY: STOP_AFTER_PLAN, "plan_path": params.get("plan_path") or None, PARK_SOURCE_REPLY_KEY: park_source}
    digest = ask_stage_receipt.params_digest(params)
    plan_parse: Optional[_PlanParse] = None
    with leg("preflight"):
        spine_sha = _spine_sha(root, params.get("plan_path") or None)
    try:
        prior_dir = record_dir(root, run_id)
    except ValueError:
        prior_dir = None
    if prior_dir is not None:
        with leg("preflight"):
            recorded = ask_stage_receipt.landed(prior_dir, digest, spine_sha)
            if recorded is not None:
                again = {"manifest_handle": recorded, ask_stage_receipt.ALREADY_STAGED_KEY: True}
                _, again_twin = reply_sidecar.handle_and_twin(root, prior_dir / "manifest.json")
                if again_twin is not None:
                    again["twin_path"] = again_twin
                return again
            running = ask_stage_receipt.in_flight(prior_dir, digest)
            if running is not None:
                return {ask_stage_receipt.STAGE_IN_FLIGHT_KEY: running}
            if ask_stage_receipt.partial(prior_dir):
                return {
                    "error": f"{STAGE_RECEIPT_MISSING}: manifest.json landed for run {run_id} with no matching "
                    "stage receipt. Reconcile the run; nothing was written."
                }
            parsed: list = []
            escaping = _escaping_write_refusal(
                root, params.get("plan_path") or None, params.get("only_rows"), parse_out=parsed
            )
            if parsed:
                plan_parse = parsed[0]
            if escaping is not None:
                return {"error": escaping}
        prior_dir.mkdir(parents=True, exist_ok=True)
        try:
            with leg("claim"):
                claimed = ask_stage_receipt.claim(prior_dir, digest)
        except ask_stage_receipt.ClaimCreateStalled as exc:
            return {"error": f"{STAGE_CLAIM_STALLED}: {exc}. Nothing was staged; re-fire the ask."}
        if not claimed:
            # A racing claim of other params has no matching digest, so `in_flight` is None; the key still answers.
            return {ask_stage_receipt.STAGE_IN_FLIGHT_KEY: ask_stage_receipt.in_flight(prior_dir, digest) or {}}
    stamp = None
    try:
        stage(
            root,
            run_id=run_id,
            plan_rel=params.get("plan_path") or None,
            sizing_rel=params.get("sizing_path") or None,
            writes=writes,
            gated=[g for g in (params.get("gated") or []) if isinstance(g, dict)],
            cross_repo_approved=bool(params.get("cross_repo_approved")),
            session_id=params.get("session_id") or None,
            commit_sizing_rel=params.get("commit_sizing_path") or None,
            only_rows=params["only_rows"] if params.get("only_rows") is not None else None,
            claim_register_rows=bool(params.get("claim_register_rows")),
            plan_parse=plan_parse,
        )
        if params.get("plan_path"):
            with leg("stamp"):
                stamp = stamp_plan_executing(root, params["plan_path"])
        with leg("receipt"):
            handle, twin_rel = reply_sidecar.handle_and_twin(root, record_dir(root, run_id) / "manifest.json")
            ask_stage_receipt.write(record_dir(root, run_id), digest, handle, spine_sha)
    except StageWaiting as exc:
        wait = exc.wait
        return {
            PARKED_REPLY_KEY: STAGE_WAIT_PARKED,
            "plan_path": params.get("plan_path") or None,
            "waiting": {"blockers": wait["blockers"], "deadline": wait["deadline"]},
            "remedy": f"A blocker known dead: {release_remedy(wait['blockers'])}.",
        }
    except (AskStageError, SizingFireRefused, reply_sidecar.ReplySidecarError) as exc:
        reply = {"error": str(exc)}
        if getattr(exc, "owed_em_ids", None):
            reply["owed_em_ids"] = exc.owed_em_ids
            reply["owed_plan"] = exc.plan_path
            if getattr(exc, "owed_memo_rows", None):
                reply["owed_memo_rows"] = exc.owed_memo_rows
        if getattr(exc, "gated_rows", None):
            reply["gated_rows"] = exc.gated_rows
            reply["owed_plan"] = exc.plan_path
        if getattr(exc, "criterion_blocking_drops", None):
            reply["criterion_blocking_drops"] = exc.criterion_blocking_drops
            reply["owed_plan"] = exc.plan_path
        return reply
    finally:
        if prior_dir is not None:
            with leg("release"):
                ask_stage_receipt.release(prior_dir)
    reply = {"manifest_handle": handle}
    if twin_rel is not None:
        reply["twin_path"] = twin_rel
    if stamp is not None and stamp["outcome"] != "unchanged":
        reply[PLAN_EXECUTING_STAMP_KEY] = stamp
    return reply
